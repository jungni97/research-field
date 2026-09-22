# Paper Radar

예방의학 · 응급의학 분야에서 최근 6개월 이내 발표된 논문 중 "핫한" 논문을 추적하는
개인용 정적 대시보드입니다. 서버가 필요 없습니다.

## 점수 산정 방식

의학 논문은 출간 직후 인용수가 거의 쌓이지 않기 때문에, 인용 지표만으로는
"최근 화제작"을 가려내기 어렵습니다. 그래서 세 가지 지표를 정규화해 합산합니다.

| 지표 | 비중 | 설명 |
|---|---|---|
| Altmetric 주목도 | 기본점의 50% | 언론·SNS·블로그 등에서의 언급도 |
| 인용수 | 기본점의 30% | 현재까지의 피인용 수 (iCite 기준) |
| 저널 등급 | 기본점의 20% | NEJM·Lancet·JAMA 등 주요 저널 가중치 |
| **현재 호(issue) 게재** | **가산점 +10** | 그 저널의 가장 최근 정식 호에 배정된 논문이면 가산. 이미 편집진이 골라 실은 논문이라는 뜻이라 강한 신호로 별도 취급 |

기본점(위 세 지표 조합)은 90점 만점이고, 현재 호 가산점 10점을 더해 최종
`hot_score`가 최대 100점이 됩니다. "현재 호"인지는 PubMed의 volume/issue
정보로 판별합니다 — issue 번호가 없거나 "ahead of print" 상태면 아직 정식
호에 배정되지 않은 것으로 보고, 같은 저널 안에서 issue가 배정된 논문 중
가장 최근 날짜를 기준으로 45일 이내인 논문에 가산점을 줍니다
(`collect.py`의 `CURRENT_ISSUE_WINDOW_DAYS`로 조정 가능).

`collect.py`의 `JOURNAL_WEIGHTS` 딕셔너리를 수정하면 저널 가중치를 원하는 대로
바꿀 수 있고, `FIELDS`의 PubMed 쿼리를 수정하면 검색 범위(MeSH 용어)를 조정할 수
있습니다.

## 빠르게 한 번 써보기 (자동화 없이)

1. Python 환경에서:
   ```bash
   pip install requests
   python collect.py
   ```
   PubMed → iCite → Altmetric 순서로 조회하므로 논문 수에 따라 몇 분 걸릴 수
   있습니다.
2. `data/papers.json`이 생성되면, 같은 폴더에서 로컬 서버를 하나 띄워 index.html을
   엽니다. (브라우저가 `file://`에서는 fetch를 막는 경우가 많아 간단한 서버가
   필요합니다.)
   ```bash
   python -m http.server 8000
   ```
   그리고 브라우저에서 `http://localhost:8000` 접속.
3. 갱신하고 싶을 때마다 `python collect.py`를 다시 실행하면 됩니다.

## 완전 자동화 (매일 무인 갱신, 무료)

서버 없이 완전히 자동으로 매일 갱신되게 하려면 GitHub만 있으면 됩니다.

1. 이 폴더를 새 GitHub 저장소에 올립니다.
2. 저장소의 **Settings → Pages**에서 `main` 브랜치, 루트(`/`)를 소스로 지정해
   GitHub Pages를 켭니다. → `https://<사용자명>.github.io/<저장소명>/`에서
   바로 접속 가능.
3. `.github/workflows/update.yml`이 이미 포함되어 있어서, 별도 설정 없이
   매일 UTC 20:00(한국시간 새벽 5시)에 자동으로 `collect.py`를 실행하고
   `data/papers.json`을 커밋합니다.
4. **Actions** 탭에서 `Update paper data` 워크플로우를 수동으로 한 번
   실행(`Run workflow`)해서 첫 데이터를 채워주세요.

이후로는 매일 아침 접속할 때마다 최신 데이터가 반영되어 있습니다.

## 커스터마이징 아이디어

- `RELDATE_DAYS`를 바꿔 "최근 N일" 범위를 조정
- `JOURNAL_WEIGHTS`에 자주 보는 저널을 추가
- Altmetric API는 무료 조회에 속도 제한이 있어 논문 수가 많으면 시간이 걸립니다.
  `MAX_PER_FIELD`를 낮추면 더 빨리 끝납니다.
- 분야를 더 추가하고 싶다면 `FIELDS` 딕셔너리에 PubMed MeSH 쿼리를 하나 더
  넣으면 됩니다.
