import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("PAPERPORTAL_DB", BASE_DIR / "data" / "papers.db"))
STATIC_DIR = BASE_DIR / "static"

# 토픽별 arXiv 검색 쿼리.
# 문법: https://info.arxiv.org/help/api/user-manual.html#query_details
TOPICS: dict[str, dict[str, str]] = {
    "llm": {
        "label": "LLM",
        "description": "Large Language Models",
        "query": (
            "(cat:cs.CL OR cat:cs.AI OR cat:cs.LG) AND "
            '(ti:LLM OR abs:LLM OR ti:"language model" OR abs:"large language model")'
        ),
    },
    "vision": {
        "label": "Vision",
        "description": "Computer Vision",
        "query": "cat:cs.CV",
    },
}

# "주요 학회" 탭과 학회별 가져오기 목록 맨 위에 보일 학회. 이름은 venues.py의 표시 이름과 같아야 한다.
MAJOR_VENUES = [
    "CVPR", "ICCV", "ECCV",  # Computer Vision
    "NeurIPS", "ICML", "ICLR",  # Machine Learning
    "ACL", "EMNLP", "NAACL", "COLM",  # NLP · LLM
    "AAAI",  # AI 전반
    "TPAMI",  # 저널
]

INITIAL_FETCH = 200  # DB가 비어 있을 때 처음 가져올 개수
OLDER_FETCH = 200  # "이전 논문 더 가져오기" 1회당 개수
MAX_NEW_FETCH = 2000  # "새 논문 가져오기" 상한. 이미 저장된 논문에 닿으면 그 전에 멈춘다.

ARXIV_PAGE_SIZE = 100
ARXIV_DELAY_SECONDS = 3.0  # arXiv API 이용 약관: 요청 간 최소 3초
ARXIV_NUM_RETRIES = 1  # 429(요청 과다)일 때 즉시 재시도는 제한을 늘릴 뿐이라 낮게 유지

# 인용 수(Semantic Scholar) · HF 추천 수(Hugging Face Daily Papers)
METRICS_MAX_AGE_HOURS = 24  # 이보다 최근에 가져온 지표는 다시 요청하지 않는다
HF_WINDOW_DAYS = 14  # arXiv 게시 후 이 기간 안에 HF Daily Papers에 오른 것만 찾는다
S2_API_KEY = os.environ.get("S2_API_KEY")  # 선택. 있으면 Semantic Scholar 요청 한도가 넉넉해진다
