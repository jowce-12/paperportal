"""논문 중요도 점수 (0~100).

- 모든 지표는 로그 스케일이라 1 → 10 → 100으로 늘 때 비슷한 폭씩 오른다.
- 인용은 오래된 논문일수록 쌓이므로, 1년 넘은 논문은 연평균으로 환산한다.
- 갓 나온 논문은 인용이 거의 0이라 HF 추천 수와 학회 채택 여부가 주된 신호가 된다.

가중치를 바꾸면 다음 서버 시작(또는 지표 업데이트) 때 모든 논문의 점수를 다시 계산한다.
"""

from datetime import datetime, timezone
from math import log2
from typing import Mapping

CITATION_WEIGHT = 6  # 연간 인용 1회 ≈ 6점, 15회 ≈ 24점, 100회 ≈ 40점
INFLUENTIAL_WEIGHT = 8  # 연간 주요 인용 1회 ≈ 8점, 7회 ≈ 24점, 31회 ≈ 40점
HF_UPVOTE_WEIGHT = 5  # HF 추천 15개 ≈ 20점, 100개 ≈ 33점, 300개 ≈ 41점
VENUE_BONUS = 10  # 학회·저널 채택 (본 트랙)
VENUE_TRACK_BONUS = 4  # 워크숍·Findings 채택


def parts(paper: Mapping, now: datetime | None = None) -> dict[str, float]:
    """항목별 점수. paper는 papers 테이블의 행(dict 또는 sqlite3.Row)."""
    now = now or datetime.now(timezone.utc)
    years = max((now - datetime.fromisoformat(paper["published"])).days / 365.25, 1.0)
    result: dict[str, float] = {}
    if paper["citation_count"]:
        result["citations"] = CITATION_WEIGHT * log2(1 + paper["citation_count"] / years)
    if paper["influential_citation_count"]:
        result["influential"] = INFLUENTIAL_WEIGHT * log2(1 + paper["influential_citation_count"] / years)
    if paper["hf_upvotes"]:
        result["hf_upvotes"] = HF_UPVOTE_WEIGHT * log2(1 + paper["hf_upvotes"])
    if paper["venue_status"] == "accepted":
        result["venue"] = VENUE_TRACK_BONUS if paper["venue_track"] else VENUE_BONUS
    return {key: round(value, 1) for key, value in result.items()}


def score(paper: Mapping, now: datetime | None = None) -> int:
    return min(100, round(sum(parts(paper, now).values())))
