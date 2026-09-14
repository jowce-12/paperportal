"""명령줄에서 논문과 인용·추천 수 가져오기. 예약 작업(작업 스케줄러, cron)으로 돌리기 좋다.

    uv run python -m app.cli                  # 모든 토픽의 새 논문 + 인용·추천 수
    uv run python -m app.cli vision --older   # Vision 토픽의 이전 논문 + 인용·추천 수
    uv run python -m app.cli vision --venue CVPR --year 2026   # Vision 토픽의 CVPR 2026 논문
    uv run python -m app.cli --metrics-only   # 인용·추천 수만
"""

import argparse
import logging
import sys

from . import db, fetcher, metrics
from .config import TOPICS


def main() -> None:
    parser = argparse.ArgumentParser(description="arXiv 논문과 인용·추천 수를 가져와 DB에 저장합니다.")
    parser.add_argument("topics", nargs="*", help=f"토픽: {', '.join(TOPICS)} (기본: 전체)")
    parser.add_argument("--older", action="store_true", help="저장된 가장 오래된 논문 이전 것을 가져옵니다.")
    parser.add_argument("--venue", help="이 학회로 확인된 논문만 가져옵니다 (예: CVPR, NeurIPS, ACL).")
    parser.add_argument("--year", type=int, help="--venue와 함께: 학회 연도 (예: 2026)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--no-metrics", action="store_true", help="인용·추천 수는 업데이트하지 않습니다.")
    group.add_argument("--metrics-only", action="store_true", help="arXiv는 건너뛰고 인용·추천 수만 업데이트합니다.")
    parser.add_argument("--force", action="store_true", help="최근에 가져온 인용·추천 수도 다시 가져옵니다.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    if unknown := set(args.topics) - set(TOPICS):
        parser.error(f"알 수 없는 토픽: {', '.join(sorted(unknown))}")
    if args.year and not args.venue:
        parser.error("--year는 --venue와 함께 써야 합니다.")

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    db.init_db()
    topics = args.topics or list(TOPICS)
    failed = False

    if not args.metrics_only:
        for topic in topics:
            label = " ".join(str(part) for part in (topic, args.venue, args.year) if part)
            try:
                result = fetcher.fetch(topic, "older" if args.older else "new", args.venue, args.year)
            except fetcher.FetchFailed as e:
                print(f"[{label}] arXiv 실패: {e.__cause__} (저장 {e.saved}편)", file=sys.stderr)
                failed = True
                continue
            matched = f", 학회 확인 {result['matched']}편" if args.venue else ""
            print(f"[{label}] 받음 {result['received']}편{matched}, 새로 저장 {result['inserted']}편"
                  + (" (상한 도달: 중간 누락 가능)" if result["truncated"] else "")
                  + (" (더 이전 결과 없음)" if result["exhausted"] else ""))

    if not args.no_metrics:
        for topic in topics if args.topics else [None]:
            result = metrics.update(topic, force=args.force)
            print(f"[{topic or '전체'}] 인용 수 확인 {result['checked']}편 (Semantic Scholar에 있음 {result['cited']}편),"
                  f" HF 추천 반영 {result['hf_matched']}편")
            for error in result["errors"]:
                print(f"[{topic or '전체'}] {error}", file=sys.stderr)
                failed = True

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
