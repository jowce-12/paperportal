"""arXiv comment / journal_ref에서 제출처(학회·저널)를 추출한다.

예) "Accepted to the 3D4S Workshop at CVPR 2026"   -> CVPR, 2026, accepted, workshop
    "Submitted to IEEE Transactions on Image Processing. Under review" -> TIP, None, review

이 파일을 수정하면 다음 서버 시작 시 저장된 모든 논문의 제출처를 다시 계산한다.
"""

import hashlib
import re
from pathlib import Path

# 분야별 (표시 이름, 정규식). 약어는 대소문자를 구분하고, 앞뒤가 영문자가 아닐 때만 매칭된다.
# 흔한 단어와 겹치는 짧은 약어는 뒤에 연도가 붙을 때만 인정한다.
# 분야는 "학회별 가져오기"의 선택 목록에 그대로 쓰인다.
VENUE_GROUPS: dict[str, list[tuple[str, str]]] = {
    "Computer Vision · Graphics": [
        ("CVPR", r"CVPR|Conference on Computer Vision and Pattern Recognition"),
        ("ICCV", r"ICCV|International Conference on Computer Vision(?! and Pattern)"),
        ("ECCV", r"ECCV|European Conference on Computer Vision"),
        ("WACV", r"WACV|Winter Conference on Applications of Computer Vision"),
        ("BMVC", r"BMVC|British Machine Vision Conference"),
        ("ACCV", r"ACCV|Asian Conference on Computer Vision"),
        ("3DV", r"3DV|International Conference on 3D Vision"),
        ("TPAMI", r"T-?PAMI|Transactions on Pattern Analysis and Machine Intelligence"),
        ("IJCV", r"IJCV|International Journal of Computer Vision"),
        ("TIP", r"IEEE TIP|Transactions on Image Processing"),
        ("MICCAI", r"MICCAI|Medical Image Computing and Computer[- ]Assisted Intervention"),
        ("SIGGRAPH Asia", r"SIGGRAPH Asia"),
        ("SIGGRAPH", r"SIGGRAPH(?! Asia)"),
        ("Eurographics", r"Eurographics"),
        ("Pacific Graphics", r"Pacific Graphics"),
    ],
    "Machine Learning": [
        ("NeurIPS", r"(?i:NeurIPS)|NIPS|Neural Information Processing Systems"),
        ("ICML", r"ICML|International Conference on Machine Learning"),
        ("ICLR", r"ICLR|International Conference on Learning Representations"),
        ("AAAI", r"AAAI"),
        ("IJCAI", r"IJCAI"),
        ("AISTATS", r"AISTATS"),
        ("UAI", r"UAI(?=\s?['’]?\d{2})"),
        ("COLT", r"COLT(?=\s?['’]?\d{2})"),
        ("TMLR", r"TMLR|Transactions on Machine Learning Research"),
        ("JMLR", r"JMLR|Journal of Machine Learning Research"),
        ("KDD", r"KDD"),
        ("WWW", r"WWW(?=\s?['’]?\d{2})|The Web Conference|TheWebConf"),
        ("SIGIR", r"SIGIR"),
        ("ACM MM", r"ACM ?MM|ACM Multimedia"),
    ],
    "NLP · Speech": [
        ("ACL", r"ACL|Annual Meeting of the Association for Computational Linguistics"),
        ("EMNLP", r"EMNLP|Empirical Methods in Natural Language Processing"),
        ("NAACL", r"NAACL"),
        ("EACL", r"EACL"),
        ("AACL", r"AACL"),
        ("COLING", r"COLING"),
        ("COLM", r"COLM|Conference on Language Modeling"),
        ("TACL", r"TACL|Transactions of the Association for Computational Linguistics"),
        ("Interspeech", r"(?i:Interspeech)"),
        ("ICASSP", r"ICASSP"),
    ],
    "Robotics": [
        ("ICRA", r"ICRA"),
        ("IROS", r"IROS"),
        ("CoRL", r"CoRL|Conference on Robot Learning"),
        ("RSS", r"RSS(?=\s?['’]?\d{2})|Robotics: Science and Systems"),
    ],
}

VENUES = [venue for group in VENUE_GROUPS.values() for venue in group]

_VENUE_PATTERNS = [
    (name, re.compile(rf"(?<![A-Za-z])(?:{pattern})(?![A-Za-z])")) for name, pattern in VENUES
]

# arXiv 검색어 (comment·journal_ref 필드). 없으면 표시 이름으로 검색한다.
# 검색은 넓게 하고, 실제로 그 학회인지는 저장 전에 detect()로 다시 확인한다.
ARXIV_SEARCH_TERMS: dict[str, list[str]] = {
    "ICCV": ["ICCV", "International Conference on Computer Vision"],
    "ECCV": ["ECCV", "European Conference on Computer Vision"],
    "TPAMI": ["TPAMI", "Pattern Analysis and Machine Intelligence"],
    "IJCV": ["IJCV", "International Journal of Computer Vision"],
    "TIP": ["Transactions on Image Processing"],
    "NeurIPS": ["NeurIPS", "NIPS"],
    "ICML": ["ICML", "International Conference on Machine Learning"],
    "ICLR": ["ICLR", "International Conference on Learning Representations"],
    "WWW": ["WWW", "Web Conference"],
    "ACM MM": ["ACM MM", "ACM Multimedia"],
    "COLM": ["COLM", "Conference on Language Modeling"],
    "CoRL": ["CoRL", "Conference on Robot Learning"],
    "RSS": ["RSS", "Robotics Science and Systems"],
}

# 목록에 없는 학회: "ICANN 2026"처럼 대문자 약어 바로 뒤에 연도가 오는 경우.
_GENERIC = re.compile(r"(?<![A-Za-z])([A-Z]{2,}[A-Za-z0-9]*)[\s-]?20\d{2}(?!\d)")
_GENERIC_STOPWORDS = {"IEEE", "ACM", "CVF", "AI", "LLM", "LLMS", "GPT", "COVID", "SOTA", "ARXIV"}
_GENERIC_CONTEXT = re.compile(
    r"(?i)accept|submit|present|appear|publish|conference|workshop|proceedings|review"
)

_ACCEPTED = re.compile(
    r"(?i)accept|to appear|appear(?:s|ed)? (?:in|at)|published (?:in|at|by|as)|presented (?:at|in)"
    r"|camera[- ]ready|\boral\b|spotlight|highlight|best paper|in proceedings|main conference"
)
# 제출처 이름이 없는 절에서 채택을 판단할 때는 오탐이 적은 표현만 쓴다.
_ACCEPTED_STRONG = re.compile(r"(?i)accept|to appear|camera[- ]ready")
_REVIEW = re.compile(
    r"(?i)submitted|under (?:review|submission)|in (?:review|submission)|\bsubmission\b"
)
_YEAR = re.compile(r"(?<!\d)(20\d{2})(?!\d)|['’](\d{2})(?!\d)")
_CLAUSE_SPLIT = re.compile(r"[;\n]|\.\s")

RULES_VERSION = hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:12]

EMPTY = {"venue": None, "venue_year": None, "venue_status": None, "venue_track": None}


def _hits(clauses: list[str], text: str) -> list[tuple[int, int, int, str]]:
    """(절 번호, 절 안 위치, 길이, 제출처) 목록."""
    hits = [
        (i, m.start(), m.end() - m.start(), name)
        for i, clause in enumerate(clauses)
        for name, pattern in _VENUE_PATTERNS
        for m in pattern.finditer(clause)
    ]
    if hits or not _GENERIC_CONTEXT.search(text):
        return hits
    return [
        (i, m.start(), len(m.group(1)), m.group(1))
        for i, clause in enumerate(clauses)
        for m in _GENERIC.finditer(clause)
        if m.group(1).upper() not in _GENERIC_STOPWORDS
    ]


def _nearest_year(clause: str, pos: int) -> int | None:
    years = [
        (abs(m.start() - pos), int(m.group(1) or f"20{m.group(2)}"))
        for m in _YEAR.finditer(clause)
    ]
    return min(years)[1] if years else None


def _analyze(text: str, published: bool) -> dict | None:
    clauses = _CLAUSE_SPLIT.split(text)
    hits = _hits(clauses, text)
    if not hits:
        return None

    def has_keyword(clause: str) -> bool:
        return bool(_ACCEPTED.search(clause) or _REVIEW.search(clause))

    # "Extended version of our ICCV 2025 paper. Submitted to TPAMI" -> TPAMI.
    # accepted/submitted 같은 키워드가 있는 절을 우선하고, 그다음 먼저 언급된 곳, 같은 위치면 긴 이름.
    i, pos, _, name = min(hits, key=lambda h: (not has_keyword(clauses[h[0]]), h[0], h[1], -h[2]))
    clause = clauses[i]

    # 제출처 이름이 없는 다른 절 (예: "Submitted to MICCAI 2026; early accepted on May 7")
    venue_clauses = {h[0] for h in hits}
    rest = [c for j, c in enumerate(clauses) if j not in venue_clauses]
    if published or _ACCEPTED.search(clause) or any(_ACCEPTED_STRONG.search(c) for c in rest):
        status = "accepted"
    elif _REVIEW.search(clause) or any(_REVIEW.search(c) for c in rest):
        status = "review"
    else:
        status = "accepted"  # "ECCV 2026"처럼 이름만 적힌 경우

    track = (
        "workshop" if re.search(r"(?i)workshop", clause)
        else "findings" if re.search(r"(?i)findings", clause)
        else None
    )
    return {
        "venue": name,
        "venue_year": _nearest_year(clause, pos),
        "venue_status": status,
        "venue_track": track,
    }


def detect(comment: str | None, journal_ref: str | None) -> dict:
    # journal_ref(출판 정보)가 있으면 우선한다.
    for text, published in ((journal_ref, True), (comment, False)):
        if text and (result := _analyze(text, published)):
            return result
    return dict(EMPTY)


def matches(comment: str | None, journal_ref: str | None, venue: str, year: int | None) -> bool:
    """이 논문의 제출처가 venue(와 year)인지. 연도가 적혀 있지 않으면 연도는 따지지 않는다."""
    found = detect(comment, journal_ref)
    return found["venue"] == venue and (year is None or found["venue_year"] in (None, year))


def arxiv_query(venue: str, year: int | None) -> str:
    """학회명을 comment·journal_ref에서 찾는 arXiv API 검색식."""

    def field(term: str) -> str:
        value = term if term.isalnum() else f'"{term}"'
        return f"co:{value} OR jr:{value}"

    terms = ARXIV_SEARCH_TERMS.get(venue, [venue])
    names = " OR ".join(field(term) for term in terms)
    if year is None:
        return names
    # "ECCV2026"처럼 붙여 쓴 경우는 한 단어로 색인되므로 따로 찾는다.
    joined = [field(f"{term}{year}") for term in terms if term.isalnum()]
    return " OR ".join([f"(({names}) AND (co:{year} OR jr:{year}))", *joined])
