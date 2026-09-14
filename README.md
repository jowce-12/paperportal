# PaperPortal

arXiv의 LLM / Vision 논문을 [`arxiv`](https://pypi.org/project/arxiv/) 라이브러리로 가져와
SQLite에 저장하고 웹페이지로 보여줍니다. 제출처, 인용 수, 중요도도 함께 보여줍니다.
페이지를 열거나 검색할 때는 DB만 읽고, 외부 API(arXiv, Semantic Scholar, Hugging Face)에는
**가져오기·업데이트 버튼(또는 CLI)을 누를 때만** 요청합니다.

## 실행

```powershell
uv sync
uv run uvicorn app.main:app --reload
```

http://127.0.0.1:8000 을 엽니다.

## 가져오기 동작

| 동작 | arXiv 요청 | 설명 |
| --- | --- | --- |
| 페이지 열기 · 검색 · 페이지 이동 | 없음 | `data/papers.db`에서만 읽음 (1페이지당 10편) |
| 새 논문 가져오기 (처음) | 최신 200편 | `INITIAL_FETCH` |
| 새 논문 가져오기 (이후) | 최신순으로 받다가 이미 받은 구간에 닿으면 멈춤 | 최대 `MAX_NEW_FETCH`편 |
| 이전 논문 더 가져오기 | 이미 받은 구간 이전 200편 | `submittedDate` 범위 검색, `OLDER_FETCH` |
| 학회별 가져오기 → 가져오기 | 토픽 검색에 학회명(과 연도)을 더해 위와 같은 방식으로 검색 | 제출처가 그 학회로 확인된 논문만 저장 |
| 학회별 가져오기 → 이전 것 더 가져오기 | 같은 조건으로 이미 검색한 구간 이전 200편 | 결과가 끝나면 "더 이전 결과 없음" |

- 검색(토픽, 또는 토픽 + 학회 + 연도)마다 지금까지 받은 구간을 `fetch_ranges`에 기록하고, 그 바깥만 요청합니다. 그래서 학회별 가져오기를 해도 일반 "새 논문 가져오기"가 중간 논문을 건너뛰지 않습니다.
- 한 논문이 두 토픽에 모두 해당하면 (예: 멀티모달) 논문은 한 번만 저장되고 토픽에 각각 연결됩니다.
- 새 버전(v2, v3…)이 받아지면 기존 행을 갱신합니다.
- arXiv API 약관에 따라 요청 사이에 3초씩 대기합니다. HTTP 429가 나오면 몇 분 뒤 다시 시도하세요.

## 제출처(학회·저널)별 보기

arXiv에는 제출처 전용 필드가 없어서, 저자가 적은 `journal_ref`와 `comment`
(예: "Accepted to the 3D4S Workshop at CVPR 2026")에서 [app/venues.py](app/venues.py)의 규칙으로 추출합니다.

- **제출처 탭**
  - `전체 논문`: 기본값. 모든 논문을 보여줍니다.
  - `주요 학회`: 주요 학회 논문만 보여주고, 칩도 주요 학회만 표시합니다.
  - `모든 학회`: 제출처가 확인된 논문을 모두 보여주며, 주요 학회 칩에는 ★이 붙습니다.
  - 주요 학회 목록은 [app/config.py](app/config.py)의 `MAJOR_VENUES`에서 바꿉니다. 기본값은 CVPR, ICCV, ECCV, NeurIPS, ICML, ICLR, ACL, EMNLP, NAACL, COLM, AAAI, TPAMI입니다.
- **제출처 칩**: 현재 토픽·검색어 기준 제출처별 논문 수. 누르면 해당 제출처만 표시합니다.
- **채택된 논문만**: "Submitted to", "Under review" 같은 심사 중 논문은 제외합니다.
- 카드의 배지에는 연도와 트랙(Workshop, Findings)을 함께 표시합니다. 심사 중 논문은 점선 배지로 표시하고, 배지를 누르면 그 제출처로 필터링합니다.
- 주요 CV·ML·NLP·로보틱스 학회와 저널은 규칙 목록으로 찾습니다. 목록에 없는 곳은 `ICANN 2026`처럼 "대문자 약어 + 연도" 형태일 때만 인식합니다.
- 신규 arXiv 논문은 대부분 제출처를 적지 않습니다. 이런 논문은 제출처 필터에 나오지 않습니다.

`VENUE_GROUPS` 목록에 학회를 추가하는 등 `venues.py`를 수정하면, 다음 서버 시작 때 저장된 모든 논문의 제출처를 다시 계산합니다. arXiv에 다시 요청하지는 않습니다.

### 학회별로 가져오기

"학회별 가져오기"를 누르고 학회와 연도를 고른 뒤 "가져오기"를 누릅니다(전체 탭에서는 토픽이 없어 숨겨집니다). 학회 목록은 주요 학회가 맨 위에 있고, 나머지는 분야별로 묶여 있습니다.

1. arXiv에서 comment 또는 journal_ref에 학회명이 적힌 현재 토픽의 논문을 검색합니다. 예: Vision + CVPR 2026이면 `(cat:cs.CV) AND (((co:CVPR OR jr:CVPR) AND (co:2026 OR jr:2026)) OR co:CVPR2026 OR jr:CVPR2026)`
2. 검색은 넓게 걸리므로, 받은 논문마다 위의 제출처 추출 규칙으로 실제 그 학회(와 연도)인지 다시 확인합니다. 예를 들어 "Extended version of our CVPR 2025 paper. Submitted to TPAMI"는 CVPR로 저장하지 않습니다.
3. 가져온 뒤에는 목록이 그 학회로 필터링됩니다.

- 학회명을 적지 않은 논문은 찾을 수 없습니다. 학회 논문 전체가 아니라 **arXiv에 학회명을 표기한 논문**만 대상입니다.
- 학회별 검색어는 `ARXIV_SEARCH_TERMS`에서 바꿀 수 있습니다(예: NeurIPS는 NIPS도 검색).

## 인용 수 · 중요도

각 논문 카드 오른쪽(모바일은 아래)에 표시하고, 목록을 **중요도순 · 인용순 · 주요 인용순 · HF 추천순**으로 정렬할 수 있습니다.

| 지표 | 출처 | 설명 |
| --- | --- | --- |
| 인용 | [Semantic Scholar](https://www.semanticscholar.org/) | 전체 인용 수 |
| 주요 인용 | Semantic Scholar | Highly Influential Citations. 이 논문의 방법이나 결과를 비중 있게 다룬 인용 |
| HF 추천 | [Hugging Face Daily Papers](https://huggingface.co/papers) | 커뮤니티 추천 수. Daily Papers에 선정된 논문에만 있음 |
| 중요도 | [app/scoring.py](app/scoring.py) | 위 지표와 학회 채택 여부를 합친 0~100점 |

**중요도 계산:** 각 지표를 로그 스케일로 점수화해 더합니다(최대 100).
- 인용과 주요 인용은 1년 넘은 논문이면 연평균으로 환산해서, 오래된 논문이 인용 누적만으로 유리해지지 않게 합니다.
- 학회 채택은 +10점, 워크숍이나 Findings는 +4점입니다.
- 등급: 60점 이상 매우 높음, 35점 이상 높음, 15점 이상 보통입니다. 점수에 마우스를 올리면 항목별 점수가 보입니다.

> 게시된 지 며칠 안 된 논문은 인용이 거의 0입니다. 이런 논문은 HF 추천 수와 학회 채택 여부로만 점수가 매겨집니다.

**업데이트:** "인용·추천 업데이트" 버튼을 누르거나 CLI로 실행합니다. 새 논문을 가져온 뒤에는 자동으로 이어서 실행됩니다.
- 24시간 안에 확인한 논문은 건너뜁니다(`METRICS_MAX_AGE_HOURS`).
- Semantic Scholar는 500편당 요청 1번, Hugging Face는 날짜당 요청 1번이면 됩니다.
- Semantic Scholar 요청 한도가 부족하면 [API 키](https://www.semanticscholar.org/product/api#api-key)를 받아 `S2_API_KEY` 환경변수로 지정하세요.

## CLI / 예약 실행

```powershell
uv run python -m app.cli                  # 모든 토픽의 새 논문 + 인용·추천 수
uv run python -m app.cli vision --older   # Vision 토픽의 이전 논문 + 인용·추천 수
uv run python -m app.cli vision --venue CVPR --year 2026           # Vision 토픽의 CVPR 2026 논문
uv run python -m app.cli llm --venue ACL --year 2026 --older       # 같은 조건으로 이전 것 더
uv run python -m app.cli --metrics-only   # 인용·추천 수만 (--force: 24시간 캐시 무시)
```

arXiv는 하루에 한 번 새 논문을 공개하므로, Windows 작업 스케줄러에 하루 한 번 등록해 두면 충분합니다.

## 설정

토픽과 검색 쿼리, 가져오기 개수는 [app/config.py](app/config.py)에서 바꿉니다.
토픽을 추가하면 탭도 자동으로 생깁니다. DB 위치는 `PAPERPORTAL_DB` 환경변수로 바꿀 수 있습니다.

## 데이터 (`data/papers.db`)

지금까지 가져온 논문 DB가 저장소에 포함되어 있어, 클론하면 arXiv에 요청하지 않고 바로 볼 수 있습니다.
DB를 커밋할 때는 **서버를 끈 뒤** 커밋하세요. SQLite WAL 모드라서 서버 실행 중의 변경은
`papers.db-wal`에만 남아 있을 수 있고, 이 임시 파일은 `.gitignore`로 제외됩니다.

## 구조

```
app/
  config.py   토픽 쿼리 · 가져오기 설정
  db.py       SQLite 스키마 · 마이그레이션 · 쿼리 (papers, paper_topics, fetch_ranges, fetch_log, hf_days, meta)
  venues.py   comment / journal_ref에서 제출처 추출, 학회별 arXiv 검색식
  scoring.py  중요도 점수 계산식
  fetcher.py  arXiv 가져오기 (증분 · 과거)
  metrics.py  인용 수(Semantic Scholar) · HF 추천 수 가져오기
  main.py     FastAPI (GET /api/topics, /api/papers, /api/venues, /api/venue-catalog,
                           /api/venue-fetch/{topic}
                       POST /api/fetch/{topic}?venue=&year=, /api/metrics)
  cli.py      명령줄 가져오기
static/       프론트엔드 (HTML · CSS · JS, 빌드 없음)
```
