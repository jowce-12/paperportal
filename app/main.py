import logging
from contextlib import asynccontextmanager
from typing import Literal

import arxiv
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles

from . import db, fetcher, metrics, venues
from .config import MAJOR_VENUES, STATIC_DIR, TOPICS


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    if unknown := set(MAJOR_VENUES) - {name for name, _ in venues.VENUES}:
        logging.getLogger("uvicorn.error").warning(
            "config.MAJOR_VENUES에 venues.py에 없는 이름이 있습니다 (논문이 매칭되지 않음): %s",
            ", ".join(sorted(unknown)),
        )
    yield


app = FastAPI(title="PaperPortal", lifespan=lifespan)


def _check_topic(topic: str) -> None:
    if topic not in TOPICS:
        raise HTTPException(404, f"알 수 없는 토픽: {topic}")


@app.get("/api/topics")
def get_topics():
    stats = db.topic_stats()
    topics = [
        {
            "key": key,
            "label": t["label"],
            "description": t["description"],
            "count": stats.get(key, {}).get("count", 0),
            "last_fetched_at": stats.get(key, {}).get("last_fetched_at"),
            "metrics_updated_at": stats.get(key, {}).get("metrics_updated_at"),
        }
        for key, t in TOPICS.items()
    ]
    return {
        "total": stats["_all"]["count"],
        "metrics_updated_at": stats["_all"]["metrics_updated_at"],
        "topics": topics,
    }


@app.get("/api/papers")
def get_papers(
    topic: str | None = None,
    q: str = "",
    venue: str | None = None,
    scope: Literal["major", "any"] | None = None,
    accepted: bool = False,
    sort: Literal[tuple(db.SORTS)] = "published",  # type: ignore[valid-type]
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """DB에서만 읽는다. 외부 API에는 요청하지 않는다.
    scope: major면 주요 학회 논문만, any면 제출처가 확인된 논문만."""
    if topic:
        _check_topic(topic)
    return db.list_papers(
        topic=topic, q=q, venue=venue, scope=scope, accepted=accepted,
        sort=sort, limit=limit, offset=offset,
    )


@app.post("/api/metrics")
def post_metrics(topic: str | None = None, force: bool = False):
    """인용 수(Semantic Scholar)와 HF 추천 수를 갱신한다. 부분 실패는 errors에 담는다."""
    if topic:
        _check_topic(topic)
    try:
        return metrics.update(topic, force)
    except metrics.MetricsBusyError as e:
        raise HTTPException(409, str(e))


@app.get("/api/venues")
def get_venues(topic: str | None = None, q: str = "", accepted: bool = False):
    """현재 토픽·검색어에 해당하는 논문의 제출처별 개수."""
    if topic:
        _check_topic(topic)
    return {"venues": db.list_venues(topic, q, accepted), "major": MAJOR_VENUES}


def _venue_catalog() -> list[dict]:
    """주요 학회를 맨 위에, 나머지는 분야별로 (한 학회는 한 번만)."""
    groups = [{"label": "주요 학회", "venues": MAJOR_VENUES}]
    groups += [
        {"label": label, "venues": names}
        for label, entries in venues.VENUE_GROUPS.items()
        if (names := [name for name, _ in entries if name not in MAJOR_VENUES])
    ]
    known = {name for name, _ in venues.VENUES}
    # 목록에는 없지만 저장된 논문에서 발견된 학회 (예: ICANN)
    if others := sorted(set(db.venue_names()) - known):
        groups.append({"label": "기타 (저장된 논문에서 발견)", "venues": others})
    return groups


def _check_venue(venue: str | None, year: int | None) -> None:
    if year is not None and not venue:
        raise HTTPException(400, "연도는 학회와 함께 지정해야 합니다.")
    if venue and not any(venue in group["venues"] for group in _venue_catalog()):
        raise HTTPException(400, f"알 수 없는 학회: {venue}")


@app.get("/api/venue-catalog")
def get_venue_catalog():
    """학회별 가져오기에서 고를 수 있는 학회 목록."""
    return {"groups": _venue_catalog()}


@app.get("/api/venue-fetch/{topic}")
def get_venue_fetch(topic: str, venue: str, year: int | None = Query(None, ge=1990, le=2100)):
    """학회별 가져오기 상태: 지금까지 검색한 범위와 저장된 논문 수."""
    _check_topic(topic)
    _check_venue(venue, year)
    return db.venue_fetch_status(topic, venue, year)


@app.post("/api/fetch/{topic}")
def post_fetch(
    topic: str,
    mode: Literal["new", "older"] = "new",
    venue: str | None = None,
    year: int | None = Query(None, ge=1990, le=2100),
):
    _check_topic(topic)
    _check_venue(venue, year)
    try:
        return fetcher.fetch(topic, mode, venue, year)
    except fetcher.FetchBusyError as e:
        raise HTTPException(409, str(e))
    except fetcher.FetchFailed as e:
        cause = e.__cause__
        if isinstance(cause, arxiv.HTTPError) and cause.status == 429:
            status, message = 503, "arXiv 요청 한도를 초과했습니다 (HTTP 429). 몇 분 뒤 다시 시도하세요."
        elif isinstance(cause, arxiv.HTTPError):
            status, message = 502, f"arXiv 요청 실패 (HTTP {cause.status})."
        else:
            status, message = 502, f"arXiv 요청 실패: {cause}"
        if e.saved:
            message += f" 실패 전까지 받은 {e.saved}편은 저장했습니다."
        raise HTTPException(status, message)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
