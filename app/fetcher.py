import re
import threading
from datetime import datetime
from typing import Literal

import arxiv
import requests

from . import db, venues
from .config import (
    ARXIV_DELAY_SECONDS,
    ARXIV_NUM_RETRIES,
    ARXIV_PAGE_SIZE,
    INITIAL_FETCH,
    MAX_NEW_FETCH,
    OLDER_FETCH,
    TOPICS,
)

FetchMode = Literal["new", "older"]

# arXiv 요청 제한 때문에 가져오기 작업은 한 번에 하나만 실행한다.
_lock = threading.Lock()


class FetchBusyError(RuntimeError):
    pass


class FetchFailed(RuntimeError):
    """arXiv 요청 실패. 원인은 __cause__에, 실패 전까지 받아 새로 저장한 논문 수는 saved에 담긴다."""

    def __init__(self, saved: int):
        super().__init__(f"arXiv 요청 실패 ({saved}편 저장됨)")
        self.saved = saved


def _to_row(r: arxiv.Result) -> dict:
    version = r.get_short_id()
    paper_id = re.sub(r"v\d+$", "", version)
    return {
        "id": paper_id,
        "version": version,
        "title": " ".join(r.title.split()),
        "summary": " ".join(r.summary.split()),
        "authors": [a.name for a in r.authors],
        "primary_category": r.primary_category,
        "categories": r.categories,
        "published": r.published.isoformat(),
        "updated": r.updated.isoformat(),
        "abs_url": f"https://arxiv.org/abs/{paper_id}",
        "pdf_url": r.pdf_url or f"https://arxiv.org/pdf/{version}",
        "comment": r.comment or None,
        "journal_ref": r.journal_ref or None,
    }


def fetch(
    topic: str, mode: FetchMode = "new", venue: str | None = None, year: int | None = None
) -> dict:
    """arXiv에서 논문을 가져와 DB에 저장한다.

    검색마다 지금까지 받은 제출 시각 범위(fetch_ranges)를 기준으로:
    - new:   최신순으로 받다가 범위의 가장 최신 논문보다 오래된 논문이 나오면 멈춘다.
             처음이면 INITIAL_FETCH개를 받는다.
    - older: 범위의 가장 오래된 논문 이전 것을 OLDER_FETCH개 받는다.

    venue를 주면 토픽 검색에 학회명 검색(comment·journal_ref)을 더하고,
    받은 논문 중 제출처가 그 학회(와 연도)로 확인된 것만 저장한다.
    """
    if not _lock.acquire(blocking=False):
        raise FetchBusyError("다른 가져오기 작업이 진행 중입니다.")
    try:
        known = db.get_range(db.source_key(topic, venue, year)) or {}
        query = TOPICS[topic]["query"]
        if venue:
            query = f"({query}) AND ({venues.arxiv_query(venue, year)})"
        stop_before: datetime | None = None

        if mode == "older" and known.get("oldest"):
            # arXiv 날짜 필터는 분 단위(UTC). 경계가 겹쳐 받은 중복은 upsert가 걸러낸다.
            upper = datetime.fromisoformat(known["oldest"]).strftime("%Y%m%d%H%M")
            query = f"({query}) AND submittedDate:[190001010000 TO {upper}]"
            limit = OLDER_FETCH
        elif mode == "new" and known.get("newest"):
            stop_before = datetime.fromisoformat(known["newest"])
            limit = MAX_NEW_FETCH
        else:
            limit = INITIAL_FETCH

        client = arxiv.Client(
            page_size=min(ARXIV_PAGE_SIZE, limit),
            delay_seconds=ARXIV_DELAY_SECONDS,
            num_retries=ARXIV_NUM_RETRIES,
        )
        search = arxiv.Search(
            query=query,
            max_results=limit,
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )

        rows: list[dict] = []
        reached_known = False

        def save(complete: bool) -> tuple[int, int]:
            """(학회 조건에 맞는 논문 수, 새로 저장된 수)"""
            matched = [
                row for row in rows
                if not venue or venues.matches(row["comment"], row["journal_ref"], venue, year)
            ]
            inserted = db.save_papers(topic, matched)
            # 학회 조건에 안 맞아 버린 논문도 받은 범위에는 넣는다 (다시 요청하지 않도록).
            # 처음/이전 가져오기가 상한보다 적게 끝났으면 더 오래된 검색 결과가 없다.
            exhausted = True if complete and stop_before is None and len(rows) < limit else None
            db.update_range(topic, venue, year, [row["published"] for row in rows], exhausted)
            db.log_fetch(topic, mode, len(rows), inserted)
            return len(matched), inserted

        try:
            for result in client.results(search):
                if stop_before and result.published < stop_before:
                    reached_known = True
                    break
                rows.append(_to_row(result))
        except (arxiv.ArxivError, requests.RequestException) as e:
            # 최신 논문 이어받기 도중 실패했을 때 일부만 저장하면, 받은 것과 기존 범위 사이에
            # 빈 구간이 생기고 다음 "new" 요청이 그 구간을 건너뛴다. 그 외에는 받은 만큼 저장한다.
            saved = save(complete=False)[1] if stop_before is None and rows else 0
            raise FetchFailed(saved) from e

        matched, inserted = save(complete=True)
        return {
            "topic": topic,
            "mode": mode,
            "venue": venue,
            "year": year,
            "received": len(rows),
            "matched": matched,
            "inserted": inserted,
            # 상한까지 받고도 기존 범위에 닿지 못했다면 중간에 빠진 논문이 있을 수 있다.
            "truncated": stop_before is not None and not reached_known and len(rows) >= limit,
            "exhausted": bool(db.get_range(db.source_key(topic, venue, year))["exhausted"]),
        }
    finally:
        _lock.release()
