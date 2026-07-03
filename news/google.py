"""Google Trends 한국 실시간 인기 provider (공식 RSS export 기반).

설계 계약:
- 공식/허용 경로만 사용한다. UI 크롤링/차단 우회 없음 — Google Trends가 공식 제공하는
  RSS export(trends.google.com ... /rss?geo=KR)만 호출한다.
- 기본 비활성. GOOGLE_TRENDS_ENABLED=true AND GOOGLE_TRENDS_PROVIDER=rss 일 때만 외부 HTTP 호출.
  둘 중 하나라도 아니면 외부 호출 0(후보/신호 skip).
- 실패(네트워크/파싱/응답이상)는 전체 pipeline을 죽이지 않는다 → 빈 결과 + google_fetch_failed 로그.

인터페이스:
- fetch_candidates() -> [{keyword, rank, volume_bucket, started_at, active, related_news, related_terms}]
    실시간 인기 keyword 후보 공급(최대 CANDIDATE_MAX). 없는 필드는 생략(있으면 보존).
- fetch_signals(keywords) -> {keyword: {interest(0~1), volume_bucket, active}}
    후보의 Google Trends presence/volume/active를 0~1 demand 신호로 환산.
"""
import logging
import os
import re
from typing import Dict, List, Optional
from xml.etree import ElementTree

import requests

logger = logging.getLogger(__name__)

ENABLED_ENV = "GOOGLE_TRENDS_ENABLED"
PROVIDER_ENV = "GOOGLE_TRENDS_PROVIDER"

GEO = "KR"
CANDIDATE_MAX = 20
REQUEST_TIMEOUT = 10

# 공식 Google Trends daily RSS export. ht:approx_traffic / ht:news_item / pubDate 포함.
RSS_URL = f"https://trends.google.com/trends/trendingsearches/daily/rss?geo={GEO}"
# Google Hot Trends 네임스페이스
_HT_NS = "https://trends.google.com/trends/trendingsearches/daily"

# 프로세스 1회성 cron 실행 기준 — fetch_candidates()/fetch_signals()가 각각 호출돼도
# RSS를 한 번만 fetch하도록 파싱 결과를 프로세스 수명 동안 메모이즈한다(쿼터 보호).
_cache: Optional[List[dict]] = None
_cache_loaded = False


def is_enabled() -> bool:
    """provider 활성 여부. GOOGLE_TRENDS_ENABLED=true AND GOOGLE_TRENDS_PROVIDER=rss 일 때만 True."""
    enabled = os.environ.get(ENABLED_ENV, "").lower() in ("1", "true", "yes")
    provider = os.environ.get(PROVIDER_ENV, "").lower()
    return enabled and provider == "rss"


def _parse_traffic(text: Optional[str]) -> Optional[int]:
    """'2,000,000+' 같은 approx_traffic 문자열 → 정수(숫자만). 파싱 실패 시 None."""
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _volume_score(volume_int: Optional[int]) -> float:
    """approx_traffic 정수 → 0~1 volume score(log 스케일, 100만+를 상한 근사로)."""
    if not volume_int or volume_int <= 0:
        return 0.0
    import math
    # 1,000 → ~0.3, 100,000 → ~0.83, 1,000,000+ → 1.0 근사
    return min(1.0, math.log10(volume_int) / 6.0)


def _tag(elem, name: str) -> Optional[str]:
    child = elem.find(name)
    if child is None:
        return None
    text = (child.text or "").strip()
    return text or None


def _fetch_trends() -> List[dict]:
    """RSS를 fetch/파싱해 후보 리스트 반환. 실패/파싱이상은 [] + WARNING(pipeline 안 죽임)."""
    try:
        resp = requests.get(RSS_URL, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        root = ElementTree.fromstring(resp.content)
    except Exception as e:
        logger.warning("[news] google unavailable(RSS fetch/parse 실패) → google_fetch_failed: %s", e)
        return []

    items = root.findall(".//item")
    out: List[dict] = []
    for idx, item in enumerate(items[:CANDIDATE_MAX]):
        keyword = _tag(item, "title")
        if not keyword:
            continue
        cand: dict = {"keyword": keyword, "rank": idx + 1, "active": True}

        approx = _tag(item, f"{{{_HT_NS}}}approx_traffic")
        if approx:
            cand["volume_bucket"] = approx  # 원문 보존
        started_at = _tag(item, "pubDate")
        if started_at:
            cand["started_at"] = started_at

        related_news = []
        for ni in item.findall(f"{{{_HT_NS}}}news_item"):
            n_title = _tag(ni, f"{{{_HT_NS}}}news_item_title")
            n_url = _tag(ni, f"{{{_HT_NS}}}news_item_url")
            if n_title or n_url:
                entry = {}
                if n_title:
                    entry["title"] = n_title
                if n_url:
                    entry["url"] = n_url
                related_news.append(entry)
        if related_news:
            cand["related_news"] = related_news

        out.append(cand)
    return out


def _get_trends_cached() -> List[dict]:
    """프로세스 1회 fetch. 비활성이면 빈 리스트(외부호출 0)."""
    global _cache, _cache_loaded
    if not is_enabled():
        logger.warning("[news] Google Trends provider 비활성(GOOGLE_TRENDS_ENABLED/PROVIDER) → skip")
        return []
    if _cache_loaded:
        return _cache or []
    _cache = _fetch_trends()
    _cache_loaded = True
    return _cache or []


def fetch_candidates() -> List[dict]:
    """Google Trends 한국 실시간 인기 후보(최대 CANDIDATE_MAX). 비활성/실패 시 []."""
    trends = _get_trends_cached()
    if not trends:
        return []
    logger.info("[news] Google Trends 후보 %d개 수집", len(trends))
    return trends


def _demand_interest(cand: dict) -> float:
    """단일 trend 후보 → 0~1 demand interest. rank 위치 + volume + active 결합."""
    rank = cand.get("rank")
    rank_score = 1.0 / (1.0 + float(rank)) if rank else 0.0
    vol_score = _volume_score(_parse_traffic(cand.get("volume_bucket")))
    interest = max(rank_score, vol_score)
    if not cand.get("active", True):
        interest *= 0.5
    return round(min(1.0, interest), 4)


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def fetch_signals(keywords: List[str]) -> Dict[str, dict]:
    """후보 키워드 중 Google Trends에 존재하는 것만 demand 신호 반환.

    반환: {keyword: {interest(0~1), volume_bucket, active}}. 비활성/실패/미매칭 → 빈 dict/생략.
    keywords 인자의 표기를 그대로 key로 사용(candidate pool과 일치하도록 정규화 매칭).
    """
    trends = _get_trends_cached()
    if not trends:
        return {}
    by_norm = {_norm(c["keyword"]): c for c in trends}
    out: Dict[str, dict] = {}
    for kw in keywords or []:
        cand = by_norm.get(_norm(kw))
        if not cand:
            continue
        sig = {"interest": _demand_interest(cand), "active": bool(cand.get("active", True))}
        if cand.get("volume_bucket"):
            sig["volume_bucket"] = cand["volume_bucket"]
        out[kw] = sig
    return out


def reset_cache() -> None:
    """테스트/재실행용 캐시 초기화."""
    global _cache, _cache_loaded
    _cache = None
    _cache_loaded = False
