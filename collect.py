#!/usr/bin/env python3
"""
paper-radar/collect.py

최근 6개월 이내 예방의학(Preventive Medicine) / 응급의학(Emergency Medicine) 논문을
PubMed에서 수집하고, 여러 지표를 정규화·합산해 "핫함 점수(hot_score)"를 매겨
data/papers.json 으로 저장합니다.

지표 구성 (의학 논문은 출간 직후 인용수가 거의 안 쌓이므로, 인용 지표보다
"주목도" 지표의 비중을 더 크게 둡니다):
  - altmetric      (기본점의 50%) : 언론·SNS·블로그 등에서의 언급도
  - citations      (기본점의 30%) : 현재까지의 피인용수 (iCite 기준, RCR 병기)
  - journal        (기본점의 20%) : 저널 등급 가중치 (주요 저널일수록 가산점)
  - current_issue  (가산점 +10)   : 그 저널의 "가장 최근 정식 호(issue)"에 배정된
                                    논문이면 가산점. 저널이 이미 편집 단계에서
                                    골라 실은 논문이라 별도의 강한 신호로 취급.

필요 패키지: requests  (pip install requests)
실행: python collect.py
출력: data/papers.json
"""

import json
import math
import time
import urllib.parse
from datetime import datetime, timedelta

import requests

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
ICITE = "https://icite.od.nih.gov/api/pubs"
ALTMETRIC = "https://api.altmetric.com/v1/pmid"

# 검색할 두 분야와 PubMed 쿼리 (MeSH 기반 + 자유어 보강)
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

# 주요 저널 가중치 (0~1). 목록에 없으면 기본값(0.3) 적용.
JOURNAL_WEIGHTS = {
    "The New England Journal of Medicine": 1.0,
    "N Engl J Med": 1.0,
    "Lancet": 1.0,
    "The Lancet": 1.0,
    "JAMA": 0.95,
    "JAMA Internal Medicine": 0.85,
    "JAMA Network Open": 0.6,
    "BMJ": 0.9,
    "Annals of Internal Medicine": 0.8,
    "Annals of Emergency Medicine": 0.85,
    "Academic Emergency Medicine": 0.7,
    "The American Journal of Emergency Medicine": 0.5,
    "Resuscitation": 0.7,
    "American Journal of Preventive Medicine": 0.7,
    "Preventive Medicine": 0.55,
    "Preventive Medicine Reports": 0.4,
    "MMWR. Morbidity and Mortality Weekly Report": 0.75,
}

RELDATE_DAYS = 182  # 약 6개월
MAX_PER_FIELD = 150  # 분야별 최대 수집 논문 수
REQUEST_DELAY = 0.34  # NCBI 무료 한도(초당 3회) 준수용 딜레이


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


def parse_sort_date(summary_item: dict) -> datetime | None:
    raw = summary_item.get("sortpubdate", "")
    if not raw:
        return None
    try:
        return datetime.strptime(raw[:10], "%Y/%m/%d")
    except ValueError:
        return None


CURRENT_ISSUE_WINDOW_DAYS = 45  # 저널의 "최신 호"로 간주할 여유 기간


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
        # "epubahead"/"aheadofprint" 상태거나 issue가 비어 있으면 아직 정식 호에
        # 배정되지 않은 것으로 취급.
        has_issue = bool(issue) and "ahead" not in pubstatus

        records.append(
            {
                "pmid": pmid,
                "title": title,
                "journal": journal,
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
    for rec in records:
        am = altmetric_lookup(rec["pmid"])
        rec["altmetric_score"] = am.get("score", 0) or 0
        rec["altmetric_news"] = am.get("cited_by_msm_count", 0) or 0
        rec["altmetric_social"] = (
            (am.get("cited_by_tweeters_count", 0) or 0)
            + (am.get("cited_by_posts_count", 0) or 0)
        )
        time.sleep(REQUEST_DELAY)

    # 저널별 "현재 호" 여부 플래그
    flag_current_issue(records)

    # --- 점수 계산 ---
    alt_scores = [math.log1p(r["altmetric_score"]) for r in records]
    cite_scores = [math.log1p(r["citation_count"]) for r in records]
    alt_norm = normalize(alt_scores)
    cite_norm = normalize(cite_scores)

    CURRENT_ISSUE_BONUS = 10  # 100점 만점 기준 가산점

    for rec, a_n, c_n in zip(records, alt_norm, cite_norm):
        j_weight = JOURNAL_WEIGHTS.get(rec["journal"], 0.3)
        base = (0.5 * a_n + 0.3 * c_n + 0.2 * j_weight) * 90  # 기본점 만점 90
        bonus = CURRENT_ISSUE_BONUS if rec["is_current_issue"] else 0
        rec["hot_score"] = round(base + bonus, 1)
        rec["field"] = field_key
        rec["field_label"] = field_cfg["label"]
        # 내부용 임시 필드 정리
        rec.pop("_has_issue", None)
        rec.pop("_sort_date", None)

    records.sort(key=lambda r: r["hot_score"], reverse=True)
    return records


def main():
    all_records = []
    for key, cfg in FIELDS.items():
        all_records.extend(collect_field(key, cfg))

    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "window_days": RELDATE_DAYS,
        "papers": all_records,
    }

    with open("data/papers.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"완료: 총 {len(all_records)}건을 data/papers.json 에 저장했습니다.")


if __name__ == "__main__":
    main()
