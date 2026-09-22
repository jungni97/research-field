#!/usr/bin/env python3
"""
paper-radar/collect.py

최근 6개월 이내 예방의학(Preventive Medicine) / 응급의학(Emergency Medicine) 논문을
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

RELDATE_DAYS = 182  # 약 6개월

# --- 수집 범위 ---
# 예방의학/응급의학처럼 넓은 MeSH 검색어는 6개월 안에도 수천 건이 나옵니다.
# 이 값이 너무 작으면 "최신순 상위 N건"만 가져오게 되어, 애초에 인용·주목도가
# 쌓일 시간조차 없었던 방금 나온 논문들로만 채워지는 편향이 생깁니다.
# (정규화도 이 N건 안에서만 이뤄지므로 "핫함" 비교 자체가 왜곡됩니다.)
# 값을 올릴수록 정확도는 올라가지만 Altmetric 조회 시간이 늘어납니다.
MAX_PER_FIELD = 600  # 분야별로 실제 수집(=점수 계산 대상)할 논문 수

# --- 화면에 보여줄 범위 (진짜 "핫한 것만" 필터링) ---
# 위에서 모은 MAX_PER_FIELD건 중, 저널당/분야당 상위 몇 개만 실제로
# data/papers.json에 남길지를 정합니다. 점수 계산은 전체 풀을 기준으로 하되,
# 최종 출력은 이렇게 추려서 페이지가 "핫한 것만" 보이게 합니다.
TOP_PAPERS_PER_JOURNAL = 10  # 저널 하나당 최대 몇 편까지 보여줄지
TOP_JOURNALS_PER_FIELD = 25  # 분야 하나당 최대 몇 개 저널까지 보여줄지

REQUEST_DELAY = 0.34  # NCBI 무료 한도(초당 3회) 준수용 딜레이
ALTMETRIC_DELAY = 0.5  # Altmetric 무료 조회 한도 준수용 딜레이 (조금 더 여유있게)
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


def esearch(query: str, retmax: int) -> list[str]:
    params = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": retmax,
        "datetype": "pdat",
        "reldate": RELDATE_DAYS,
        "sort": "most+recent",
    }
    r = requests.get(f"{EUTILS}/esearch.fcgi", params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("esearchresult", {}).get("idlist", [])


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
    적게 나오면 단어(unigram) 상위 항목으로 보충합니다.
    """
    bigram_score: Counter = Counter()
    bigram_count: Counter = Counter()
    unigram_score: Counter = Counter()
    unigram_count: Counter = Counter()

    for r in records:
        tokens = tokenize_title(r["title"])
        seen_bigrams = set()
        seen_unigrams = set()
        for i, tok in enumerate(tokens):
            if tok not in seen_unigrams:
                unigram_score[tok] += r["hot_score"]
                unigram_count[tok] += 1
                seen_unigrams.add(tok)
            if i + 1 < len(tokens):
                phrase = f"{tok} {tokens[i + 1]}"
                if phrase not in seen_bigrams:
                    bigram_score[phrase] += r["hot_score"]
                    bigram_count[phrase] += 1
                    seen_bigrams.add(phrase)

    topics = [
        {"phrase": phrase, "score": round(score, 1), "paper_count": bigram_count[phrase]}
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
            {"phrase": w, "score": round(score, 1), "paper_count": unigram_count[w]}
            for w, score in unigram_score.items()
            if w not in used_words and unigram_count[w] >= 2
        ]
        extra.sort(key=lambda t: t["score"], reverse=True)
        topics.extend(extra[: TOP_TOPICS_PER_FIELD - len(topics)])
        topics.sort(key=lambda t: t["score"], reverse=True)

    return topics


def collect_field(field_key: str, field_cfg: dict) -> list[dict]:
    print(f"[{field_cfg['label']}] PubMed 검색 중...")
    pmids = esearch(field_cfg["query"], MAX_PER_FIELD)
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
                "paper_count": total_found,
                "top_paper": shown[0],
                "papers": shown,
            }
        )

    # 저널 정렬 기준: 그 저널의 가장 핫한 논문 점수가 높은 순
    groups.sort(key=lambda g: g["top_paper"]["hot_score"], reverse=True)
    return groups[:TOP_JOURNALS_PER_FIELD]


def main():
    fields_out = []
    for key, cfg in FIELDS.items():
        records = collect_field(key, cfg)
        fields_out.append(
            {
                "key": key,
                "label": cfg["label"],
                "paper_count": len(records),
                "journals": group_by_journal(records),
                "hot_topics": compute_hot_topics(records),
            }
        )

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
