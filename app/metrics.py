"""인용 수(Semantic Scholar)와 HF 추천 수(Hugging Face Daily Papers)를 가져와 DB에 저장한다."""

import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone

import requests

from . import db
from .config import HF_WINDOW_DAYS, METRICS_MAX_AGE_HOURS, S2_API_KEY

log = logging.getLogger(__name__)

S2_BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"
S2_BATCH_SIZE = 500  # batch API 최대치
HF_DAILY_URL = "https://huggingface.co/api/daily_papers"
HF_DAILY_START = date(2023, 5, 4)  # HF Daily Papers가 시작된 날
HF_PAGE_SIZE = 100
USER_AGENT = "paperportal/0.1"

_lock = threading.Lock()


class MetricsBusyError(RuntimeError):
    pass


def _request(method: str, url: str, headers: dict | None = None, **kwargs) -> requests.Response:
    """429·5xx면 2, 4, 8초 쉬었다가 다시 시도한다."""
    headers = {"User-Agent": USER_AGENT, **(headers or {})}
    for delay in (2, 4, 8, None):
        resp = requests.request(method, url, headers=headers, timeout=60, **kwargs)
        retryable = resp.status_code == 429 or resp.status_code >= 500
        if not retryable or delay is None:
            resp.raise_for_status()
            return resp
        log.info("HTTP %s from %s, retrying in %ss", resp.status_code, url, delay)
        time.sleep(delay)
    raise AssertionError("unreachable")


def _describe(err: requests.RequestException) -> str:
    if err.response is not None:
        if err.response.status_code == 429:
            return "요청 한도 초과 (HTTP 429). 잠시 뒤 다시 시도하세요."
        return f"HTTP {err.response.status_code}"
    return type(err).__name__


def fetch_citations(ids: list[str]) -> dict[str, tuple[str, int, int] | None]:
    """arXiv ID -> (S2 paperId, 인용 수, 주요 인용 수). Semantic Scholar에 없는 논문은 None. 최대 500개."""
    resp = _request(
        "POST",
        S2_BATCH_URL,
        headers={"x-api-key": S2_API_KEY} if S2_API_KEY else None,
        params={"fields": "citationCount,influentialCitationCount"},
        json={"ids": [f"ARXIV:{paper_id}" for paper_id in ids]},
    )
    return {
        paper_id: (item["paperId"], item["citationCount"], item["influentialCitationCount"]) if item else None
        for paper_id, item in zip(ids, resp.json())
    }


def fetch_hf_day(day: str) -> dict[str, int]:
    """그날 HF Daily Papers에 오른 논문의 arXiv ID -> 추천 수."""
    upvotes: dict[str, int] = {}
    page = 0
    while True:
        items = _request(
            "GET", HF_DAILY_URL, params={"date": day, "limit": HF_PAGE_SIZE, "p": page}
        ).json()
        upvotes.update({item["paper"]["id"]: item["paper"].get("upvotes") or 0 for item in items})
        if len(items) < HF_PAGE_SIZE:
            return upvotes
        page += 1


def _hf_days(papers: list[dict]) -> set[str]:
    """논문 게시일부터 HF_WINDOW_DAYS까지의 날짜 (오늘 이후 제외)."""
    today = datetime.now(timezone.utc).date()
    days: set[str] = set()
    for paper in papers:
        published = datetime.fromisoformat(paper["published"]).date()
        start = max(published, HF_DAILY_START)
        end = min(published + timedelta(days=HF_WINDOW_DAYS), today)
        days.update((start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1))
    return days


def update(topic: str | None = None, force: bool = False) -> dict:
    """인용 수를 가져온 지 METRICS_MAX_AGE_HOURS가 지난 논문의 지표를 갱신한다.
    force면 기간과 상관없이 모두 다시 가져온다. 한쪽 소스가 실패해도 다른 쪽은 계속 진행한다."""
    if not _lock.acquire(blocking=False):
        raise MetricsBusyError("인용·추천 수를 이미 업데이트하는 중입니다.")
    try:
        max_age = 0 if force else METRICS_MAX_AGE_HOURS
        papers = db.papers_for_metrics(topic, max_age)
        errors: list[str] = []

        cited = 0
        try:
            ids = [paper["id"] for paper in papers]
            for start in range(0, len(ids), S2_BATCH_SIZE):
                if start:
                    time.sleep(1)  # 비인증 요청은 초당 1회 정도로
                results = fetch_citations(ids[start : start + S2_BATCH_SIZE])
                db.save_citations(results)
                cited += sum(counts is not None for counts in results.values())
        except requests.RequestException as e:
            errors.append(f"Semantic Scholar: {_describe(e)}")

        hf_matched = 0
        try:
            for day in db.hf_days_due(_hf_days(papers), max_age, HF_WINDOW_DAYS):
                hf_matched += db.save_hf_day(day, fetch_hf_day(day))
        except requests.RequestException as e:
            errors.append(f"Hugging Face: {_describe(e)}")

        db.recompute_importance()
        return {"checked": len(papers), "cited": cited, "hf_matched": hf_matched, "errors": errors}
    finally:
        _lock.release()
