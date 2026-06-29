"""통합 랭킹 점수 산출 (순수 함수, I/O 없음).

후보(candidate)별 신호(news/datalab/google/daum)를 받아
- 후보 집합 내 0~1 정규화
- 가용 신호만으로 weight 재정규화
- penalty 차감
- 최종 score 기준 Top10 선택
한다.

설계 계약 (docs/news-ranking-plan.md):
- Daum 순서를 최종 rank로 그대로 쓰지 않는다. score 기준으로 재정렬한다.
- Naver News 신호가 최종 랭킹의 핵심(기본 weight 0.60).
- 소스 신호가 전부 결측이면 해당 신호 weight 0 → 남은 신호로 재정규화.
- 순수 함수 → 단위 테스트 용이. 외부 호출/DB write 없음.
"""
from typing import Dict, List, Optional

# 가중치 (사용자 승인값 2026-06-29)
WEIGHTS = {
    "news": 0.60,
    "datalab": 0.20,
    "google": 0.10,
    "daum": 0.10,
}

# News 내부 구성 가중 (recent_count / latest_freshness / domain_diversity / title_relevance)
NEWS_SUBWEIGHTS = {
    "recent_count": 0.40,
    "latest_freshness": 0.30,
    "domain_diversity": 0.15,
    "title_relevance": 0.15,
}

# penalty 계수 (초기 최소화: 2개만)
LOW_RELEVANCE_THRESHOLD = 0.15
LOW_RELEVANCE_PENALTY = 0.10
NOISE_PENALTY = 0.15

TOP_N = 10

# freshness 기준: 최근 N시간
RECENT_HOURS = 12


def _minmax_normalize(values: Dict[str, float]) -> Dict[str, float]:
    """후보별 raw 값 dict → 0~1 정규화. 전부 동일/단일이면 0.5 균등."""
    if not values:
        return {}
    nums = list(values.values())
    lo, hi = min(nums), max(nums)
    if hi - lo < 1e-12:
        # 전부 동일 → 변별력 없음, 0.5 균등 (None/0 구분은 호출부 책임)
        return {k: 0.5 for k in values}
    return {k: (v - lo) / (hi - lo) for k, v in values.items()}


def _news_subscore(news: Dict) -> Optional[float]:
    """단일 후보의 news 신호 dict → 0~1 합성 점수. 신호 없으면 None.

    news = {recent_count, latest_age_hours, domain_diversity, title_relevance}
    (정규화는 후보 집합 단위로 별도 수행하므로 여기선 원자 신호만 0~1로 환산)
    """
    if not news:
        return None
    # latest_freshness: 최근일수록 1, RECENT_HOURS 초과면 0으로 선형 감소
    age = news.get("latest_age_hours")
    if age is None:
        freshness = 0.0
    else:
        freshness = max(0.0, 1.0 - (age / RECENT_HOURS)) if RECENT_HOURS > 0 else 0.0
    # recent_count / domain_diversity 는 집합 정규화 대상이라 raw 보관
    return {
        "recent_count": float(news.get("recent_count", 0) or 0),
        "latest_freshness": float(freshness),
        "domain_diversity": float(news.get("domain_diversity", 0) or 0),
        "title_relevance": float(news.get("title_relevance", 0) or 0),
    }


def _is_noise(keyword: str) -> bool:
    kw = (keyword or "").strip()
    if len(kw) <= 1:
        return True
    if kw.isdigit():
        return True
    return False


def compute_scores(candidates: List[Dict], signals: Dict[str, Dict]) -> List[Dict]:
    """후보 리스트 + 신호맵 → score 포함 ranked 리스트(내림차순).

    candidates: [{keyword, sources:{daum:rank|None, danawa:rank|None, google:rank|None, aux:bool}}]
    signals: {
        "news":   {keyword: {recent_count, latest_age_hours, domain_diversity, title_relevance}},
        "datalab":{keyword: {recent_delta}},
        "google": {keyword: {rank|interest}},
        "daum":   {keyword: daum_rank},
    }
    반환: [{keyword, score, source_breakdown{news,datalab,google,daum}, rank_reason,
            used_signals(set로 안 주고 list), news_meta}] score 내림차순.
    """
    keywords = [c["keyword"] for c in candidates]
    if not keywords:
        return []

    # --- 가용 신호 판정 (소스 단위) ---
    news_map = signals.get("news") or {}
    datalab_map = signals.get("datalab") or {}
    google_map = signals.get("google") or {}
    daum_map = signals.get("daum") or {}

    available = {}
    if any(news_map.get(k) for k in keywords):
        available["news"] = WEIGHTS["news"]
    if any(datalab_map.get(k) for k in keywords):
        available["datalab"] = WEIGHTS["datalab"]
    if any(google_map.get(k) for k in keywords):
        available["google"] = WEIGHTS["google"]
    if any(daum_map.get(k) is not None for k in keywords):
        available["daum"] = WEIGHTS["daum"]

    total = sum(available.values())
    if total <= 0:
        return []
    renorm = {s: w / total for s, w in available.items()}

    # --- News 원자 신호 수집 후 집합 정규화 ---
    news_atoms = {k: _news_subscore(news_map.get(k)) for k in keywords}
    # recent_count, domain_diversity 는 후보 집합 min-max 정규화
    rc_raw = {k: a["recent_count"] for k, a in news_atoms.items() if a}
    dd_raw = {k: a["domain_diversity"] for k, a in news_atoms.items() if a}
    rc_norm = _minmax_normalize(rc_raw)
    dd_norm = _minmax_normalize(dd_raw)

    def news_norm(k):
        a = news_atoms.get(k)
        if not a:
            return 0.0
        return (
            NEWS_SUBWEIGHTS["recent_count"] * rc_norm.get(k, 0.0)
            + NEWS_SUBWEIGHTS["latest_freshness"] * a["latest_freshness"]
            + NEWS_SUBWEIGHTS["domain_diversity"] * dd_norm.get(k, 0.0)
            + NEWS_SUBWEIGHTS["title_relevance"] * a["title_relevance"]
        )

    # --- DataLab: recent_delta 집합 정규화 ---
    delta_raw = {}
    for k in keywords:
        d = datalab_map.get(k)
        if d and d.get("recent_delta") is not None:
            delta_raw[k] = float(d["recent_delta"])
    delta_norm = _minmax_normalize(delta_raw)

    # --- Google: rank(작을수록 좋음) 또는 interest ---
    g_raw = {}
    for k in keywords:
        g = google_map.get(k)
        if not g:
            continue
        if g.get("interest") is not None:
            g_raw[k] = float(g["interest"])
        elif g.get("rank") is not None:
            g_raw[k] = 1.0 / (1.0 + float(g["rank"]))  # rank 역수
    g_norm = _minmax_normalize(g_raw)

    # --- Daum: rank 역순 보정 ---
    d_raw = {}
    for k in keywords:
        r = daum_map.get(k)
        if r is not None:
            d_raw[k] = 1.0 / (1.0 + float(r))
    d_norm = _minmax_normalize(d_raw)

    ranked = []
    for c in candidates:
        k = c["keyword"]
        # News-required(키워드 단위): News 신호 없는 후보는 최종 랭킹에서 제외.
        #   datalab/daum 점수만으로 뉴스 없는 키워드가 Top10에 오르는 것을 막는다.
        #   (단 news 소스 자체가 unavailable인 경우는 위에서 이미 전체 처리됨)
        if "news" in available and not news_map.get(k):
            continue
        breakdown = {
            "news": round(news_norm(k), 4) if "news" in available else 0.0,
            "datalab": round(delta_norm.get(k, 0.0), 4) if "datalab" in available else 0.0,
            "google": round(g_norm.get(k, 0.0), 4) if "google" in available else 0.0,
            "daum": round(d_norm.get(k, 0.0), 4) if "daum" in available else 0.0,
        }
        score = sum(renorm.get(s, 0.0) * breakdown[s] for s in renorm)

        # penalty
        a = news_atoms.get(k)
        title_rel = a["title_relevance"] if a else 0.0
        if "news" in available and title_rel < LOW_RELEVANCE_THRESHOLD:
            score -= LOW_RELEVANCE_PENALTY
        if _is_noise(k):
            score -= NOISE_PENALTY
        score = max(0.0, score)

        ranked.append({
            "keyword": k,
            "score": round(score, 4),
            "source_breakdown": breakdown,
            "rank_reason": _build_rank_reason(breakdown, available),
            "news_meta": news_map.get(k) or {},
            "used_signals": list(renorm.keys()),
        })

    # score 내림차순, 동점 시 news breakdown 우선
    ranked.sort(key=lambda r: (r["score"], r["source_breakdown"]["news"]), reverse=True)
    return ranked


def _build_rank_reason(breakdown: Dict[str, float], available: Dict) -> str:
    """실제 기여한 신호만 사실대로 표기(과장 금지)."""
    parts = []
    if "news" in available and breakdown["news"] > 0:
        parts.append("최근 뉴스 다수")
    if "datalab" in available and breakdown["datalab"] > 0:
        parts.append("검색 관심 상승")
    if "google" in available and breakdown["google"] > 0:
        parts.append("구글 신호")
    if "daum" in available and breakdown["daum"] > 0:
        parts.append("실검 보정")
    return " + ".join(parts) if parts else "신호 부족"


def select_top(ranked: List[Dict], top_n: int = TOP_N) -> List[Dict]:
    return ranked[:top_n]
