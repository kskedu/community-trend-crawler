"""후보 수집/병합 + News 신호 산출.

설계 계약 (docs/news-ranking-plan.md §3, §4-2):
- 후보 pool = Daum seed + Danawa seed + Google(stub) + 경량 보조후보(뉴스 title 토큰).
- normalize/dedup 후 상한(기본 30)으로 자른다.
- News 신호(recent_count/latest_age_hours/domain_diversity/title_relevance)는
  normalizer 결과에서 파생 — 기사 본문 전문 저장 없음.
- 다양성 hard guard: Daum 단독 출처가 아닌 후보 수 < MIN_NON_DAUM_CANDIDATES 이면
  상위에서 upsert skip (이 모듈은 카운트만 제공).
"""
import logging
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from news.normalizer import normalize_article
from news.summarizer import _tokens  # 기존 토크나이저 재사용(신규 의존성 없음)

logger = logging.getLogger(__name__)

CANDIDATE_MAX = 30
AUX_SEED_TOP = 5        # 보조후보 추출에 쓸 daum 상위 키워드 수
AUX_MAX = 8             # 보조후보 최대 개수
RECENT_HOURS = 12       # News 최근성 기준
MIN_NON_DAUM_CANDIDATES = 4


def _norm_key(keyword: str) -> str:
    return (keyword or "").strip().lower()


def _merge(pool: Dict[str, dict], keyword: str, source: str, rank: Optional[int]):
    """후보 pool에 keyword를 source/rank와 함께 병합."""
    kw = (keyword or "").strip()
    if not kw:
        return
    key = _norm_key(kw)
    if key not in pool:
        pool[key] = {"keyword": kw, "sources": {}}
    if rank is not None:
        # 더 좋은(작은) rank 보존
        cur = pool[key]["sources"].get(source)
        if cur is None or rank < cur:
            pool[key]["sources"][source] = rank
    else:
        pool[key]["sources"].setdefault(source, True)


def collect_candidates(
    daum_ranked: List[dict],
    danawa_ranked: List[dict],
    google_candidates: List[dict],
    aux_keywords: List[str],
    limit: int = CANDIDATE_MAX,
) -> List[dict]:
    """여러 소스 후보를 병합/dedup → [{keyword, sources:{...}}] (상한 적용)."""
    pool: Dict[str, dict] = {}
    for item in daum_ranked or []:
        _merge(pool, item.get("keyword"), "daum", item.get("rank"))
    for item in danawa_ranked or []:
        _merge(pool, item.get("keyword"), "danawa", item.get("rank"))
    for item in google_candidates or []:
        _merge(pool, item.get("keyword"), "google", item.get("rank"))
    for kw in aux_keywords or []:
        _merge(pool, kw, "aux", None)

    candidates = list(pool.values())
    # daum rank 우선 정렬(후보 안정성). 최종 순위는 ranker가 결정.
    candidates.sort(key=lambda c: c["sources"].get("daum", 9999))
    return candidates[:limit]


# Daum 파생/종속 소스 — 다양성 카운트에서 제외.
#   aux 는 Daum 상위 키워드의 뉴스 title 토큰에서 파생되므로 독립 소스가 아니다.
_DAUM_DEPENDENT_SOURCES = {"daum", "aux"}


def count_non_daum(candidates: List[dict]) -> int:
    """독립 소스(danawa/google 등)에서 온 후보 수(다양성 hard guard용).

    Daum 및 Daum 파생(aux)만 가진 후보는 세지 않는다 → 진짜 독립 후보만 카운트.
    """
    n = 0
    for c in candidates:
        srcs = set(c["sources"].keys())
        if srcs - _DAUM_DEPENDENT_SOURCES:
            n += 1
    return n


def derive_aux_keywords(
    daum_ranked: List[dict],
    fetch_news: Callable[[str], List[dict]],
    top: int = AUX_SEED_TOP,
    aux_max: int = AUX_MAX,
) -> List[str]:
    """daum 상위 키워드의 뉴스 title 빈출 토큰에서 보조후보 추출(경량, NLP 의존 없음)."""
    seed_kws = {(_norm_key(i.get("keyword"))) for i in (daum_ranked or [])}
    freq: Dict[str, int] = {}
    for item in (daum_ranked or [])[:top]:
        kw = item.get("keyword")
        if not kw:
            continue
        for raw in fetch_news(kw) or []:
            art = normalize_article(raw)
            if not art:
                continue
            for tok in set(_tokens(art.get("title", ""))):
                if len(tok) >= 2 and _norm_key(tok) not in seed_kws:
                    freq[tok] = freq.get(tok, 0) + 1
    # 2회 이상 등장한 토큰만 후보로(노이즈 억제)
    ranked = sorted([t for t, c in freq.items() if c >= 2], key=lambda t: freq[t], reverse=True)
    return ranked[:aux_max]


def _age_hours(published_at: Optional[str]) -> Optional[float]:
    if not published_at:
        return None
    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    age = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    return age if age >= 0 else None


def compute_news_signal(keyword: str, raw_items: List[dict]) -> Optional[dict]:
    """키워드별 News 신호 산출(normalizer 파생). 유효 기사 없으면 None.

    반환: {recent_count, latest_age_hours, domain_diversity, title_relevance, articles}
    (articles는 후속 build 단계 재사용용 normalized 리스트. 본문 전문 미포함)
    """
    normalized = []
    for raw in raw_items or []:
        art = normalize_article(raw)
        if art:
            normalized.append(art)
    if not normalized:
        return None

    kw_low = (keyword or "").lower()
    domains = set()
    recent_count = 0
    ages = []
    rel_hits = 0
    for a in normalized:
        url = a.get("url") or ""
        # 도메인
        try:
            from urllib.parse import urlparse
            host = urlparse(url).netloc.lower()
            if host:
                domains.add(host)
        except Exception:
            pass
        age = _age_hours(a.get("published_at"))
        if age is not None:
            ages.append(age)
            if age <= RECENT_HOURS:
                recent_count += 1
        text = f"{a.get('title','')} {a.get('snippet','')}".lower()
        if kw_low and kw_low in text:
            rel_hits += 1

    return {
        "recent_count": recent_count,
        "latest_age_hours": min(ages) if ages else None,
        "domain_diversity": len(domains),
        "title_relevance": rel_hits / len(normalized) if normalized else 0.0,
        "articles": normalized,
    }


def build_news_signals(
    candidates: List[dict],
    fetch_news: Callable[[str], List[dict]],
) -> Dict[str, dict]:
    """후보별 News 신호맵 + normalized articles 보관."""
    out = {}
    for c in candidates:
        kw = c["keyword"]
        sig = compute_news_signal(kw, fetch_news(kw))
        if sig:
            out[kw] = sig
    return out
