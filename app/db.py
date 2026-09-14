import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

from . import scoring, venues
from .config import DB_PATH, MAJOR_VENUES

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id               TEXT PRIMARY KEY,   -- 버전 없는 arXiv ID (예: 2409.01234)
    version          TEXT NOT NULL,      -- 버전 포함 ID (예: 2409.01234v2)
    title            TEXT NOT NULL,
    summary          TEXT NOT NULL,
    authors          TEXT NOT NULL,      -- JSON 배열
    primary_category TEXT,
    categories       TEXT NOT NULL,      -- JSON 배열
    published        TEXT NOT NULL,      -- ISO 8601 (UTC)
    updated          TEXT NOT NULL,
    abs_url          TEXT NOT NULL,
    pdf_url          TEXT NOT NULL,
    comment          TEXT,
    journal_ref      TEXT,
    venue            TEXT,               -- comment/journal_ref에서 추출 (venues.py)
    venue_year       INTEGER,
    venue_status     TEXT,               -- accepted | review
    venue_track      TEXT,               -- workshop | findings
    s2_paper_id                TEXT,     -- Semantic Scholar 논문 ID (링크용)
    citation_count             INTEGER,  -- Semantic Scholar (NULL: 아직 없음)
    influential_citation_count INTEGER,  -- Semantic Scholar "Highly Influential Citations"
    citations_updated_at       TEXT,
    hf_upvotes       INTEGER,            -- Hugging Face Daily Papers 추천 수 (NULL: 선정 안 됨)
    importance       INTEGER,            -- 0~100, scoring.py
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_papers_published ON papers (published DESC);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 날짜별 HF Daily Papers를 언제 가져왔는지 (같은 날짜를 반복 요청하지 않도록)
CREATE TABLE IF NOT EXISTS hf_days (
    day        TEXT PRIMARY KEY,         -- YYYY-MM-DD
    fetched_at TEXT NOT NULL
);

-- 한 논문이 여러 토픽에 속할 수 있다 (예: 멀티모달 논문은 LLM + Vision).
CREATE TABLE IF NOT EXISTS paper_topics (
    paper_id TEXT NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    topic    TEXT NOT NULL,
    PRIMARY KEY (paper_id, topic)
);
CREATE INDEX IF NOT EXISTS idx_paper_topics_topic ON paper_topics (topic);

-- 검색(토픽, 또는 토픽 + 학회 + 연도)별로 지금까지 빈틈 없이 받은 제출 시각 범위.
-- 가져오기는 이 범위 바깥만 요청한다. 토픽에 저장된 논문의 최신/최고 시각을 쓰면 안 되는 이유:
-- 학회별 가져오기로 받은 논문이 토픽 범위를 넓혀 일반 가져오기가 그 사이를 건너뛰게 된다.
CREATE TABLE IF NOT EXISTS fetch_ranges (
    source     TEXT PRIMARY KEY,         -- source_key() 참고
    topic      TEXT NOT NULL,
    venue      TEXT,
    venue_year INTEGER,
    newest     TEXT,                     -- 받은 논문 중 가장 최신 제출 시각 (없으면 NULL)
    oldest     TEXT,
    exhausted  INTEGER NOT NULL DEFAULT 0,  -- 더 오래된 검색 결과가 없음
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    topic      TEXT NOT NULL,
    mode       TEXT NOT NULL,            -- new | older
    fetched_at TEXT NOT NULL,
    received   INTEGER NOT NULL,
    inserted   INTEGER NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# 처음 버전 이후 papers에 추가된 컬럼. 기존 DB에는 ALTER TABLE로 붙인다.
_ADDED_COLUMNS = {
    "journal_ref": "TEXT",
    "venue": "TEXT",
    "venue_year": "INTEGER",
    "venue_status": "TEXT",
    "venue_track": "TEXT",
    "s2_paper_id": "TEXT",
    "citation_count": "INTEGER",
    "influential_citation_count": "INTEGER",
    "citations_updated_at": "TEXT",
    "hf_upvotes": "INTEGER",
    "importance": "INTEGER",
}


def init_db() -> None:
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(papers)")}
        for column, decl in _ADDED_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE papers ADD COLUMN {column} {decl}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_papers_venue ON papers (venue)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_papers_importance ON papers (importance DESC)")
        _backfill_topic_ranges(conn)
        _refresh_venues(conn)
        # 인용은 논문 나이로 환산하므로 시작할 때마다 다시 계산한다 (수천 편에 수십 ms).
        _recompute_importance(conn)


_SCORE_COLUMNS = (
    "id, published, citation_count, influential_citation_count, hf_upvotes, venue_status, venue_track"
)


def _recompute_importance(conn: sqlite3.Connection, ids: list[str] | None = None) -> None:
    if ids is None:
        rows = conn.execute(f"SELECT {_SCORE_COLUMNS} FROM papers").fetchall()
    else:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT {_SCORE_COLUMNS} FROM papers WHERE id IN ({placeholders})", ids
        ).fetchall()
    now = datetime.now(timezone.utc)
    conn.executemany(
        "UPDATE papers SET importance = ? WHERE id = ?",
        [(scoring.score(row, now), row["id"]) for row in rows],
    )


def recompute_importance() -> None:
    with connect() as conn:
        _recompute_importance(conn)


def _backfill_topic_ranges(conn: sqlite3.Connection) -> None:
    """fetch_ranges가 생기기 전의 DB: 토픽 논문은 모두 일반 가져오기로 받았으므로 저장된 범위를 그대로 쓴다."""
    if conn.execute("SELECT 1 FROM meta WHERE key = 'ranges_backfilled'").fetchone():
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO fetch_ranges (source, topic, newest, oldest, updated_at)
        SELECT t.topic || '||', t.topic, MAX(p.published), MIN(p.published), ?
        FROM paper_topics t JOIN papers p ON p.id = t.paper_id
        GROUP BY t.topic
        """,
        (_now(),),
    )
    conn.execute("INSERT INTO meta (key, value) VALUES ('ranges_backfilled', '1')")


def _refresh_venues(conn: sqlite3.Connection) -> None:
    """venues.py 규칙이 바뀌었으면 저장된 모든 논문의 제출처를 다시 계산한다."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'venue_rules'").fetchone()
    if row and row["value"] == venues.RULES_VERSION:
        return
    rows = conn.execute("SELECT id, comment, journal_ref FROM papers").fetchall()
    conn.executemany(
        """
        UPDATE papers SET venue = :venue, venue_year = :venue_year,
                          venue_status = :venue_status, venue_track = :venue_track
        WHERE id = :id
        """,
        [{**venues.detect(r["comment"], r["journal_ref"]), "id": r["id"]} for r in rows],
    )
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('venue_rules', ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (venues.RULES_VERSION,),
    )


def save_papers(topic: str, papers: list[dict]) -> int:
    """논문을 upsert하고 토픽에 연결한다. 이 토픽에 새로 추가된 논문 수를 반환."""
    inserted = 0
    now = _now()
    with connect() as conn:
        for p in papers:
            conn.execute(
                """
                INSERT INTO papers (id, version, title, summary, authors, primary_category,
                                    categories, published, updated, abs_url, pdf_url, comment,
                                    journal_ref, venue, venue_year, venue_status, venue_track, created_at)
                VALUES (:id, :version, :title, :summary, :authors, :primary_category,
                        :categories, :published, :updated, :abs_url, :pdf_url, :comment,
                        :journal_ref, :venue, :venue_year, :venue_status, :venue_track, :created_at)
                ON CONFLICT (id) DO UPDATE SET
                    version = excluded.version,
                    title = excluded.title,
                    summary = excluded.summary,
                    authors = excluded.authors,
                    primary_category = excluded.primary_category,
                    categories = excluded.categories,
                    updated = excluded.updated,
                    pdf_url = excluded.pdf_url,
                    comment = excluded.comment,
                    journal_ref = excluded.journal_ref,
                    venue = excluded.venue,
                    venue_year = excluded.venue_year,
                    venue_status = excluded.venue_status,
                    venue_track = excluded.venue_track
                WHERE excluded.updated >= papers.updated
                """,
                {
                    **p,
                    **venues.detect(p["comment"], p["journal_ref"]),
                    "authors": json.dumps(p["authors"], ensure_ascii=False),
                    "categories": json.dumps(p["categories"]),
                    "created_at": now,
                },
            )
            cur = conn.execute(
                "INSERT OR IGNORE INTO paper_topics (paper_id, topic) VALUES (?, ?)",
                (p["id"], topic),
            )
            inserted += cur.rowcount
        if papers:
            _recompute_importance(conn, [p["id"] for p in papers])
    return inserted


def papers_for_metrics(topic: str | None, max_age_hours: float) -> list[dict]:
    """인용 수를 한 번도 안 가져왔거나 max_age_hours보다 오래된 논문."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat(timespec="seconds")
    clause, params = _where(topic, "", False)
    stale = "(p.citations_updated_at IS NULL OR p.citations_updated_at <= ?)"
    clause = f"{clause} AND {stale}" if clause else f"WHERE {stale}"
    with connect() as conn:
        rows = conn.execute(
            f"SELECT p.id, p.published FROM papers p {clause} ORDER BY p.published DESC",
            [*params, cutoff],
        ).fetchall()
    return [dict(row) for row in rows]


def save_citations(results: dict[str, tuple[str, int, int] | None]) -> None:
    """(S2 paperId, 인용 수, 주요 인용 수). None이면 Semantic Scholar에 아직 없는 논문 — 기존 값은 유지한다."""
    now = _now()
    with connect() as conn:
        conn.executemany(
            """
            UPDATE papers SET s2_paper_id = COALESCE(?, s2_paper_id),
                              citation_count = COALESCE(?, citation_count),
                              influential_citation_count = COALESCE(?, influential_citation_count),
                              citations_updated_at = ?
            WHERE id = ?
            """,
            [(*(counts or (None, None, None)), now, paper_id) for paper_id, counts in results.items()],
        )


def hf_days_due(days: set[str], max_age_hours: float, final_after_days: int) -> list[str]:
    """아직 안 가져왔거나 다시 가져올 때가 된 날짜.
    날짜로부터 final_after_days가 지난 뒤에 가져온 적이 있으면 추천 수가 거의 변하지 않으므로 건너뛴다."""
    now = datetime.now(timezone.utc)
    with connect() as conn:
        fetched = {
            row["day"]: datetime.fromisoformat(row["fetched_at"])
            for row in conn.execute("SELECT day, fetched_at FROM hf_days")
        }
    due = []
    for day in sorted(days):
        at = fetched.get(day)
        day_start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
        if at is None:
            due.append(day)
        elif at - day_start >= timedelta(days=final_after_days):
            continue
        elif now - at >= timedelta(hours=max_age_hours):
            due.append(day)
    return due


def save_hf_day(day: str, upvotes: dict[str, int]) -> int:
    """그날 HF Daily Papers의 추천 수를 저장. DB에 있는 논문 중 반영된 수를 반환."""
    with connect() as conn:
        cur = conn.executemany(
            "UPDATE papers SET hf_upvotes = ? WHERE id = ?",
            [(count, paper_id) for paper_id, count in upvotes.items()],
        )
        conn.execute(
            "INSERT INTO hf_days (day, fetched_at) VALUES (?, ?)"
            " ON CONFLICT (day) DO UPDATE SET fetched_at = excluded.fetched_at",
            (day, _now()),
        )
        return cur.rowcount


def log_fetch(topic: str, mode: str, received: int, inserted: int) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO fetch_log (topic, mode, fetched_at, received, inserted) VALUES (?, ?, ?, ?, ?)",
            (topic, mode, _now(), received, inserted),
        )


def source_key(topic: str, venue: str | None = None, year: int | None = None) -> str:
    """fetch_ranges의 키. 예) "vision||", "vision|CVPR|2026", "llm|ACL|" """
    return f"{topic}|{venue or ''}|{year or ''}"


def get_range(source: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM fetch_ranges WHERE source = ?", (source,)).fetchone()
    return dict(row) if row else None


def update_range(
    topic: str,
    venue: str | None,
    year: int | None,
    published: list[str],
    exhausted: bool | None,
) -> None:
    """받은 논문의 제출 시각으로 범위를 넓힌다. exhausted가 None이면 기존 값을 유지한다."""
    source = source_key(topic, venue, year)
    with connect() as conn:
        row = conn.execute(
            "SELECT newest, oldest, exhausted FROM fetch_ranges WHERE source = ?", (source,)
        ).fetchone()
        known = [value for value in (row["newest"], row["oldest"]) if value] if row else []
        conn.execute(
            """
            INSERT INTO fetch_ranges (source, topic, venue, venue_year, newest, oldest, exhausted, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source) DO UPDATE SET
                newest = excluded.newest, oldest = excluded.oldest,
                exhausted = excluded.exhausted, updated_at = excluded.updated_at
            """,
            (
                source, topic, venue, year,
                max([*published, *known], default=None),
                min([*published, *known], default=None),
                int(exhausted) if exhausted is not None else (row["exhausted"] if row else 0),
                _now(),
            ),
        )


def venue_fetch_status(topic: str, venue: str, year: int | None) -> dict:
    """학회별 가져오기 패널용: 검색 범위와 저장된 논문 수."""
    clause, params = _where(topic, "", False, venue)
    if year is not None:
        clause += " AND p.venue_year = ?"
        params.append(year)
    with connect() as conn:
        count = conn.execute(f"SELECT COUNT(*) FROM papers p {clause}", params).fetchone()[0]
    return {"range": get_range(source_key(topic, venue, year)), "count": count}


def venue_names() -> list[str]:
    """DB에 저장된 논문에서 발견된 제출처 이름."""
    with connect() as conn:
        rows = conn.execute("SELECT DISTINCT venue FROM papers WHERE venue IS NOT NULL").fetchall()
    return [row[0] for row in rows]


def topic_stats() -> dict[str, dict]:
    with connect() as conn:
        counts = conn.execute(
            """
            SELECT t.topic, COUNT(*) AS count, MAX(p.citations_updated_at) AS metrics_updated_at
            FROM paper_topics t JOIN papers p ON p.id = t.paper_id
            GROUP BY t.topic
            """
        ).fetchall()
        fetches = conn.execute(
            "SELECT topic, MAX(fetched_at) AS last_fetched_at FROM fetch_log GROUP BY topic"
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) AS count, MAX(citations_updated_at) AS metrics_updated_at FROM papers"
        ).fetchone()
    stats: dict[str, dict] = {"_all": dict(total)}
    for row in counts:
        stats.setdefault(row["topic"], {}).update(
            count=row["count"], metrics_updated_at=row["metrics_updated_at"]
        )
    for row in fetches:
        stats.setdefault(row["topic"], {})["last_fetched_at"] = row["last_fetched_at"]
    return stats


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _where(
    topic: str | None,
    q: str,
    accepted: bool,
    venue: str | None = None,
    scope: str | None = None,
) -> tuple[str, list]:
    """scope: "major"면 주요 학회(MAJOR_VENUES) 논문만, "any"면 제출처가 확인된 논문만."""
    where: list[str] = []
    params: list = []
    if topic:
        where.append("p.id IN (SELECT paper_id FROM paper_topics WHERE topic = ?)")
        params.append(topic)
    for term in q.split():
        where.append(
            "(p.title LIKE ? ESCAPE '\\' OR p.summary LIKE ? ESCAPE '\\' OR p.authors LIKE ? ESCAPE '\\')"
        )
        params += [_like(term)] * 3
    if accepted:
        where.append("p.venue_status = 'accepted'")
    if venue:
        where.append("p.venue = ?")
        params.append(venue)
    if scope == "major":
        where.append(f"p.venue IN ({','.join('?' * len(MAJOR_VENUES))})")
        params += MAJOR_VENUES
    elif scope == "any":
        where.append("p.venue IS NOT NULL")
    return (f"WHERE {' AND '.join(where)}" if where else ""), params


def list_venues(topic: str | None, q: str, accepted: bool) -> list[dict]:
    """현재 필터(제출처 제외)에 해당하는 논문의 제출처별 개수. major: 주요 학회 여부."""
    clause, params = _where(topic, q, accepted, scope="any")
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT p.venue AS name, COUNT(*) AS count FROM papers p {clause}
            GROUP BY p.venue ORDER BY count DESC, p.venue
            """,
            params,
        ).fetchall()
    return [{**dict(row), "major": row["name"] in MAJOR_VENUES} for row in rows]


SORTS = {
    "published": "p.published DESC",
    "importance": "p.importance DESC NULLS LAST, p.published DESC",
    "citations": "p.citation_count DESC NULLS LAST, p.published DESC",
    "influential": "p.influential_citation_count DESC NULLS LAST, p.citation_count DESC NULLS LAST, p.published DESC",
    "hf": "p.hf_upvotes DESC NULLS LAST, p.published DESC",
}


def list_papers(
    *,
    topic: str | None,
    q: str = "",
    venue: str | None = None,
    scope: str | None = None,
    accepted: bool = False,
    sort: str = "published",
    limit: int,
    offset: int = 0,
) -> dict:
    clause, params = _where(topic, q, accepted, venue, scope)

    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM papers p {clause}", params).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT p.*,
                   (SELECT group_concat(topic) FROM paper_topics t WHERE t.paper_id = p.id) AS topics
            FROM papers p {clause}
            ORDER BY {SORTS[sort]}
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()

    items = []
    now = datetime.now(timezone.utc)
    for row in rows:
        item = dict(row)
        item["authors"] = json.loads(item["authors"])
        item["categories"] = json.loads(item["categories"])
        item["topics"] = sorted(item["topics"].split(",")) if item["topics"] else []
        item["importance_parts"] = scoring.parts(item, now)
        del item["created_at"]
        items.append(item)
    return {"total": total, "items": items}
