#!/usr/bin/env python3
"""
paper-radar/collect.py

최근 3년 이내 예방의학(Preventive Medicine) / 응급의학(Emergency Medicine) 논문을
PubMed에서 수집하고, 저널별로 묶어 "핫함 점수(hot_score)"와 함께
data/papers.json 으로 저장합니다.

출력 구조: 분야(field) -> 저널(journal) -> 그 저널의 논문 목록.
각 저널 그룹에는 실제 Journal Impact Factor(IF), WoS 색인 등급(SCIE/ESCI),
그 저널에서 가장 핫한 논문(top_paper)이 함께 표시됩니다.
각 분야에는 논문 제목에서 뽑은 "핫토픽 키워드"(hot_topics)도 함께 담깁니다.

점수 구성 (의학 논문은 출간 직후 인용수가 거의 안 쌓이므로, 인용 지표보다
"주목도" 지표의 비중을 더 크게 둡니다):
  - altmetric      (기본점의 50%) : 언론·SNS·블로그 등에서의 언급도
  - citations      (기본점의 30%) : 현재까지의 피인용수 (iCite 기준, RCR 병기)
  - impact factor  (기본점의 20%) : 저널의 실제 IF (IMPACT_FACTORS, 로그 스케일 정규화)
  - current_issue  (가산점 +10)   : 그 저널의 "가장 최근 정식 호(issue)"에 배정된
                                    논문이면 가산점 (편집진이 이미 골라 실은 논문).

필요 패키지: requests  (pip install requests)
실행: python collect.py
출력: data/papers.json
"""

import json
import math
import re
import time
from collections import Counter
from datetime import datetime, timedelta

import requests

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
ICITE = "https://icite.od.nih.gov/api/pubs"
ALTMETRIC = "https://api.altmetric.com/v1/pmid"
MYMEMORY = "https://api.mymemory.translated.net/get"

# MyMemory 무료 번역 API는 이메일 없이도 쓸 수 있지만 하루 무료 한도가 적습니다
# (약 5,000단어/일). 여기 본인 이메일을 넣으면 무료로 50,000단어/일까지 늘어납니다.
# (MyMemory 정책, 별도 가입 필요 없음 — 그냥 파라미터로만 붙습니다.)
MYMEMORY_EMAIL = ""  # 예: "you@example.com"

# 검색할 두 분야와 PubMed 쿼리 (MeSH 기반 + 자유어 보강)
# 분야를 더 추가하고 싶으면 이 딕셔너리에 항목을 하나 더 넣으면 됩니다.
FIELDS = {
    "preventive": {
        "label": "예방의학",
        "query": (
            '("preventive medicine"[MeSH Terms] OR "primary prevention"[MeSH Terms] '
            'OR "preventive health services"[MeSH Terms] OR "mass screening"[MeSH Terms] '
            'OR "health promotion"[MeSH Terms] OR "vaccination"[MeSH Terms])'
        ),
    },
    "emergency": {
        "label": "응급의학",
        "query": (
            '("emergency medicine"[MeSH Terms] OR "emergency service, hospital"[MeSH Terms] '
            'OR "emergency treatment"[MeSH Terms] OR "resuscitation"[MeSH Terms] '
            'OR "triage"[MeSH Terms])'
        ),
    },
}

# 주요 저널의 실제 Journal Impact Factor (2024/2025 JCR 기준, 대략값).
# 목록에 없는 저널은 DEFAULT_IMPACT_FACTOR를 사용합니다.
# 값이 바뀌면 (매년 6월 JCR 갱신) 여기 숫자만 고치면 됩니다.
IMPACT_FACTORS = {
    "The New England Journal of Medicine": 78.5,
    "N Engl J Med": 78.5,
    "Lancet": 88.5,
    "The Lancet": 88.5,
    "JAMA": 55.0,
    "JAMA internal medicine": 23.3,
    "JAMA Internal Medicine": 23.3,
    "JAMA Network Open": 9.6,
    "BMJ": 93.6,
    "BMJ (Clinical research ed.)": 93.6,
    "Annals of internal medicine": 15.2,
    "Annals of Internal Medicine": 15.2,
    "Annals of emergency medicine": 5.0,
    "Annals of Emergency Medicine": 5.0,
    "Academic emergency medicine": 3.2,
    "Academic Emergency Medicine": 3.2,
    "Resuscitation": 5.4,
    "The American journal of emergency medicine": 2.7,
    "The American Journal of Emergency Medicine": 2.7,
    "American journal of preventive medicine": 4.7,
    "American Journal of Preventive Medicine": 4.7,
    "Preventive medicine": 3.2,
    "Preventive Medicine": 3.2,
    "Preventive medicine reports": 2.8,
    "Preventive Medicine Reports": 2.8,
    "MMWR. Morbidity and mortality weekly report": 17.0,
    "MMWR. Morbidity and Mortality Weekly Report": 17.0,
    "Archives of academic emergency medicine": 2.8,
    "Archives of Academic Emergency Medicine": 2.8,
    "Journal of preventive medicine and public health": 2.9,
    "Journal of Preventive Medicine and Public Health": 2.9,
    "Resuscitation plus": 3.1,
    "Resuscitation Plus": 3.1,
}
DEFAULT_IMPACT_FACTOR = 1.5  # 목록에 없는 저널의 기본 IF (필요시 조정)

# WoS(Web of Science) 색인 등급. 확인된 저널만 채워뒀고, 나머지는 "미상"으로
# 표시됩니다 (Scopus 등재 여부는 별도 유료 데이터라 자동 판별하지 않습니다).
JOURNAL_INDEX = {
    "The New England Journal of Medicine": "SCIE",
    "N Engl J Med": "SCIE",
    "Lancet": "SCIE",
    "The Lancet": "SCIE",
    "JAMA": "SCIE",
    "JAMA internal medicine": "SCIE",
    "JAMA Internal Medicine": "SCIE",
    "JAMA Network Open": "SCIE",
    "BMJ": "SCIE",
    "BMJ (Clinical research ed.)": "SCIE",
    "Annals of internal medicine": "SCIE",
    "Annals of Internal Medicine": "SCIE",
    "Annals of emergency medicine": "SCIE",
    "Annals of Emergency Medicine": "SCIE",
    "Academic emergency medicine": "SCIE",
    "Academic Emergency Medicine": "SCIE",
    "Resuscitation": "SCIE",
    "American journal of preventive medicine": "SCIE",
    "American Journal of Preventive Medicine": "SCIE",
    "Preventive medicine": "SCIE",
    "Preventive Medicine": "SCIE",
    "Preventive medicine reports": "SCIE",
    "Preventive Medicine Reports": "SCIE",
    "Archives of academic emergency medicine": "ESCI",
    "Archives of Academic Emergency Medicine": "ESCI",
    "Journal of preventive medicine and public health": "ESCI",
    "Journal of Preventive Medicine and Public Health": "ESCI",
    "Resuscitation plus": "ESCI",
    "Resuscitation Plus": "ESCI",
}
DEFAULT_INDEX_TYPE = "미상"

# JCR(Journal Citation Reports) 사분위(Q1~Q4). Clarivate JCR 기준이며, 저널이
# 여러 카테고리에 속해 사분위가 다르면 논문 분야(예방의학/응급의학)와 더
# 관련 있는 카테고리 쪽을 기준으로 하나만 골라 넣었습니다. 확인된 저널만
# 채워뒀고 나머지는 "미상"입니다. JCR은 매년 6월 갱신되니, 정확한 최신
# 값은 https://clarivate.com/.../journal-citation-reports/ 에서 직접
# 확인해서 이 딕셔너리를 갱신하는 걸 추천합니다.
JOURNAL_QUARTILE = {
    "The New England Journal of Medicine": "Q1",
    "N Engl J Med": "Q1",
    "Lancet": "Q1",
    "The Lancet": "Q1",
    "JAMA": "Q1",
    "JAMA internal medicine": "Q1",
    "JAMA Internal Medicine": "Q1",
    "JAMA Network Open": "Q1",
    "BMJ": "Q1",
    "BMJ (Clinical research ed.)": "Q1",
    "Annals of internal medicine": "Q1",
    "Annals of Internal Medicine": "Q1",
    "Annals of emergency medicine": "Q1",
    "Annals of Emergency Medicine": "Q1",
    "Academic emergency medicine": "Q1",
    "Academic Emergency Medicine": "Q1",
    "Resuscitation": "Q1",
    "American journal of preventive medicine": "Q1",
    "American Journal of Preventive Medicine": "Q1",
    "Preventive medicine": "Q1",
    "Preventive Medicine": "Q1",
    "Preventive medicine reports": "Q2",
    "Preventive Medicine Reports": "Q2",
    "MMWR. Morbidity and mortality weekly report": "Q1",
    "MMWR. Morbidity and Mortality Weekly Report": "Q1",
    "Archives of academic emergency medicine": "Q2",
    "Archives of Academic Emergency Medicine": "Q2",
    "Journal of preventive medicine and public health": "Q2",
    "Journal of Preventive Medicine and Public Health": "Q2",
    "Resuscitation plus": "Q2",
    "Resuscitation Plus": "Q2",
}
DEFAULT_QUARTILE = "미상"

RELDATE_DAYS = 365 * 3  # 최근 3년

# --- 수집 범위 ---
# 예방의학/응급의학처럼 넓은 MeSH 검색어는 3년이면 수만 건이 나옵니다.
# 단순히 "최신순 상위 N건"만 가져오면, 기간을 아무리 늘려도 실제로는 최근
# 한두 달치만 긁히는 편향이 생깁니다 (예전에 6개월 창에서 겪었던 문제와
# 동일). 그래서 전체 기간을 BUCKET_COUNT개 구간으로 쪼개서 구간마다
# 고르게 수집합니다 — 3년 전 논문도, 최근 논문도 비교 대상에 들어오도록.
BUCKET_COUNT = 6  # 3년을 6구간(구간당 약 6개월)으로 분할
MAX_PER_BUCKET = 50  # 구간 하나당 수집할 논문 수 (분야당 최대 BUCKET_COUNT*MAX_PER_BUCKET건)

# --- 화면에 보여줄 범위 (진짜 "핫한 것만" 필터링) ---
# 위에서 모은 풀 중, 저널당/분야당 상위 몇 개만 실제로
# data/papers.json에 남길지를 정합니다. 점수 계산은 전체 풀을 기준으로 하되,
# 최종 출력은 이렇게 추려서 페이지가 "핫한 것만" 보이게 합니다.
TOP_PAPERS_PER_JOURNAL = 10  # 저널 하나당 최대 몇 편까지 보여줄지
TOP_JOURNALS_PER_FIELD = 25  # 분야 하나당 최대 몇 개 저널까지 보여줄지

REQUEST_DELAY = 0.34  # NCBI 무료 한도(초당 3회) 준수용 딜레이
ALTMETRIC_DELAY = 0.5  # Altmetric 무료 조회 한도 준수용 딜레이 (조금 더 여유있게)
TRANSLATE_DELAY = 0.4  # MyMemory 번역 API 호출 간 딜레이
CURRENT_ISSUE_WINDOW_DAYS = 45  # 저널의 "최신 호"로 간주할 여유 기간
CURRENT_ISSUE_BONUS = 10  # 100점 만점 기준 가산점
TOP_TOPICS_PER_FIELD = 12  # 분야별로 보여줄 핫토픽 키워드 개수

# 핫토픽 키워드 추출 시 제외할 일반 단어 (영어 논문 제목 기준)
STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "with", "and", "or",
    "vs", "versus", "by", "from", "into", "over", "under", "between", "among",
    "is", "are", "was", "were", "be", "being", "been", "as", "than", "that",
    "this", "these", "those", "it", "its", "their", "our", "we", "which", "who",
    "study", "studies", "trial", "trials", "randomized", "randomised", "controlled",
    "systematic", "review", "reviews", "meta-analysis", "analysis", "cohort",
    "retrospective", "prospective", "observational", "cross-sectional", "national",
    "multicenter", "multicentre", "single-center", "single-centre", "clinical",
    "patients", "patient", "results", "outcome", "outcomes", "effect", "effects",
    "effectiveness", "efficacy", "risk", "risks", "factors", "factor", "associated",
    "association", "associations", "impact", "role", "use", "using", "based",
    "among", "during", "after", "before", "following", "united", "states", "new",
    "case", "cases", "report", "reports", "data", "evidence", "care", "health",
    "medical", "medicine", "department", "department's", "hospital", "hospitals",
    "adult", "adults", "children", "child", "pediatric", "paediatric", "population",
    "rate", "rates", "level", "levels", "years", "year", "not", "more", "less",
    "high", "low", "comparison", "comparing", "compare", "approach", "practice",
    "program", "programme", "implementation", "intervention", "interventions",
    # 검색 쿼리 자체에 포함된, 분야명과 동어반복인 단어 (변별력이 없어 제외)
    "emergency", "department", "departments", "preventive", "prevention",
    "primary", "service", "services", "mass", "promotion",
}


def esearch_daterange(query: str, start, end, retmax: int) -> list[str]:
    params = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": retmax,
        "datetype": "pdat",
        "mindate": start.strftime("%Y/%m/%d"),
        "maxdate": end.strftime("%Y/%m/%d"),
        "sort": "most+recent",
    }
    r = requests.get(f"{EUTILS}/esearch.fcgi", params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("esearchresult", {}).get("idlist", [])


def esearch_windowed(query: str) -> list[str]:
    """
    전체 기간(RELDATE_DAYS)을 BUCKET_COUNT개 구간으로 나눠 구간마다
    최대 MAX_PER_BUCKET건씩 수집합니다. 한 번에 "최신순 N건"만 가져오면
    기간을 아무리 늘려도 최근 논문에만 쏠리기 때문입니다.
    """
    end = datetime.utcnow().date()
    start = end - timedelta(days=RELDATE_DAYS)
    bucket_days = RELDATE_DAYS // BUCKET_COUNT

    seen: set[str] = set()
    ordered_ids: list[str] = []
    cursor_end = end
    for i in range(BUCKET_COUNT):
        cursor_start = cursor_end - timedelta(days=bucket_days)
        if i == BUCKET_COUNT - 1:
            cursor_start = start  # 나눗셈 나머지는 마지막(가장 과거) 구간에 포함
        ids = esearch_daterange(query, cursor_start, cursor_end, MAX_PER_BUCKET)
        for pid in ids:
            if pid not in seen:
                seen.add(pid)
                ordered_ids.append(pid)
        time.sleep(REQUEST_DELAY)
        cursor_end = cursor_start
    return ordered_ids


def esummary(pmids: list[str]) -> dict:
    if not pmids:
        return {}
    out = {}
    for i in range(0, len(pmids), 200):
        chunk = pmids[i : i + 200]
        params = {"db": "pubmed", "id": ",".join(chunk), "retmode": "json"}
        r = requests.get(f"{EUTILS}/esummary.fcgi", params=params, timeout=30)
        r.raise_for_status()
        result = r.json().get("result", {})
        for pmid in chunk:
            if pmid in result:
                out[pmid] = result[pmid]
        time.sleep(REQUEST_DELAY)
    return out


def icite_lookup(pmids: list[str]) -> dict:
    out = {}
    for i in range(0, len(pmids), 200):
        chunk = pmids[i : i + 200]
        params = {"pmids": ",".join(chunk)}
        try:
            r = requests.get(ICITE, params=params, timeout=30)
            r.raise_for_status()
            for rec in r.json().get("data", []):
                out[str(rec.get("pmid"))] = rec
        except requests.RequestException:
            pass
        time.sleep(REQUEST_DELAY)
    return out


def altmetric_lookup(pmid: str) -> dict:
    try:
        r = requests.get(f"{ALTMETRIC}/{pmid}", timeout=15)
        if r.status_code == 200:
            return r.json()
    except requests.RequestException:
        pass
    return {}


def fetch_abstracts(pmids: list[str]) -> dict:
    """
    efetch(rettype=abstract, retmode=xml)로 초록 원문을 배치 조회합니다.
    esummary에는 초록이 없어서 별도 호출이 필요합니다. 200개씩 묶어서
    요청하므로, 이미 걸러진 논문에만 적용하면 호출 횟수가 적습니다.
    초록이 여러 섹션(BACKGROUND/METHODS/...)으로 나뉜 경우 라벨을 붙여
    이어 붙입니다. 초록이 없는 논문(사설/증례보고 등)은 빈 문자열입니다.
    """
    import xml.etree.ElementTree as ET

    out: dict[str, str] = {}
    for i in range(0, len(pmids), 200):
        chunk = pmids[i : i + 200]
        params = {
            "db": "pubmed",
            "id": ",".join(chunk),
            "rettype": "abstract",
            "retmode": "xml",
        }
        try:
            r = requests.get(f"{EUTILS}/efetch.fcgi", params=params, timeout=60)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            for article in root.findall(".//PubmedArticle"):
                pmid_el = article.find(".//MedlineCitation/PMID")
                if pmid_el is None or not pmid_el.text:
                    continue
                pmid = pmid_el.text.strip()
                parts = []
                for ab in article.findall(".//Abstract/AbstractText"):
                    label = ab.get("Label")
                    text = "".join(ab.itertext()).strip()
                    if not text:
                        continue
                    parts.append(f"{label}: {text}" if label else text)
                out[pmid] = " ".join(parts)
        except (requests.RequestException, ET.ParseError):
            pass
        time.sleep(REQUEST_DELAY)
    return out


def translate_title(text: str) -> str:
    """MyMemory 무료 번역 API로 영문 제목을 한글로 번역합니다. 실패 시 빈 문자열."""
    if not text:
        return ""
    params = {"q": text[:500], "langpair": "en|ko"}
    if MYMEMORY_EMAIL:
        params["de"] = MYMEMORY_EMAIL
    try:
        r = requests.get(MYMEMORY, params=params, timeout=15)
        if r.status_code == 200:
            data = r.json()
            translated = data.get("responseData", {}).get("translatedText", "")
            # 한도 초과 시 MyMemory가 안내 문구를 대신 돌려주는 경우가 있어 필터링
            if translated and "MYMEMORY WARNING" not in translated.upper():
                return translated
    except requests.RequestException:
        pass
    return ""


def normalize(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def parse_pubdate(summary_item: dict) -> str:
    raw = summary_item.get("sortpubdate", "") or summary_item.get("pubdate", "")
    return raw[:10].replace("/", "-") if raw else ""


def parse_sort_date(summary_item: dict):
    raw = summary_item.get("sortpubdate", "")
    if not raw:
        return None
    try:
        return datetime.strptime(raw[:10], "%Y/%m/%d")
    except ValueError:
        return None


def flag_current_issue(records: list[dict]) -> None:
    """
    같은 저널 내에서 volume/issue가 붙어 정식 발행된(= epub ahead-of-print가
    아닌) 논문들 중 가장 최근 날짜를 그 저널의 '현재 호' 기준일로 보고,
    그 날짜에서 CURRENT_ISSUE_WINDOW_DAYS 이내에 있는 논문에 플래그를 붙인다.
    """
    by_journal: dict[str, list[dict]] = {}
    for r in records:
        by_journal.setdefault(r["journal"], []).append(r)

    for journal, items in by_journal.items():
        issued = [r for r in items if r.get("_has_issue") and r.get("_sort_date")]
        if not issued:
            for r in items:
                r["is_current_issue"] = False
            continue
        latest = max(r["_sort_date"] for r in issued)
        cutoff = latest - timedelta(days=CURRENT_ISSUE_WINDOW_DAYS)
        for r in items:
            r["is_current_issue"] = bool(
                r.get("_has_issue") and r.get("_sort_date") and r["_sort_date"] >= cutoff
            )


def tokenize_title(title: str) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z\-]+", title.lower())
    return [w for w in words if len(w) >= 3 and w not in STOPWORDS]


def compute_hot_topics(records: list[dict]) -> list[dict]:
    """
    논문 제목에서 (불용어를 뺀) 연속된 두 단어 구절(bigram)을 뽑아, 그 구절이
    등장하는 논문들의 hot_score 합계가 큰 순으로 정렬합니다. bigram이 너무
    적게 나오면 단어(unigram) 상위 항목으로 보충합니다. 각 주제에는 매칭된
    논문의 pmid 목록도 함께 담아서, 화면에서 주제를 클릭하면 그 논문들만
    걸러 보여줄 수 있게 합니다.
    """
    bigram_score: Counter = Counter()
    bigram_count: Counter = Counter()
    bigram_pmids: dict[str, list[str]] = {}
    unigram_score: Counter = Counter()
    unigram_count: Counter = Counter()
    unigram_pmids: dict[str, list[str]] = {}

    for r in records:
        tokens = tokenize_title(r["title"])
        seen_bigrams = set()
        seen_unigrams = set()
        for i, tok in enumerate(tokens):
            if tok not in seen_unigrams:
                unigram_score[tok] += r["hot_score"]
                unigram_count[tok] += 1
                unigram_pmids.setdefault(tok, []).append(r["pmid"])
                seen_unigrams.add(tok)
            if i + 1 < len(tokens):
                phrase = f"{tok} {tokens[i + 1]}"
                if phrase not in seen_bigrams:
                    bigram_score[phrase] += r["hot_score"]
                    bigram_count[phrase] += 1
                    bigram_pmids.setdefault(phrase, []).append(r["pmid"])
                    seen_bigrams.add(phrase)

    topics = [
        {
            "phrase": phrase,
            "score": round(score, 1),
            "paper_count": bigram_count[phrase],
            "pmids": bigram_pmids[phrase],
        }
        for phrase, score in bigram_score.items()
        if bigram_count[phrase] >= 2  # 최소 2편 이상에서 함께 등장한 구절만
    ]
    topics.sort(key=lambda t: t["score"], reverse=True)
    topics = topics[:TOP_TOPICS_PER_FIELD]

    if len(topics) < TOP_TOPICS_PER_FIELD:
        used_words = set()
        for t in topics:
            used_words.update(t["phrase"].split(" "))
        extra = [
            {
                "phrase": w,
                "score": round(score, 1),
                "paper_count": unigram_count[w],
                "pmids": unigram_pmids[w],
            }
            for w, score in unigram_score.items()
            if w not in used_words and unigram_count[w] >= 2
        ]
        extra.sort(key=lambda t: t["score"], reverse=True)
        topics.extend(extra[: TOP_TOPICS_PER_FIELD - len(topics)])
        topics.sort(key=lambda t: t["score"], reverse=True)

    return topics


TOP_TREND_TOPICS = 6  # 연도별 트렌드 그래프에 표시할 주제(선) 개수


def get_title_phrases(title: str) -> set[str]:
    """제목에서 뽑을 수 있는 구절 집합 (단어 하나짜리 + 두 단어 구절 모두)."""
    tokens = tokenize_title(title)
    phrases = set(tokens)
    phrases.update(f"{tokens[i]} {tokens[i + 1]}" for i in range(len(tokens) - 1))
    return phrases


def compute_topic_trends(records: list[dict]) -> dict:
    """
    전체 수집 풀(화면에 안 보이는 논문까지 포함)을 기준으로, 연도별로
    어떤 주제가 활발했는지 보여줄 데이터를 만듭니다. hot_topics(화면 표시용,
    걸러진 논문 기준)와는 별개로 더 넓은 풀에서 계산해 연도별 그림이
    왜곡되지 않게 합니다.
    """
    years = sorted({(r["pubdate"] or "")[:4] for r in records if r.get("pubdate")})
    years = [y for y in years if y.isdigit()]
    # 너무 옛날 잡음(날짜 파싱 오류 등)이 섞이지 않도록 최근 4개 연도만
    years = years[-4:]

    overall = compute_hot_topics(records)
    top_phrases = [t["phrase"] for t in overall[:TOP_TREND_TOPICS]]

    series = {p: {y: 0.0 for y in years} for p in top_phrases}
    for r in records:
        y = (r.get("pubdate") or "")[:4]
        if y not in years:
            continue
        phrases = get_title_phrases(r["title"])
        for p in top_phrases:
            if p in phrases:
                series[p][y] += r["hot_score"]

    return {
        "years": years,
        "series": [
            {"phrase": p, "scores": [round(series[p][y], 1) for y in years]}
            for p in top_phrases
        ],
    }


def collect_field(field_key: str, field_cfg: dict) -> list[dict]:
    print(f"[{field_cfg['label']}] PubMed 검색 중...")
    pmids = esearch_windowed(field_cfg["query"])
    print(f"[{field_cfg['label']}] {len(pmids)}건 발견, 메타데이터 수집 중...")

    summaries = esummary(pmids)
    icite = icite_lookup(pmids)

    records = []
    for pmid in pmids:
        s = summaries.get(pmid)
        if not s:
            continue
        title = s.get("title", "").strip()
        journal = s.get("fulljournalname") or s.get("source", "")
        pubdate = parse_pubdate(s)
        authors = [a.get("name", "") for a in s.get("authors", [])][:3]

        ic = icite.get(pmid, {})
        citation_count = ic.get("citation_count") or 0
        rcr = ic.get("relative_citation_ratio")

        volume = (s.get("volume") or "").strip()
        issue = (s.get("issue") or "").strip()
        pubstatus = (s.get("pubstatus") or "").lower()
        has_issue = bool(issue) and "ahead" not in pubstatus

        records.append(
            {
                "pmid": pmid,
                "title": title,
                "journal": journal,
                "impact_factor": IMPACT_FACTORS.get(journal, DEFAULT_IMPACT_FACTOR),
                "index_type": JOURNAL_INDEX.get(journal, DEFAULT_INDEX_TYPE),
                "quartile": JOURNAL_QUARTILE.get(journal, DEFAULT_QUARTILE),
                "pubdate": pubdate,
                "authors": authors,
                "citation_count": citation_count,
                "rcr": rcr,
                "volume": volume,
                "issue": issue,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "_has_issue": has_issue,
                "_sort_date": parse_sort_date(s),
            }
        )

    print(f"[{field_cfg['label']}] Altmetric 조회 중 (건당 요청, 시간이 걸립니다)...")
    for idx, rec in enumerate(records, 1):
        am = altmetric_lookup(rec["pmid"])
        rec["altmetric_score"] = am.get("score", 0) or 0
        rec["altmetric_news"] = am.get("cited_by_msm_count", 0) or 0
        rec["altmetric_social"] = (
            (am.get("cited_by_tweeters_count", 0) or 0)
            + (am.get("cited_by_posts_count", 0) or 0)
        )
        if idx % 100 == 0:
            print(f"[{field_cfg['label']}]   ...{idx}/{len(records)}건 조회 완료")
        time.sleep(ALTMETRIC_DELAY)

    flag_current_issue(records)

    # --- 점수 계산 ---
    alt_norm = normalize([math.log1p(r["altmetric_score"]) for r in records])
    cite_norm = normalize([math.log1p(r["citation_count"]) for r in records])
    if_norm = normalize([math.log1p(r["impact_factor"]) for r in records])

    for rec, a_n, c_n, i_n in zip(records, alt_norm, cite_norm, if_norm):
        base = (0.5 * a_n + 0.3 * c_n + 0.2 * i_n) * 90  # 기본점 만점 90
        bonus = CURRENT_ISSUE_BONUS if rec["is_current_issue"] else 0
        rec["hot_score"] = round(base + bonus, 1)
        rec["field"] = field_key
        rec["field_label"] = field_cfg["label"]
        rec.pop("_has_issue", None)
        rec.pop("_sort_date", None)

    return records


def group_by_journal(records: list[dict]) -> list[dict]:
    """
    저널별로 묶고, 각 그룹 안에서 hot_score 내림차순 정렬 + top_paper 추출.
    진짜 '핫한 것만' 보이도록 저널당 TOP_PAPERS_PER_JOURNAL편, 분야당
    TOP_JOURNALS_PER_FIELD개 저널까지만 남깁니다 (점수 계산 자체는 이미
    전체 풀을 기준으로 끝난 뒤이므로, 이건 순수하게 화면에 보여줄 범위만
    추리는 단계입니다).
    """
    by_journal: dict[str, list[dict]] = {}
    for r in records:
        by_journal.setdefault(r["journal"], []).append(r)

    groups = []
    for journal, papers in by_journal.items():
        papers.sort(key=lambda r: r["hot_score"], reverse=True)
        total_found = len(papers)
        shown = papers[:TOP_PAPERS_PER_JOURNAL]
        groups.append(
            {
                "journal": journal,
                "impact_factor": shown[0]["impact_factor"],
                "index_type": shown[0]["index_type"],
                "quartile": shown[0]["quartile"],
                "paper_count": total_found,
                "top_paper": shown[0],
                "papers": shown,
            }
        )

    # 저널 정렬 기준: 그 저널의 가장 핫한 논문 점수가 높은 순
    groups.sort(key=lambda g: g["top_paper"]["hot_score"], reverse=True)
    return groups[:TOP_JOURNALS_PER_FIELD]


def enrich_survivors(fields_out: list[dict]) -> None:
    """
    저널당/분야당 상위로 걸러진(=화면에 실제로 보일) 논문들에만 초록과
    한글 제목 번역을 붙입니다. 전체 수집 풀이 아니라 걸러진 것에만
    적용해서 API 호출을 아낍니다.
    """
    survivors = []
    for f in fields_out:
        for g in f["journals"]:
            survivors.extend(g["papers"])

    if not survivors:
        return

    print(f"[번역/초록] 대상 {len(survivors)}건 초록 조회 중...")
    abstracts = fetch_abstracts([r["pmid"] for r in survivors])

    print(f"[번역/초록] 대상 {len(survivors)}건 제목 번역 중...")
    for idx, r in enumerate(survivors, 1):
        r["abstract"] = abstracts.get(r["pmid"], "")
        r["title_ko"] = translate_title(r["title"])
        if idx % 50 == 0:
            print(f"[번역/초록]   ...{idx}/{len(survivors)}건 완료")
        time.sleep(TRANSLATE_DELAY)


def main():
    fields_out = []
    for key, cfg in FIELDS.items():
        records = collect_field(key, cfg)
        journals = group_by_journal(records)
        survivors = [p for g in journals for p in g["papers"]]
        fields_out.append(
            {
                "key": key,
                "label": cfg["label"],
                "paper_count": len(records),
                "journals": journals,
                # 핫토픽은 (전체 풀이 아니라) 화면에 실제로 보일 논문들만
                # 대상으로 계산해서, 주제를 클릭했을 때 나오는 pmid가 항상
                # 화면에 있는 논문과 매칭되도록 합니다.
                "hot_topics": compute_hot_topics(survivors),
                # 연도별 트렌드는 걸러진 논문이 아니라 수집한 전체 풀
                # 기준으로 계산합니다 (더 대표성 있는 그림을 위해).
                "topic_trends": compute_topic_trends(records),
            }
        )

    enrich_survivors(fields_out)

    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "window_days": RELDATE_DAYS,
        "fields": fields_out,
    }

    with open("data/papers.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    total = sum(f["paper_count"] for f in fields_out)
    print(f"완료: 총 {total}건을 data/papers.json 에 저장했습니다.")


if __name__ == "__main__":
    main()
