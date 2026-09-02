"""통합 랭킹 점수 산출 (순수 함수, I/O 없음).

후보(candidate)별 신호(news/datalab/google/daum)를 받아
- 후보 집합 내 0~1 정규화
- 가용 신호만으로 weight 재정규화
- penalty 차감
- 유사 키워드 dedupe + same-issue merge (score 계산 후, Top10 확정 전)
- 최종 score 기준 Top10 선택
한다.

설계 계약 (docs/news-ranking-plan.md):
- Daum 순서를 최종 rank로 그대로 쓰지 않는다. score 기준으로 재정렬한다.
- Naver News 신호가 최종 랭킹의 핵심(기본 weight 0.60).
- 소스 신호가 전부 결측이면 해당 신호 weight 0 → 남은 신호로 재정규화.
- 순수 함수 → 단위 테스트 용이. 외부 호출/DB write 없음.

품질 개선(dedupe/merge, docs/news-ranking-quality-plan.md §7):
- dedupe/same-issue merge는 select_top() 이전, compute_scores() 이후에 적용한다
  (score까지 계산된 뒤에야 "더 높은 score의 대표"를 고를 수 있음).
- keyword(canonical, movement 비교용)는 merge/dedupe 후에도 안정적으로 유지하고,
  사건 맥락을 담은 조합형 표기는 display_keyword에만 넣는다(§7-1).
- backfill은 selected-set(이미 처리된 keyword/alias)을 누적하며 ranked 전체를
  순회하는 단일 루프로 처리해 재중복을 막는다(§7-2).
- merge된 item은 원본 후보의 sources를 그대로 실어 builder의 candidate_map lookup
  실패를 방어한다(§7-3).
"""
import logging
from typing import Callable, Dict, List, Optional

from news.candidates import _INDEPENDENT_SEARCH_FAMILIES
from news.normalizer import GENERIC_NEWS_SECTION_LABELS, title_evidence_text

logger = logging.getLogger(__name__)

# ── 유사 키워드 dedupe 설정 ──
_DEDUPE_SUFFIXES = ("고등학교", "중학교", "초등학교", "대학교")
_DEDUPE_SUFFIX_ALIASES = {
    "고등학교": "고", "중학교": "중", "초등학교": "초", "대학교": "대",
}
# substring containment 판정에서 제외할 만큼 "넓은"(일반적인) 단독 키워드.
# 이 목록에 있는 키워드는 다른 키워드의 substring이어도 자동 merge하지 않는다.
_TOO_BROAD_SINGLE_WORDS = {
    "독일", "사원", "기흥", "한국", "미국", "중국", "일본", "정부", "회사",
}
DEDUPE_TOKEN_JACCARD_THRESHOLD = 0.6

# ── same-issue merge 설정 ──
MERGE_ARTICLE_OVERLAP_THRESHOLD = 0.5
# article 그룹 간 공유 사건 토큰(문서빈도>=2 근사) 기반 보조 merge 신호.
# article overlap(개별 기사 pairwise Jaccard)이 표현 차이로 낮게 나오는 same-issue 케이스를
# 보완한다. "겹치는 사건 토큰 최소 개수" + "keyword anchor 교차 등장" 조건을 모두 만족해야
# merge 근거로 인정한다(일반 서술어 1~2개 겹침으로 오탐 병합되는 것을 막기 위함).
REPRESENTATIVE_OVERLAP_MIN_SHARED_TOKENS = 2
# 서로 다른 URL로 신디케이트된 "사실상 동일한 기사"를 공유 근거로 인정하는 기준
# (2026-08-05 운영 진단). PR #17이 차단한 roundup bridge는 공유 URL 경로만 막았는데,
# 같은 roundup이 제휴/전재로 다른 URL을 달고 양쪽 검색결과에 잡히면 URL 교집합이 비어
# PR #17 가드가 통째로 우회됐다(_is_same_issue의 `if not shared_urls` 조기 반환).
# 판정은 **title only** — 운영 진단 스냅샷에 snippet이 없어 임계값 근거를 title로만
# 측정할 수 있고, 측정 필드와 판정 필드를 일치시켜야 기준이 흔들리지 않는다.
# (기존 merge 신호인 _pairwise_evidence_overlap의 title+snippet Jaccard는 건드리지 않음.)
_NEAR_DUPLICATE_TITLE_JACCARD = 0.9
# 짧은 제목("속보"/"오늘 날씨"류)이 우연히 일치해 공유 근거로 승격되는 것을 막는 하한.
# 운영 48h 기사 제목 5,649건 토큰수 mean 8.2 / p5 6 / 5토큰 미만 1.3%, 실제 cross-URL
# near-dup 히트 8건은 전부 8~9토큰이라 정상 신디케이트 탐지를 깎지 않는다.
_NEAR_DUPLICATE_MIN_TITLE_TOKENS = 5
DISPLAY_KEYWORD_MAX_LEN = 18

# 최상위 4축 가중치 (사용자 확정 2026-07-04)
#   news_evidence   : Naver News 근거(recent/도메인 다양성/title relevance)
#   search_demand   : 검색 수요(google trends → datalab → home rank 우선순위 coalesce)
#   source_consensus: 독립 홈/트렌드 source family 합의 수
#   freshness       : 최신 기사 신선도(top-level 승격)
WEIGHTS = {
    "news": 0.45,
    "search_demand": 0.30,
    "source_consensus": 0.15,
    "freshness": 0.10,
}

# News Evidence 내부 구성 가중 (freshness는 top-level 축으로 승격돼 제외).
NEWS_SUBWEIGHTS = {
    "recent_count": 0.60,
    "domain_diversity": 0.20,
    "title_relevance": 0.20,
}

# search_demand 내부 우선순위(coalesce 순서). 앞선 source의 신호가 있으면 그 값을 채택한다.
#   google trends volume/active > datalab recent_delta > naver_home rank > bing rank
#   > daum rank > nate rank. (naver_home은 현재 수집 소스가 없어 실제로는 건너뜀)
SEARCH_DEMAND_PRIORITY = (
    "google", "datalab", "naver_home", "bing_home", "daum_home", "nate_home",
)

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
    """단일 후보의 news 신호 dict → News Evidence 원자 신호. 신호 없으면 None.

    news = {recent_count, latest_age_hours, domain_diversity, title_relevance}
    (정규화는 후보 집합 단위로 별도 수행하므로 여기선 원자 신호만 보관.
     freshness는 top-level 축으로 승격돼 이 서브스코어에 포함하지 않는다.)
    """
    if not news:
        return None
    # recent_count / domain_diversity 는 집합 정규화 대상이라 raw 보관
    return {
        "recent_count": float(news.get("recent_count", 0) or 0),
        "domain_diversity": float(news.get("domain_diversity", 0) or 0),
        "title_relevance": float(news.get("title_relevance", 0) or 0),
    }


def _freshness_val(news: Dict) -> float:
    """단일 후보 news 신호 → 0~1 freshness(top-level 축). 최근일수록 1, RECENT_HOURS 초과 0."""
    if not news:
        return 0.0
    age = news.get("latest_age_hours")
    if age is None:
        return 0.0
    if RECENT_HOURS <= 0:
        return 0.0
    return max(0.0, 1.0 - (age / RECENT_HOURS))


def _is_noise(keyword: str) -> bool:
    kw = (keyword or "").strip()
    if len(kw) <= 1:
        return True
    if kw.isdigit():
        return True
    return False


# === 반복형/evergreen 콘텐츠(운세류) 제외(2026-07-05) ===
# "오늘의 운세"/"띠별 운세" 같은 콘텐츠는 매일 반복 발행되고 기사 수·freshness가 항상
# 높아 기존 quality/fresh gate를 그대로 통과한다 — 하지만 "실시간 이슈"가 아니라 정기
# 콘텐츠이므로 별도 신호(패턴 매칭)로 걸러야 한다. 날짜 접미사("-7월 5일", "2026년 7월 6일")가
# 붙어도 패턴 자체(부분 문자열)는 그대로 포함되므로 별도 날짜 정규식이 필요 없다.
#
# "사주"/"타로" 단독은 패턴에 넣지 않는다(Codex review-only P1, 2026-07-05): "청부 사주"/
# "언론사 사주"/"댓글 사주 의혹"처럼 사건성 키워드에도 등장하는 짧은 단독어라, 단독으로
# 넣으면 keyword 즉시매칭 경로에서 정상 이슈까지 운세로 오탐 제외된다. 운세 맥락이 명확한
# phrase("오늘의 사주"/"사주풀이"/"오늘의 타로"/"타로 운세")로만 좁혀 잡는다.
_HOROSCOPE_PATTERNS = (
    "오늘의 운세", "띠별 운세", "별자리 운세", "별자리별 운세",
    "주간 운세", "월간 운세", "꿈해몽", "로또운세",
    "오늘의 사주", "사주풀이", "무료 사주", "신년 사주", "사주 운세",
    "오늘의 타로", "타로 운세", "타로운세", "타로카드 운세",
)
# 기사 title 다수가 운세류일 때만 "기사 묶음 자체가 운세 콘텐츠"로 판단하는 비율 기준.
# 절반 이하(0.5 포함)는 "운세성 제목이 한두 건 섞인 일반 묶음"으로 보아 제외하지 않는다
# (Codex review-only P2, 2026-07-05: >=0.5는 2건 중 1건만 걸려도 제외되는 문제가 있어
# 과반 "초과"로 좁힘 — 최소 과반 이상이 운세 콘텐츠일 때만 반복 콘텐츠로 본다).
_HOROSCOPE_ARTICLE_RATIO = 0.5


def _is_horoscope_text(text: str) -> bool:
    t = text or ""
    return any(p in t for p in _HOROSCOPE_PATTERNS)


def _is_horoscope_candidate(keyword: str, news_meta: Dict) -> bool:
    """keyword/display 표기 자체가 운세 패턴을 포함하거나, 기사 title 과반이 운세류인지.

    - keyword 자체가 운세 패턴이면(예: "운세 오늘의 운세") 즉시 True(강한 신호).
    - 그렇지 않으면 articles(정규화·relevance 반영된 원본) title 중 운세 패턴 비율이
      _HOROSCOPE_ARTICLE_RATIO 초과일 때만 True — "운세"가 다른 주제 기사에 한두 건
      섞인 정도(정확히 절반 포함)로는 과도하게 제외하지 않는다.
    """
    if _is_horoscope_text(keyword):
        return True
    articles = news_meta.get("articles") or []
    if not articles:
        return False
    hits = sum(1 for a in articles if _is_horoscope_text(a.get("title")))
    return (hits / len(articles)) > _HOROSCOPE_ARTICLE_RATIO


def _quality_gate_reason(keyword: str, news_meta: Dict) -> Optional[str]:
    """keyword-level quality gate 위반 사유. 통과면 None.

    - 고관련 기사(candidates.HIGH_RELEVANCE_THRESHOLD 이상) 2건 미만 AND quality_cluster_size
      2 미만이면 low_quality_news(관련 기사가 사실상 없음).
    - 관련 기사는 있으나 전부 오래됨(FRESH_RELEVANCE_HOURS 이내 고관련 0건)이면 stale_only.
    - keyword/기사 다수가 오늘의 운세류 반복 콘텐츠면 horoscope_content(2026-07-05) —
      기사 수/freshness가 충분해도 실시간 이슈가 아니므로 위 두 조건보다 먼저 판정한다.
    - entity 키워드 cohesion(E, 2026-07): keyword_kind=='entity'인데 고관련 기사가 실제로
      같은 사건이 아니면(has_dominant_event=False AND same_event_burst=False) low_quality_news.
      단일 엔티티 토큰("한화")만 공유하고 야구/그룹/오션 등 서로 다른 사건인 기사 2건으로
      gate를 통과하던 문제를 막는다. event/unknown 키워드에는 적용하지 않는다(산불/폭우 등
      사건·현상 단독어와 다토큰 키워드의 정상 이슈 오탈락 방지 — 사용자 지시).
    """
    if _is_horoscope_candidate(keyword, news_meta):
        return "horoscope_content"
    # crime-attribution safety(G, 2026-07-21) — 이름+범죄어 직결 키워드가 실제 범죄
    # 주체를 이름 엔티티로 입증하지 못하면 fail-closed 로 제외한다("박나래 공갈미수
    # 구속"처럼 유명인을 범죄 주체로 오인시키는 명예·법적 위험 방어). crime keyword
    # 아니면 has_unsafe_crime_attribution 이 항상 False 라 비범죄 이슈에 무영향이고,
    # 본인 실제 사건(주체 입증)은 verified_self 경로로 통과한다. horoscope 다음, 기존
    # 저품질 판정보다 먼저 둔다 — 의미 왜곡·법적 위험이 품질 미달보다 상위 우려다.
    if news_meta.get("has_unsafe_crime_attribution"):
        return "unsafe_crime_attribution"
    hrc = news_meta.get("high_relevance_count", 0)
    qcs = news_meta.get("quality_cluster_size", 0)
    if not (hrc >= 2 or qcs >= 2):
        return "low_quality_news"
    if news_meta.get("fresh_high_relevance_count", 0) < 1:
        return "stale_only"
    # entity 전용 cohesion gate — 정제(candidates C) 후 신호 소비.
    if news_meta.get("keyword_kind") == "entity":
        if not (news_meta.get("has_dominant_event") or news_meta.get("same_event_burst")):
            return "low_quality_news"
    return None


def _rank_demand_norm(candidates: List[Dict], family: str) -> Dict[str, float]:
    """candidate.sources[family] rank → 역수 후 집합 min-max 정규화 map(search_demand용)."""
    raw = {}
    for c in candidates:
        r = (c.get("sources") or {}).get(family)
        # rank는 정수. aux/phrase의 True(bool)는 홈 family가 아니라 여기 대상이 아니다.
        if isinstance(r, bool):
            continue
        if isinstance(r, (int, float)):
            raw[c["keyword"]] = 1.0 / (1.0 + float(r))
    return _minmax_normalize(raw)


def compute_scores(candidates: List[Dict], signals: Dict[str, Dict]) -> List[Dict]:
    """후보 리스트 + 신호맵 → score 포함 ranked 리스트(내림차순).

    candidates: [{keyword, sources:{google_trends|daum_home|nate_home|bing_home: rank,
                  naver_news_aux|naver_news_phrase: True}}]
    signals: {
        "news":    {keyword: {recent_count, latest_age_hours, domain_diversity, title_relevance, ...}},
        "datalab": {keyword: {recent_delta}},
        "google":  {keyword: {interest, ...}},
    }
    (홈/트렌드 rank 기반 demand는 candidate.sources에서 직접 읽는다 — 별도 rank 신호맵 불필요)

    반환: [{keyword, score, source_breakdown{news,search_demand,source_consensus,freshness},
            rank_reason, used_signals(list), news_meta, sources}] score 내림차순.

    최상위 4축(WEIGHTS): News Evidence / Search Demand / Source Consensus / Freshness.
    가용 축만 weight 재정규화. penalty는 별도 차감.
    """
    keywords = [c["keyword"] for c in candidates]
    if not keywords:
        return []

    news_map = signals.get("news") or {}
    datalab_map = signals.get("datalab") or {}
    google_map = signals.get("google") or {}

    # news_available_before_gate를 quality gate 필터링 *이전*에 원본 keywords 기준으로 확정한다
    # (Codex review-only P1: quality gate로 news 있는 후보가 전부 걸러지면 available["news"]
    # 판정이 꺼져 news-required 최종 제외가 무력화되는 회귀 방지).
    news_available_before_gate = any(news_map.get(k) for k in keywords)

    # keyword-level quality gate: news 신호는 있으나 저품질/stale 이면 정규화 이전에 제외.
    gated = []
    for c in candidates:
        nm = news_map.get(c["keyword"])
        if nm is None:
            gated.append(c)  # news 없는 후보 → 아래 news-required에서 no_news_evidence 처리
            continue
        reason = _quality_gate_reason(c["keyword"], nm)
        if reason:
            logger.info("[news] drop %s: %s", c["keyword"], reason)
            continue
        gated.append(c)
    candidates = gated
    keywords = [c["keyword"] for c in candidates]
    if not keywords:
        return []

    # --- News 원자 신호 수집 후 집합 정규화 ---
    news_atoms = {k: _news_subscore(news_map.get(k)) for k in keywords}
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
            + NEWS_SUBWEIGHTS["domain_diversity"] * dd_norm.get(k, 0.0)
            + NEWS_SUBWEIGHTS["title_relevance"] * a["title_relevance"]
        )

    # --- Search Demand: 우선순위 coalesce용 source별 정규화 map ---
    g_raw = {}
    for k in keywords:
        g = google_map.get(k)
        if g and g.get("interest") is not None:
            g_raw[k] = float(g["interest"])
    delta_raw = {}
    for k in keywords:
        d = datalab_map.get(k)
        if d and d.get("recent_delta") is not None:
            delta_raw[k] = float(d["recent_delta"])
    demand_maps = {
        "google": _minmax_normalize(g_raw),
        "datalab": _minmax_normalize(delta_raw),
        "naver_home": {},  # 수집 소스 없음(예약)
        "bing_home": _rank_demand_norm(candidates, "bing_home"),
        "daum_home": _rank_demand_norm(candidates, "daum_home"),
        "nate_home": _rank_demand_norm(candidates, "nate_home"),
    }

    def search_demand(k):
        for src in SEARCH_DEMAND_PRIORITY:
            m = demand_maps.get(src) or {}
            if k in m:
                return m[k]
        return 0.0

    demand_available = any(
        any(k in (demand_maps.get(s) or {}) for s in SEARCH_DEMAND_PRIORITY)
        for k in keywords
    )

    # --- Source Consensus: 독립 홈/트렌드 family 종수 정규화 ---
    consensus_raw = {}
    for c in candidates:
        fams = set((c.get("sources") or {}).keys()) & _INDEPENDENT_SEARCH_FAMILIES
        if fams:
            consensus_raw[c["keyword"]] = float(len(fams))
    consensus_norm = _minmax_normalize(consensus_raw)
    consensus_available = bool(consensus_raw)

    # --- 가용 축 판정 + weight 재정규화 ---
    available = {}
    if news_available_before_gate:
        available["news"] = WEIGHTS["news"]
        available["freshness"] = WEIGHTS["freshness"]  # freshness는 news 근거에서 파생
    if demand_available:
        available["search_demand"] = WEIGHTS["search_demand"]
    if consensus_available:
        available["source_consensus"] = WEIGHTS["source_consensus"]

    total = sum(available.values())
    if total <= 0:
        return []
    renorm = {s: w / total for s, w in available.items()}

    ranked = []
    for c in candidates:
        k = c["keyword"]
        # News-required(키워드 단위): News 신호 없는 후보는 최종 랭킹에서 제외.
        if "news" in available and not news_map.get(k):
            logger.info("[news] drop %s: no_news_evidence", k)
            continue
        breakdown = {
            "news": round(news_norm(k), 4) if "news" in available else 0.0,
            "search_demand": round(search_demand(k), 4) if "search_demand" in available else 0.0,
            "source_consensus": round(consensus_norm.get(k, 0.0), 4) if "source_consensus" in available else 0.0,
            "freshness": round(_freshness_val(news_map.get(k)), 4) if "freshness" in available else 0.0,
        }
        score = sum(renorm.get(s, 0.0) * breakdown[s] for s in renorm)

        # penalty (별도 차감)
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
            # dedupe_and_merge()가 merge 후에도 candidate lookup 실패 없이 sources를
            # 실어 나를 수 있도록 원본 candidate의 sources를 보존한다(§7-3).
            "sources": c.get("sources") or {},
        })

    # score 내림차순, 동점 시 news breakdown 우선
    ranked.sort(key=lambda r: (r["score"], r["source_breakdown"]["news"]), reverse=True)
    return ranked


def _build_rank_reason(breakdown: Dict[str, float], available: Dict) -> str:
    """실제 기여한 신호만 사실대로 표기(과장 금지)."""
    parts = []
    if "news" in available and breakdown["news"] > 0:
        parts.append("최근 뉴스 다수")
    if "search_demand" in available and breakdown["search_demand"] > 0:
        parts.append("검색 수요")
    if "source_consensus" in available and breakdown["source_consensus"] > 0:
        parts.append("복수 소스 합의")
    if "freshness" in available and breakdown["freshness"] > 0:
        parts.append("최신 기사")
    return " + ".join(parts) if parts else "신호 부족"


def _norm_for_compare(keyword: str) -> str:
    """공백/특수문자 제거한 소문자 비교용 정규화 키."""
    return "".join(ch for ch in (keyword or "").lower() if ch.isalnum())


def _institution_alias_forms(keyword: str) -> set:
    """기관명 축약어 alias 집합 생성. 예: '배재고등학교' → {'배재고등학교', '배재고'}."""
    forms = {keyword}
    for suffix, short in _DEDUPE_SUFFIX_ALIASES.items():
        if keyword.endswith(suffix):
            forms.add(keyword[: -len(suffix)] + short)
    return forms


# 문맥 alias 최소 증거(같은 확장형이 최소 몇 개 표시 기사에서 반복돼야 하는지).
_CONTEXTUAL_ALIAS_MIN_ARTICLES = 2


def _contextual_alias_forms(canonical_tokens, articles) -> dict:
    """기사 묶음 안에서만 검증되는 문맥 기반 약칭↔정식명칭 alias 매핑을 산출한다
    (ChatGPT P1 사전검토, 2026-07-21). 특정 기업명 하드코딩·무제한 접두 허용 없이,
    "이 기사 묶음에서 확장형이 하나로 우세하게 수렴할 때만" alias로 인정한다.

    반환: {canonical_token: {expansion, ...}} — grounding/comparison 경로에 alias_forms로
    주입되어 canonical '삼성'이 기사 '삼성전자'로 grounded 되게 한다. 수렴하지 않으면 그
    토큰은 매핑에 넣지 않아 기존 fail-closed 계약이 그대로 유지된다.

    계약(요약):
    - exact·조사결합·정상 붙여쓰기 복합은 _word_contains_token이 이미 처리하므로 여기선
      **확장형(정식명칭)** 만 대상으로 한다: 기사 어절 E가 canonical token T로 시작하되
      len(E)-len(T) >= 2 인 진짜 확장(조사 1글자 결합이 아님).
    - 동일 확장형 E가 표시 기사 최소 _CONTEXTUAL_ALIAS_MIN_ARTICLES 건에서 반복될 것.
    - E가 등장한 기사들에서 canonical의 **나머지 의미 토큰**(다른 canonical_tokens)도 함께
      근거를 가질 것(사건 토큰이 서로 다른 기사에 분산되면 alias 불인정).
    - 같은 접두 T를 갖는 **경쟁 확장형이 둘 이상 혼재**하면(삼성전자/삼성물산/삼성중공업)
      단일 alias로 확정하지 않는다(충돌 → fail-closed 유지).
    - 확장형이 정확히 하나로 수렴할 때만 {T: {E}} 인정.
    """
    from news.summarizer import _tokens

    canon = {t for t in (canonical_tokens or set()) if t and len(t) >= 2}
    if not canon or not articles:
        return {}

    # 기사별 어절 집합(제목+스니펫)을 미리 계산.
    art_tokens = []
    for a in articles:
        text = f"{a.get('title', '')} {a.get('snippet', '')}"
        art_tokens.append(set(_tokens(text)))

    alias_map: dict = {}
    for t in canon:
        others = canon - {t}
        # T로 시작하는 진짜 확장형 후보 수집. '삼성전자'/'삼성전자가'처럼 조사만 다른 표면형은
        # base(1글자 조사·어미 제거)로 묶어 하나의 확장형으로 본다(조사 변이를 '경쟁 확장형'으로
        # 오판해 alias를 잃지 않도록). 기사 인덱스와 표면형(surface)을 base별로 누적한다.
        by_base: Dict[str, Dict[str, set]] = {}  # base -> {"arts": set(idx), "forms": set(surface)}
        for idx, toks in enumerate(art_tokens):
            for w in toks:
                if w != t and w.startswith(t) and len(w) - len(t) >= 2:
                    base = w[:-1] if (len(w) >= 3 and w[-1] in _ONE_CHAR_JOSA_EOMI) else w
                    entry = by_base.setdefault(base, {"arts": set(), "forms": set()})
                    entry["arts"].add(idx)
                    entry["forms"].add(w)
        # 최소 증거(>=N개 기사)를 만족하는 확장형 base만.
        qualified = {b: e for b, e in by_base.items() if len(e["arts"]) >= _CONTEXTUAL_ALIAS_MIN_ARTICLES}
        if len(qualified) != 1:
            # 0개(증거 부족) 또는 2개 이상(경쟁 확장형 base 혼재) → 단일 alias 확정 금지.
            continue
        base, entry = next(iter(qualified.items()))
        arts_with_e = entry["arts"]
        # 나머지 canonical 의미 토큰도 확장형 등장 기사에서 함께 근거를 가질 것.
        # 이때 각 토큰 o 검증에도 _word_contains_token의 sibling exact-composition 계약을
        # 그대로 쓴다(Codex P1, 2026-07-22): o의 siblings로 canonical의 다른 토큰들(canon-{o})을
        # 넘겨, 기사에서 붙여쓰기된 복합('갤럭시카드')을 '갤럭시'/'카드'의 정당 근거로 인정한다.
        # sibling 없이 호출하면 '삼성 갤럭시 카드'가 '삼성전자 갤럭시카드' 기사에서 alias를 못
        # 얻어 과잉 drop된다. 단순 substring·무제한 접두는 여전히 인정하지 않는다.
        if others:
            supported = any(
                all(
                    any(_word_contains_token(w, o, canon - {o}) for w in art_tokens[i])
                    for o in others
                )
                for i in arts_with_e
            )
            if not supported:
                continue
        # base와 관측된 표면형(조사 결합 포함)을 모두 alias로 등록.
        alias_map[t] = {base} | entry["forms"]
    return alias_map


def _is_similar_keyword(a: str, b: str) -> bool:
    """유사 키워드 판정(개선1). 완전 일치/정규화 일치/기관명 축약/substring/token 유사 순으로 확인."""
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    if _norm_for_compare(a) == _norm_for_compare(b):
        return True

    # 기관명 축약어(고등학교 ↔ 고 등)
    forms_a = _institution_alias_forms(a)
    forms_b = _institution_alias_forms(b)
    if forms_a & forms_b:
        return True
    norm_forms_a = {_norm_for_compare(f) for f in forms_a}
    norm_forms_b = {_norm_for_compare(f) for f in forms_b}
    if norm_forms_a & norm_forms_b:
        return True

    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    # substring containment — 너무 넓은 단독 단어는 제외(오탐 방지)
    if shorter in longer and shorter not in _TOO_BROAD_SINGLE_WORDS and len(shorter) >= 2:
        return True

    # 단어(정규식 토큰, summarizer._tokens 재사용) 단위 Jaccard — 최소 2단어 이상일 때만
    # 판정한다(Codex diff 리뷰 P2: 문자 단위 set은 짧은 한글 키워드에서 서로 무관한
    # 단어끼리도 글자를 많이 공유해 오탐이 잦음. 단어 단위 + 두 키워드 모두 복수
    # 토큰을 가질 때만 비교하면 "정부"/"정치" 같은 무관 단일어 오탐을 크게 줄인다).
    from news.summarizer import _tokens as _kw_tokens
    toks_a, toks_b = set(_kw_tokens(a)), set(_kw_tokens(b))
    if len(toks_a) >= 2 and len(toks_b) >= 2:
        overlap = len(toks_a & toks_b) / len(toks_a | toks_b)
        if overlap >= DEDUPE_TOKEN_JACCARD_THRESHOLD:
            return True
    return False


def _article_overlap(articles_a: List[Dict], articles_b: List[Dict]) -> float:
    """두 keyword의 대표 기사 묶음 간 overlap(0~1). URL 일치 우선, 없으면 article-level
    pairwise 최댓값(title/snippet token Jaccard).

    incidental/side-mention 기사(candidates.compute_article_relevance가 is_incidental=True로
    판정한 부수 언급/판촉/증정 기사, 또는 object_side_mention으로 판정한 조치 대상 물품
    언급 기사)는 비교 대상에서 제외한다(Codex diff 리뷰 P2: "선풍기 증정" 같은 부수 언급
    기사가 다른 후보와 URL/문구를 공유한다는 이유만으로 same-issue merge되면, article
    relevance 필터링(개선4/5)의 설계 의도와 충돌한다 — 부수 언급은애초에 "그 키워드의
    핵심 이슈"가 아니므로 이슈 동일성 판정 근거가 될 수 없다. object_side_mention은
    is_incidental=False이지만 같은 이유로 근거에서 제외해야 한다 — Codex review-only P1:
    "노트북 회수" 기사가 URL/token overlap으로 다른 keyword와 merge되는 것을 방지 —
    아래서 _is_same_issue_evidence_article()로 판정 기준을 통일한다).

    기사들을 하나의 token union으로 합쳐서 비교하지 않는다(이전 리뷰 P2 재발 방지: 한
    키워드가 무관 기사를 여러 건 가지고 있으면 union이 커져, 실제로 겹치는 기사 쌍이
    있어도 전체 Jaccard가 희석돼 놓칠 수 있었음). 대신 A의 기사 하나하나를 B의 기사
    하나하나와 짝지어 비교해 가장 높은 pair의 overlap을 채택한다.
    """
    relevant_a = [a for a in (articles_a or []) if _is_same_issue_evidence_article(a)]
    relevant_b = [b for b in (articles_b or []) if _is_same_issue_evidence_article(b)]
    return _pairwise_evidence_overlap(relevant_a, relevant_b)


def _is_same_issue_evidence_article(article: Dict) -> bool:
    """same-issue merge 판정 근거로 쓸 수 있는 기사인지.

    is_incidental=True는 "경품 나열 부수 언급"과 "keyword가 title에 없고 snippet에만
    있어 그 keyword의 핵심 주제로는 약함"이라는 서로 다른 두 의미를 한 플래그로
    섞고 있다(candidates.compute_article_relevance의 relevance_reason 참조). 전자
    (incidental_giveaway_mention)는 같은 사건 판별 근거로도 부적합하지만(4차 리뷰 P2:
    "선풍기 증정" 부수 언급 기사가 merge 근거가 되면 안 됨), 후자
    (snippet_only_incidental_mention)는 "이 keyword의 대표 이슈로는 약하다"는 뜻일 뿐
    사건 자체와 무관한 기사라는 뜻은 아니므로 사건 토큰 근거에서 제외할 이유가 없다
    (Codex review-only 조언: relevance_reason별 세분화, keyword_not_found도 근거 배제).
    """
    reason = article.get("relevance_reason")
    if reason in ("incidental_giveaway_mention", "keyword_not_found", "object_side_mention"):
        return False
    # relevance_reason이 없는 경우(구버전 데이터/방어적 기본값)는 is_incidental 플래그만으로
    # 판단 — 세분화된 사유를 알 수 없으면 보수적으로 근거에서 제외한다.
    if reason is None and article.get("is_incidental"):
        return False
    return True


# 사건 토큰 overlap 판정에서 흔한 서술어를 제외하는 명시적 소수 블랙리스트. 전역
# STOPWORDS 확장(1차 시도)은 "발표"/"내용" 같은 단어가 끝없이 늘어나는 유지보수
# 문제가 있었다(Codex review-only 지적). 이 블랙리스트는 same-issue 최종 게이트
# (_is_same_issue)에서만 좁게 쓰여 다른 로직(요약/dedupe)에는 영향을 주지 않는다.
#
# "정치/국회/선거" 맥락(국조특위/개표소 등)과 "범죄/수사/인물 사건" 맥락(경찰/검찰/
# 진입 등)이 일반 사건 단어만 겹쳐 오탐 merge되는 문제(운영 반영 후속: "국조특위
# 개표소 진입"과 "장윤기 사건"이 "사건" 한 단어만으로 anchor 조건을 통과해 잘못
# 병합됨)를 막기 위해 확장한다. "국조특위"는 토크나이저(_TOKEN_RE, summarizer.py)가
# 형태소 분석 없이 정규식으로만 토큰화하므로 "국조"/"특위"로 자동 분리되지 않아
# 복합어 형태 그대로도 등록한다(Codex review-only 지적).
_GENERIC_EVENT_PREDICATE_WORDS = {
    "발표", "오늘", "내용", "관련", "예정", "공개", "진행",
    "사건", "경찰", "검찰", "감찰", "증거", "진입", "논란", "수사",
    "의혹", "확인", "폐기", "충돌", "국조", "특위", "국조특위",
}


def _count_same_issue_evidence_articles(articles: List[Dict]) -> int:
    return len([a for a in (articles or []) if _is_same_issue_evidence_article(a)])


def _group_df_tokens(articles: List[Dict], min_df: int = 2) -> set:
    """same-issue merge 근거로 유효한 article 그룹 전체에서 문서빈도(DF) >= min_df인
    토큰 집합. 유효 기사가 1건뿐이면(singleton) 그 기사의 전체 토큰을 후보로 반환한다.

    "고유명사/사건성 토큰"을 형태소 분석 없이 근사하는 신호(Codex review-only 조언 반영,
    옵션 B). 대표 문구 한두 줄만 비교하면(1차 시도) 정보량이 적어 재현율이 낮고,
    단순 불용어 제외(1차 시도의 _significant_tokens)만으로는 "발표"/"내용"처럼 흔한
    서술어가 두 그룹 모두에서 반복될 때 오탐을 막지 못했다. 같은 keyword 그룹 안에서
    "여러 기사에 걸쳐 반복 등장"하는 토큰만 후보로 삼으면 흔한 서술어라도 그 그룹의
    실제 사건 어휘(예: "배재고", "출전정지", "6개월")일 가능성이 높아진다.

    singleton 그룹은 "반복 등장"을 관측할 수 없어 전체 토큰이 후보가 되므로 오탐 위험이
    있다(예: "정부 오늘 새 정책 발표" 단일 기사끼리 "발표"만 겹침) — 이 위험은 호출부
    (_is_same_issue)의 "양쪽 다 singleton이면 신호 비활성화" + "겹치는 토큰 중 흔한
    서술어(_GENERIC_EVENT_PREDICATE_WORDS)가 아닌 것이 최소 1개 포함" 게이트로 방어한다
    (Codex review-only 조언: 한쪽 singleton + 반대쪽 DF>=2 + non-generic shared token
    조합으로 제한).
    """
    relevant = [a for a in (articles or []) if _is_same_issue_evidence_article(a)]
    if not relevant:
        return set()
    if len(relevant) < min_df:
        toks = set()
        for a in relevant:
            toks |= set(_tokens_of(a))
        return toks

    from news.summarizer import _tokens

    df: Dict[str, int] = {}
    for a in relevant:
        text = f"{a.get('title', '')} {a.get('snippet', '')}"
        for tok in set(_tokens(text)):
            df[tok] = df.get(tok, 0) + 1
    return {t for t, c in df.items() if c >= min_df}


def _tokens_of(article: Dict) -> List[str]:
    from news.summarizer import _tokens

    return _tokens(f"{article.get('title', '')} {article.get('snippet', '')}")


def _keyword_anchor_tokens(item: Dict) -> set:
    """item의 keyword 자체를 토큰화한 집합(2자 이상) 중 일반 사건/정치 단어
    (_GENERIC_EVENT_PREDICATE_WORDS)를 제외한 고유명사성 anchor만 반환한다.
    same-issue merge의 precision 게이트로 쓴다.

    - "정부 오늘 새 정책 발표" vs "기업 오늘 실적 발표"처럼 사건 자체가 다른데
      흔한 서술어만 겹치는 경우, 상대 keyword가 서로의 기사/키워드에 전혀
      등장하지 않으므로 이 게이트에서 막힌다.
    - "장윤기 사건"처럼 keyword 자체에 일반 사건 단어("사건")가 포함된 경우, 그
      단어를 anchor 후보에서 제외해야 한다(운영 반영 후속: "사건"이 anchor로
      인정되면 "국조특위 개표소 진입" 그룹 기사에 "사건"이라는 흔한 단어만
      등장해도 anchor 교차 조건을 통과해 서로 다른 이슈가 병합됨). anchor는
      "장윤기"처럼 그 이슈에 고유한 토큰으로만 좁혀야 한다.
    """
    from news.summarizer import _tokens

    return {
        t for t in _tokens(item.get("keyword", ""))
        if len(t) >= 2 and t not in _GENERIC_EVENT_PREDICATE_WORDS
    }


def _has_cross_keyword_anchor(
    item_a: Dict,
    item_b: Dict,
    shared_tokens: set,
    articles_a: Optional[List[Dict]] = None,
    articles_b: Optional[List[Dict]] = None,
) -> bool:
    """겹치는 사건 토큰(shared_tokens) 중, 두 keyword 중 하나의 anchor 토큰이 포함되거나
    한쪽 keyword의 anchor 토큰이 상대 article 그룹에 등장하는지 확인.

    "배재고 출전정지" ↔ "권오영 감독" 케이스: "배재고"가 양쪽 article 그룹에 반복
    등장하므로 anchor 조건을 만족한다(권오영 감독 그룹의 기사에도 "배재고"가 실제로
    반복 등장). 반대로 "정부 오늘 새 정책 발표" ↔ "기업 오늘 실적 발표"는 keyword
    anchor("정부"/"기업")가 서로의 그룹에 등장하지 않아 걸러진다.

    articles_a/articles_b: 검사 대상 기사 리스트를 명시 지정(이미 evidence 필터 통과).
    None이면 기존처럼 item 전체 기사에서 evidence 필터를 적용해 읽는다. 공유 URL bridge
    보정 경로는 잔여(비공유) 기사만 명시 전달해, 공유 roundup 기사가 anchor 등장 근거를
    스스로 충족시키는 우회를 막는다(Codex 계획리뷰 P1).
    """
    anchors_a = _keyword_anchor_tokens(item_a)
    anchors_b = _keyword_anchor_tokens(item_b)
    if shared_tokens & (anchors_a | anchors_b):
        return True

    if articles_a is None:
        articles_a = _evidence_articles_of(item_a)
    if articles_b is None:
        articles_b = _evidence_articles_of(item_b)
    tokens_in_a = set()
    for a in articles_a:
        tokens_in_a |= set(_tokens_of(a))
    tokens_in_b = set()
    for b in articles_b:
        tokens_in_b |= set(_tokens_of(b))
    if anchors_a & tokens_in_b or anchors_b & tokens_in_a:
        return True
    return False


def _representative_overlap(item_a: Dict, item_b: Dict) -> set:
    """두 ranked item의 article 그룹 간 공유 사건 토큰 집합(DF>=2 근사, 옵션 B).

    반환은 비율이 아니라 "겹치는 사건 토큰 집합" 자체 — 호출부가 개수/anchor 조건을
    함께 판단해야 하므로 스칼라 점수보다 집합이 더 유용하다.
    """
    articles_a = (item_a.get("news_meta") or {}).get("articles") or []
    articles_b = (item_b.get("news_meta") or {}).get("articles") or []
    df_a = _group_df_tokens(articles_a)
    df_b = _group_df_tokens(articles_b)
    return df_a & df_b


def _evidence_articles_of(item: Dict) -> List[Dict]:
    """item의 same-issue merge 판정용 유효 근거 기사 리스트."""
    articles = (item.get("news_meta") or {}).get("articles") or []
    return [a for a in articles if _is_same_issue_evidence_article(a)]


def _merge_anchor_tokens(item: Dict) -> set:
    """공유 URL bridge 보정(corroboration) 전용의 더 엄격한 anchor 집합.

    `_keyword_anchor_tokens`에서 검색의도 어휘(_SEARCH_INTENT_SUFFIXES: "결혼" 등)와
    display 일반어(_all_display_generic)를 추가로 제외한다. 단독 일반 토큰("결혼")이
    span=0으로 자명하게 grounding돼 roundup bridge를 재허용하는 것을 막고, 고유명사성
    anchor("정평"/"허양임")만 남긴다. 기존 `_keyword_anchor_tokens` 소비처(공유 URL이
    없는 기존 merge 경로)는 건드리지 않는다 — 이 헬퍼는 아래 `_is_same_issue`의
    공유 URL 분기에서만 사용한다(no-shared 경로 bit-identical 유지, Codex 계획리뷰).
    """
    return _keyword_anchor_tokens(item) - _SEARCH_INTENT_SUFFIXES - _all_display_generic()


def _title_tokens_of(article: Dict) -> set:
    """near-duplicate 판정 전용 title 토큰 집합(snippet 미포함 — 상수 주석 참고)."""
    from news.summarizer import _tokens

    return set(_tokens(article.get("title", "") or ""))


def _is_near_duplicate_title(tokens_a: set, tokens_b: set) -> bool:
    """두 기사 제목이 "사실상 같은 기사"(제휴/전재 신디케이션)인지 판정.

    양쪽 다 최소 토큰 수를 넘고 Jaccard가 임계값 이상일 때만 참. 짧은 제목은
    우연 일치 위험이 커서 하한(_NEAR_DUPLICATE_MIN_TITLE_TOKENS)으로 차단한다.
    """
    if (
        len(tokens_a) < _NEAR_DUPLICATE_MIN_TITLE_TOKENS
        or len(tokens_b) < _NEAR_DUPLICATE_MIN_TITLE_TOKENS
    ):
        return False
    union = tokens_a | tokens_b
    if not union:
        return False
    return len(tokens_a & tokens_b) / len(union) >= _NEAR_DUPLICATE_TITLE_JACCARD


def _split_shared_evidence(ev_a: List[Dict], ev_b: List[Dict]) -> tuple:
    """두 근거 리스트를 (공유 근거, 잔여 근거)로 가른다.

    "공유 근거" = 같은 URL이거나, 서로 다른 URL로 신디케이트된 사실상 동일 기사
    (`_is_near_duplicate_title`). PR #17은 공유 URL만 공유 근거로 봤는데, 같은 roundup이
    다른 URL을 달면 URL 교집합이 비어 가드가 통째로 우회됐다(2026-08-05 진단).

    **set-membership 판정**이다 — "상대편에 하나라도 공유 대응이 있으면 shared". 기사쌍을
    소비하는 bipartite matching이 아니므로 대칭이고, dedupe_and_merge의 fixed-point에서
    순서 의존이 생기지 않는다.

    near-dup 판정은 URL 유무를 요구하지 않는다 — URL이 없거나 빈 기사도 제목이 사실상
    동일하면 같은 근거로 본다(URL 결측을 merge 우회 통로로 두지 않는 fail-closed 방향).

    near-dup이 0건이면 결과는 기존 URL 기준 분할과 **집합 동등**하다(공유 URL 경로 보존).
    반환: (shared_a, shared_b, rest_a, rest_b) — 각 리스트의 원소 순서는 입력 순서 유지.
    """
    urls_a = {a.get("url") for a in ev_a if a.get("url")}
    urls_b = {b.get("url") for b in ev_b if b.get("url")}
    shared_urls = urls_a & urls_b

    # title 토큰은 리스트당 1회만 계산해 재사용(fixed-point 반복 호출 대비).
    toks_a = [_title_tokens_of(a) for a in ev_a]
    toks_b = [_title_tokens_of(b) for b in ev_b]

    shared_a, rest_a = [], []
    for i, a in enumerate(ev_a):
        url = a.get("url")
        if (url and url in shared_urls) or any(
            _is_near_duplicate_title(toks_a[i], tb) for tb in toks_b
        ):
            shared_a.append(a)
        else:
            rest_a.append(a)

    shared_b, rest_b = [], []
    for j, b in enumerate(ev_b):
        url = b.get("url")
        if (url and url in shared_urls) or any(
            _is_near_duplicate_title(toks_b[j], ta) for ta in toks_a
        ):
            shared_b.append(b)
        else:
            rest_b.append(b)

    return shared_a, shared_b, rest_a, rest_b


def _pairwise_evidence_overlap(relevant_a: List[Dict], relevant_b: List[Dict]) -> float:
    """이미 evidence 필터를 통과한 두 기사 리스트의 overlap(_article_overlap과 동일 계약).

    URL 일치 우선(1.0), 없으면 기사쌍 최대 token Jaccard. `_article_overlap`의 내부
    로직을 리스트 입력으로 분리한 것 — 공유 URL 제외 후의 잔여(residual) 기사끼리
    재판정할 때 필요하다. `_article_overlap`은 이 함수를 그대로 사용한다.
    """
    urls_a = {a.get("url") for a in relevant_a if a.get("url")}
    urls_b = {b.get("url") for b in relevant_b if b.get("url")}
    if urls_a and urls_b and (urls_a & urls_b):
        return 1.0

    from news.summarizer import _tokens

    def _toks(a: Dict) -> set:
        return set(_tokens(f"{a.get('title', '')} {a.get('snippet', '')}"))

    toks_list_a = [_toks(a) for a in relevant_a]
    toks_list_b = [_toks(b) for b in relevant_b]
    if not toks_list_a or not toks_list_b:
        return 0.0

    best = 0.0
    for ta in toks_list_a:
        if not ta:
            continue
        for tb in toks_list_b:
            if not tb:
                continue
            union = ta | tb
            if not union:
                continue
            overlap = len(ta & tb) / len(union)
            if overlap > best:
                best = overlap
    return best


def _same_issue_evidence_signals(
    item_a: Dict, item_b: Dict, ev_a: List[Dict], ev_b: List[Dict]
) -> bool:
    """기존 same-issue 판정 신호(overlap OR DF+anchor)를 명시적 기사 리스트로 평가한다.

    ev_a/ev_b는 이미 `_is_same_issue_evidence_article` 필터를 통과한 리스트다.
    공유 URL이 없는 pair에는 전체 근거 리스트가 그대로 들어와 기존 동작과 완전히
    동일하고(bit-identical), 공유 URL이 있는 pair에는 잔여(비공유) 리스트만 들어와
    "공유 roundup 기사가 DF/anchor 근거를 스스로 충족시키는" 우회를 차단한다
    (Codex 계획리뷰 P1: `_has_cross_keyword_anchor`가 전체 기사를 다시 읽으면 안 됨).

    신호 조건(기존 그대로):
    1. 양쪽 근거 0건이면 불가, 양쪽 다 singleton(1건)이면 불가.
    2. DF>=2(또는 singleton fallback) 토큰이 REPRESENTATIVE_OVERLAP_MIN_SHARED_TOKENS개
       이상 겹친다.
    3. 겹침 중 keyword anchor 교차 조건(_has_cross_keyword_anchor 계약).
    4. 겹침에 일반 서술어가 아닌 토큰 최소 1개.
    """
    if _pairwise_evidence_overlap(ev_a, ev_b) >= MERGE_ARTICLE_OVERLAP_THRESHOLD:
        return True

    evidence_count_a = len(ev_a)
    evidence_count_b = len(ev_b)
    if evidence_count_a == 0 or evidence_count_b == 0:
        return False
    if evidence_count_a == 1 and evidence_count_b == 1:
        return False

    shared = _group_df_tokens(ev_a) & _group_df_tokens(ev_b)
    if len(shared) < REPRESENTATIVE_OVERLAP_MIN_SHARED_TOKENS:
        return False
    if not (shared - _GENERIC_EVENT_PREDICATE_WORDS):
        return False
    return _has_cross_keyword_anchor(item_a, item_b, shared, ev_a, ev_b)


def _anchor_grounded_in_articles(anchors: set, articles: List[Dict]) -> bool:
    """merge anchor 집합이 비어있지 않고, 단일 기사 한 필드 안에서 span 제한 이내로
    함께 등장하는지(_combo_span_grounded 계약 재사용).

    공집합은 자명하게 참이 되므로 반드시 거짓 처리한다(빈 anchor 키워드는 이 경로로
    merge할 수 없다 — Codex 계획리뷰 P1). 서로 다른 기사에 흩어진 토큰 합집합으로는
    보정하지 않는다(단일 기사 필드 grounding만 인정).
    """
    if not anchors:
        return False
    return _combo_span_grounded(anchors, articles)


# 비공유 근거 경로에서 merge bridge 로 인정할 최소 "교차 근거 기사 수".
# 1이면 다중 사건 나열(roundup) 기사 **한 건**만으로 서로 다른 이슈가 붙는다
# (2026-09-03 06:48 운영 사례: 정몽규 그룹이 지예은/카사마츠/뉴욕증시 등 10개
# 무관 이슈를 흡수해 selected 7). 같은 사건이라면 양쪽 검색이 각자 복수의 독립
# 보도를 확보하므로 2건 이상 교차한다 — 운영 06:48 선정 7건의 상호 최대 Jaccard 는
# 0.000~0.077 로 이 조건과 무관하고, 실제 동일 이슈 pair 는 2/2·2/3 로 통과한다.
_MERGE_MIN_CROSS_EVIDENCE_ARTICLES = 2


def _cross_evidence_support(item_self: Dict, item_other: Dict, articles: List[Dict]):
    """상대 keyword 의 merge anchor 가 등장하는 내 근거 기사 수. anchor 가 없으면 None.

    "이 merge 를 뒷받침하는 내 기사가 몇 건인가"를 센다. roundup 한 건만 상대 사건을
    언급하고 나머지 기사는 전부 다른 사건이면 1이 되고, 진짜 같은 사건이면 내 보도
    다수에 상대 anchor 가 반복 등장한다.

    None 은 "지지 0건"과 다르다 — 상대 keyword 가 전부 일반어라 anchor 집합이 비면
    (예: "신임") 이 신호 자체를 관측할 수 없다는 뜻이다. 관측 불가를 0으로 접으면
    근거 없이 merge 를 막게 되므로 호출부에서 구분해 판정을 보류한다.
    """
    anchors = _merge_anchor_tokens(item_other)
    if not anchors:
        return None
    return sum(1 for a in articles if anchors & set(_tokens_of(a)))


def _has_multi_article_cross_evidence(
    item_a: Dict, item_b: Dict, ev_a: List[Dict], ev_b: List[Dict]
) -> bool:
    """양쪽 모두 단 1건의 기사로만 교차 연결되는(=roundup bridge 의심) pair 인지 판정.

    참이면 교차 근거가 충분하다는 뜻이라 기존 판정을 그대로 살린다. 거짓이면 양쪽 다
    지지 기사가 1건 이하 — 서로 다른 사건을 나열한 기사 한 건이 유일한 접점이므로
    merge 하지 않는다. 근거가 원래 1건뿐인 keyword(singleton)는 이 조건을 만족시킬
    수 없으므로 판정 대상에서 제외한다(기존 동작 보존 — 여기서 막으면 정상 소규모
    이슈까지 못 붙는다).

    **의도적 fail-closed trade-off(known risk).** 진짜 같은 사건이라도 양쪽 근거가
    2건뿐이고 접점이 1:1 이면 분리될 수 있다. 두 방향의 비용이 대칭이 아니라서 이쪽을
    택했다 — false merge 는 나열 기사 한 건이 transitive 연쇄로 10개 이슈를 한 그룹에
    접어 Top10 을 7개로 붕괴시키지만(2026-09-03 06:48, run 4912ac60), 과분리는 같은
    사건이 두 줄로 보이는 데 그치고 근거가 조금만 두터워지면(support >= 2) 사라진다.

    이 분리가 운영에서 실제 문제로 관측되더라도 **_MERGE_MIN_CROSS_EVIDENCE_ARTICLES
    를 낮추지 말 것** — 그건 06:48 붕괴를 그대로 되돌린다. 대신 near-dup 탐지(공유 근거
    승격) 쪽을 개선해 가드를 우회하지 않고 정상 경로로 merge 시켜야 한다.
    회귀 고정: tests/test_news_ranking.py::test_sparse_same_event_split_is_the_accepted_tradeoff
    """
    if len(ev_a) < _MERGE_MIN_CROSS_EVIDENCE_ARTICLES or len(ev_b) < _MERGE_MIN_CROSS_EVIDENCE_ARTICLES:
        return True
    support_a = _cross_evidence_support(item_a, item_b, ev_a)
    support_b = _cross_evidence_support(item_b, item_a, ev_b)
    observed = [s for s in (support_a, support_b) if s is not None]
    if not observed:
        # 양쪽 다 anchor 가 비어 신호를 관측할 수 없다 — 기존 판정에 맡긴다(fail-open).
        return True
    return max(observed) >= _MERGE_MIN_CROSS_EVIDENCE_ARTICLES


# roundup bridge 로 의심할 기사쌍 유사도의 상한(미만). 이 값 이상이면 "부분적으로 겹치는
# 나열 기사"가 아니라 사실상 같은 보도라 교차 근거 가드를 적용하지 않는다. near-dup
# 임계(_NEAR_DUPLICATE_TITLE_JACCARD)는 title only 로 재는 반면 여기 overlap 은
# title+snippet 이라 같은 값을 공유하지 않고 별도 상수로 둔다.
_MERGE_BRIDGE_SUSPECT_MAX_OVERLAP = 1.0


def _is_same_issue(item_a: Dict, item_b: Dict) -> bool:
    """same-issue merge 판정: 공유 URL 근거는 비공유(잔여) 근거의 교차 확인을 요구한다.

    배경(2026-07-30, 운영 진단): "고지용 이혼→문근영 결혼→황정민 사생활 의혹→…"처럼
    서로 다른 사건을 나열하는 연예/종합(roundup) 기사가 두 키워드의 네이버 검색 결과에
    모두 잡히면, 기존 로직은 URL 일치만으로 즉시 merge(1.0)해 서로 다른 실제 사건이
    한 이슈로 붕괴했다(transitive 연쇄로 최대 5개 키워드/3개 사건이 1그룹). 이것이
    Top10 미달(underfill)의 지배적 원인이었다(48h 27회 미달 실행 전수에서 RANK_CUTOFF=0,
    gate 통과 17~38개가 merge 후 5~12개로 붕괴).

    2026-08-05 확장: "공유 근거"를 공유 URL만이 아니라 **서로 다른 URL로 신디케이트된
    사실상 동일 기사**(_split_shared_evidence)까지로 넓혔다. 같은 roundup이 제휴/전재로
    다른 URL을 달면 URL 교집합이 비어 위 가드가 통째로 우회되고, 기존 경로의 첫 신호인
    pairwise Jaccard가 1.0이 되어 서로 다른 사건이 즉시 병합됐다(48h 미달 9회 전수에서
    RANK_CUTOFF=0 · 최대 merge group 16.7 vs 정상 3.5). 분기 판정 로직 자체는 그대로이고
    바뀐 것은 무엇을 공유 근거로 볼지의 정의뿐이다.

    판정 규칙(공유 근거 유무로 분기 — 공유 없음 경로는 기존과 완전 동일):
    - 공유 근거 없음: 기존 신호 그대로(_same_issue_evidence_signals에 전체 근거 전달).
    - 양쪽 근거가 전부 공유(동일 coverage): 문자열 유사 키워드거나, 두 keyword의
      merge anchor 합집합이 공유 기사 한 필드 안에 span 제한으로 grounding될 때만 merge.
    - 한쪽 근거만 전부 공유(subset): 문자열 유사 키워드거나, subset 쪽 merge anchor가
      상대 잔여 기사 단일 필드에 grounding될 때만 merge('정평' ⊂ '문근영 결혼' 보존).
    - 양쪽 모두 잔여 근거 보유: 기존 신호를 잔여 근거만으로 재평가하고, 실패 시
      문자열 유사 또는 merge anchor의 상대 잔여 grounding(양방향)으로만 보정.
      같은 사건이라면 양쪽 검색이 각자 독립 보도를 확보하므로 잔여끼리도 교차 근거가
      남는다 — 다중 사건 roundup 공유만으로는 merge 근거가 되지 않는다.
    """
    ev_a = _evidence_articles_of(item_a)
    ev_b = _evidence_articles_of(item_b)
    shared_a, shared_b, rest_a, rest_b = _split_shared_evidence(ev_a, ev_b)

    if not shared_a and not shared_b:
        # 공유 근거가 전혀 없는데도 붙는 경로. 여기서 merge 를 만드는 건
        # _pairwise_evidence_overlap 의 "기사쌍 최대 Jaccard >= 0.5" 인데, 서로 다른
        # URL 로 각자 작성된 다중 사건 나열(roundup) 기사끼리는 near-dup 임계(0.9)에
        # 못 미쳐 공유 근거로 승격되지 않으면서 이 임계는 넘겨(운영 실측 0.667~0.750)
        # 공유 근거 가드를 통째로 우회했다. 교차 근거가 양쪽 다 기사 1건뿐이면
        # 같은 사건의 근거가 아니라 나열 기사 한 건이 유일한 접점이라는 뜻이다.
        # 기사쌍이 사실상 동일(overlap 1.0)하면 나열 기사 bridge 가 아니라 같은 보도다 —
        # 가드 대상에서 빼고 기존 판정을 그대로 쓴다.
        if (
            _pairwise_evidence_overlap(ev_a, ev_b) < _MERGE_BRIDGE_SUSPECT_MAX_OVERLAP
            and not _has_multi_article_cross_evidence(item_a, item_b, ev_a, ev_b)
        ):
            return False
        return _same_issue_evidence_signals(item_a, item_b, ev_a, ev_b)

    kw_a = item_a.get("keyword", "")
    kw_b = item_b.get("keyword", "")

    if not rest_a and not rest_b:
        # 동일 coverage(A==B==공유). 문자열 유사이거나, 두 keyword의 anchor 합집합이
        # 공유 기사 "한 필드 안 근접 span"으로 함께 grounding되면 같은 사건으로 본다
        # ("공수처 김영환 압수수색"류 통과). 서로 다른 list 구획에 흩어진 roundup은
        # span 조건에서 배제된다. 알려진 한계: span 이내로 인접한 roundup 구획은
        # 여전히 merge될 수 있다(둘 다 roundup만으로 cohesion gate를 통과해야 하는
        # 희귀 케이스 — 관찰 로그로 추적).
        if _is_similar_keyword(kw_a, kw_b):
            return True
        # 동일 coverage는 subset/잔여 fallback과 달리 엄격 merge anchor가 아니라 일반
        # anchor 합집합을 쓴다(Codex diff 리뷰 P1: "조사"+"김영환"처럼 한쪽 anchor가
        # 검색의도/일반어 필터로 비는 진짜 중복 pair의 recall 보존). 합집합이 한 기사
        # 한 필드의 근접 span 안에 함께 grounding돼야 하므로, 단독 일반 토큰만으로
        # 자명 통과하는 subset류 우회와는 위험 구조가 다르다. 합집합 공집합은 거짓.
        anchor_union = _keyword_anchor_tokens(item_a) | _keyword_anchor_tokens(item_b)
        # 공유 URL이던 시절엔 shared_a와 shared_b가 같은 기사라 어느 쪽을 봐도 동일했지만,
        # near-dup은 제목이 완전히 같다는 보장이 없어 한쪽에만 상대 anchor가 있을 수 있다.
        # 그때 shared_a만 보면 _is_same_issue(a,b) != _is_same_issue(b,a)가 되고,
        # dedupe_and_merge가 score 순서로 호출하므로 순위에 따라 병합 여부가 흔들린다.
        # 양쪽 공유 근거를 모두 근거로 인정해 판정을 대칭으로 유지한다(Codex diff 리뷰 P2).
        return _anchor_grounded_in_articles(
            anchor_union, shared_a
        ) or _anchor_grounded_in_articles(anchor_union, shared_b)

    if not rest_a or not rest_b:
        # 한쪽만 전부 공유(subset). subset 쪽이 상대의 "자체 보도"에 실제로 등장해야
        # 같은 사건이다. roundup만 공유하는 무관 keyword는 상대 잔여 기사에 anchor가
        # 없어 차단된다.
        if _is_similar_keyword(kw_a, kw_b):
            return True
        subset_item, other_rest = (item_a, rest_b) if not rest_a else (item_b, rest_a)
        return _anchor_grounded_in_articles(_merge_anchor_tokens(subset_item), other_rest)

    # 양쪽 모두 잔여 근거 보유 — 기존 신호를 잔여 근거로만 재평가.
    if _same_issue_evidence_signals(item_a, item_b, rest_a, rest_b):
        return True
    if _is_similar_keyword(kw_a, kw_b):
        return True
    if _anchor_grounded_in_articles(_merge_anchor_tokens(item_a), rest_b):
        return True
    if _anchor_grounded_in_articles(_merge_anchor_tokens(item_b), rest_a):
        return True
    return False


def _shared_evidence_urls(item_a: Dict, item_b: Dict) -> set:
    """두 item의 유효 근거 기사 간 공유 URL 집합(관찰 로그 전용)."""
    urls_a = {a.get("url") for a in _evidence_articles_of(item_a) if a.get("url")}
    urls_b = {b.get("url") for b in _evidence_articles_of(item_b) if b.get("url")}
    return urls_a & urls_b


def _display_group_articles(members: List[Dict]) -> List[Dict]:
    """merge group 전체의 유효(same-issue evidence) 기사 목록(중복 URL 제거)."""
    seen_urls = set()
    articles = []
    for m in members:
        for a in (m.get("news_meta") or {}).get("articles") or []:
            if not _is_same_issue_evidence_article(a):
                continue
            url = a.get("url")
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            articles.append(a)
    return articles


def _token_article_coverage(articles: List[Dict]) -> Dict[str, float]:
    """그룹 기사 집합에서 각 토큰의 기사 분포율(= 그 토큰이 등장한 기사 수 / 전체 기사 수).

    사용자 확정 기준(2026-07-02): 대표성은 "공통토큰 개수"가 아니라 "그룹 전체 기사에
    얼마나 넓게 걸쳐 반복 등장하는가"로 본다. 일부 기사에만 나오는 상대국/지역/기업명은
    coverage가 낮아 자연히 감점된다(하드코딩 국가/기업명 리스트 없이 데이터로 처리).
    """
    from news.summarizer import _tokens

    n = len(articles)
    if n == 0:
        return {}
    hits: Dict[str, int] = {}
    for a in articles:
        text = f"{a.get('title', '')} {a.get('snippet', '')}"
        for tok in set(_tokens(text)):
            hits[tok] = hits.get(tok, 0) + 1
    return {t: c / n for t, c in hits.items()}


# display 공통토큰으로 인정할 최소 기사 분포율(그룹 기사 절반 이상에 등장).
DISPLAY_TOKEN_MIN_COVERAGE = 0.5

# 짧은 일반 생활명사 단독(singleton) display 보강 대상의 최대 글자 수(2026-07 운영
# 관찰: "안경" 단독 display가 실제 기사에서는 "AI 안경"으로 반복됨). length는 1차 후보
# 축소용일 뿐이고 실제 보강 여부는 prev-token modifier 반복률(coverage)이 결정한다.
SHORT_GENERIC_SINGLETON_MAX_LEN = 3

# display_keyword 전용 일반 서술어 블랙리스트 — 사용자에게 의미 없는 수식어가 대표
# 표시명이 되는 것을 막는다(운영 회귀 hotfix 2026-07-03: "홍석기 치안감" 그룹에서
# "신임"이 대표로 노출됨). _GENERIC_EVENT_PREDICATE_WORDS와 분리하는 이유: 후자는
# same-issue merge 판정에도 쓰여 여기에 인사 서술어를 넣으면 merge 로직에 side effect가
# 생긴다. 이 집합은 display 대표/조합 선택에서만 참조한다(merge 판정 불변).
_DISPLAY_GENERIC_WORDS = {
    "신임", "임명", "승진", "취임", "내정", "발탁", "선임", "인사", "전보",
    # "조사"는 "수사"류 일반 행위어지만 _GENERIC_EVENT_PREDICATE_WORDS에 넣으면
    # same-issue merge 판정에 side effect가 생기므로 display/singleton 전용인
    # 이 집합에 둔다(2026-07-03 운영 관찰: generic singleton 방어 확장).
    "조사",
    # 경제/행위 일반명사 — 운영 반영 후속 hotfix(2026-07-03): merge group에서 "한화
    # 영남권 55조"의 display가 "투자" 단독으로 뽑힘("신임" 회귀와 동일한 구조 — 고유명사
    # 없이 일반 서술어 토큰만으로 대표가 채택됨). "한화 투자"처럼 고유명사와 조합되면
    # _is_generic_only_display가 False라 그대로 허용되고, 단독일 때만 canonical로
    # fallback된다("조사"와 동일 메커니즘). "발표"는 _GENERIC_EVENT_PREDICATE_WORDS에
    # 이미 있어 중복 추가하지 않는다.
    "투자", "사업", "계획", "추진", "확대", "지원", "협력", "체결", "공급", "운영",
} | set(GENERIC_NEWS_SECTION_LABELS)


def _all_display_generic() -> set:
    """display 판정에서 제외할 일반어 = 기존 event predicate + 인사 서술어."""
    return _GENERIC_EVENT_PREDICATE_WORDS | _DISPLAY_GENERIC_WORDS


# === 검색의도 suffix display 방어(2026-07) ===
# "위홀 뜻"처럼 keyword 마지막 토큰이 검색 의도(뜻/의미/누구 등)를 나타내면, 그
# keyword 자체가 사용자에게 자연스러운 display가 아니다("이효리 조언"/"위홀 커플
# 조언"이 더 자연스러움). 토큰 "정확 일치"만 본다(substring 아님) — "이나이 대표"
# 처럼 suffix 문자열이 다른 토큰 일부로 우연히 섞인 경우까지 오탐 배제하면 안 된다
# (Codex review-only 지적).
_SEARCH_INTENT_SUFFIXES = {
    "뜻", "의미", "누구", "프로필", "나이", "인스타", "결혼", "근황", "학력", "직업",
}


def _ends_with_search_intent_suffix(keyword: str) -> bool:
    """keyword의 마지막 공백 구분 어절이 검색의도 suffix와 정확히 일치하는지.

    summarizer._tokens(정규식 `[가-힣A-Za-z0-9]{2,}`)는 "뜻"처럼 1글자 토큰을
    아예 만들지 않아 "위홀 뜻"의 suffix를 놓친다. 어절(공백) 분리 기준으로
    정확 일치만 보면 1글자 suffix도 포착하면서, "이나이 대표"처럼 suffix
    문자열이 다른 단어 일부로 섞인 경우는 별개 어절이 아니라 여전히 오탐하지
    않는다(Codex review-only 지적: substring 아닌 정확 일치 유지).
    """
    words = (keyword or "").split()
    return bool(words) and words[-1] in _SEARCH_INTENT_SUFFIXES


def _display_common_event_tokens(members: List[Dict]) -> set:
    """display_keyword 대표성 판정 전용 — merge group 전체 기사에서 분포율이
    DISPLAY_TOKEN_MIN_COVERAGE 이상인 "사건 핵심 토큰" 집합. 일반 서술어
    (_GENERIC_EVENT_PREDICATE_WORDS + _DISPLAY_GENERIC_WORDS)는 제외한다.

    문서빈도(개수)가 아니라 분포율(coverage)로 판정한다(사용자 확정 2026-07-02):
    개수 기준이면 "보스니아"/"헤르체고비나"가 상대국 기사 몇 건에 반복 등장하는 것만으로
    "월드컵"(1개 토큰)을 이겨버린다. 그룹 기사 절반 이상에 걸쳐 등장하는 토큰만
    핵심어로 인정하면, 대부분 기사에 나오는 "월드컵"/"16강"은 남고 일부 기사에만
    나오는 상대국명은 자연히 걸러진다.

    유효 기사가 2건 미만이면(반복 관측 불가) 빈 set(zero-confidence)을 반환해
    seed/구체성 tie-breaker 경로로 넘긴다.
    """
    return _common_event_tokens_from_articles(_display_group_articles(members))


def _common_event_tokens_from_articles(articles: List[Dict]) -> set:
    """_display_common_event_tokens의 저수준 버전 — merge group(members) 대신 articles
    리스트를 직접 받는다. singleton(merge 안 된 단독 keyword) display 보정에서 재사용
    (Codex review-only 지적: members 기반 헬퍼를 singleton에 그대로 씌우지 말고 articles
    인자를 받는 형태로 일반화)."""
    if len(articles) < 2:
        return set()
    coverage = _token_article_coverage(articles)
    generic = _all_display_generic()
    return {
        t for t, cov in coverage.items()
        if cov >= DISPLAY_TOKEN_MIN_COVERAGE and t not in generic
    }


def _seed_priority(member: Dict) -> int:
    """seed 출처 우선순위 점수(클수록 우선). 독립 홈/트렌드 family > naver_news_* 파생.
    tie-breaker 전용."""
    sources = set((member.get("sources") or {}).keys())
    if sources & _INDEPENDENT_SEARCH_FAMILIES:
        return 3
    return 1  # naver_news_aux/phrase 등 파생


def _keyword_coverage(member: Dict, group_articles: List[Dict]) -> float:
    """keyword의 (문자열 부분일치 기준) 기사 분포율 — keyword가 그룹 전체 기사 중
    몇 개에 실제로 등장하는가. 다어절 지역/상대국/기업명이 일부 기사에만 나오면
    낮게 나와 representative_score에서 감점된다(사용자 확정 2026-07-02).

    "keyword의 모든 토큰이 그 기사에 등장하는가"로 본다. 통짜 문자열 부분일치(kw in
    text)는 "보스니아 헤르체고비나" 같은 다어절 엔티티는 정확하지만, "메타" in
    "메타버스"/"AI" in "OpenAI"처럼 짧은 keyword가 다른 단어의 부분으로 오탐 카운트되는
    문제가 있다(Codex diff 리뷰 P3). keyword 토큰이 전부 기사 토큰에 있으면 등장으로
    보면, 다어절 엔티티 통짜 측정("보스니아"·"헤르체고비나" 둘 다 있는 기사만 카운트)과
    부분일치 오탐 방지를 동시에 만족한다.

    한계(Codex diff 재리뷰 P3, 의도적 보수 처리): 형태소 분석이 없어 조사/접미가
    붙은 표기("김영환이", "손흥민은")는 별도 토큰이라 exact subset이 어긋나 coverage가
    과소계산될 수 있다. 다만 coverage는 임계(DISPLAY_TOKEN_MIN_COVERAGE) 이진 감점에만
    쓰이고, 과소계산은 "대표성을 낮게 보는" 안전한 방향이라 지엽 엔티티를 과대평가하는
    오탐(더 위험)보다 낫다. 핵심어가 조사 때문에 부당 감점되더라도 그 후보가 canonical
    keyword(movement 비교용)로는 그대로 유지되므로 데이터 안정성에는 영향이 없다.
    형태소 기반 정밀화는 별도 과제로 남긴다.
    """
    from news.summarizer import _tokens

    if not group_articles:
        return 0.0
    kw_toks = set(_tokens(member["keyword"] or ""))
    if not kw_toks:
        return 0.0
    hits = 0
    for a in group_articles:
        art_toks = set(_tokens(f"{a.get('title', '')} {a.get('snippet', '')}"))
        if kw_toks <= art_toks:
            hits += 1
    return hits / len(group_articles)


def _representative_score(member: Dict, common_tokens: set, group_articles: List[Dict]) -> tuple:
    """display 대표 후보 선택용 복합 점수(튜플, 사전식 비교로 tie-break 다단계).

    사용자 확정 기준(2026-07-02): "그룹 공통 사건토큰 포함도"가 주 기준이되, raw 개수가
    아니라 "기사 분포율(coverage)"을 함께 본다. seed 여부는 보조/tie-breaker. 반환 튜플
    (내림차순 비교) 순서:
    1. generic-only 페널티(최상위 관문, 운영 hotfix 2026-07-03) — keyword가 일반
       서술어(신임/임명/발표 등) 토큰만으로 구성되면 -1. "홍석기 치안감" 그룹에서
       "신임"이 대표로 뽑히던 회귀를 막는다. 어떤 coverage/common_hits보다 앞서
       무조건 최하위로 민다.
    2. 검색의도 suffix 페널티(2026-07) — keyword 마지막 토큰이 뜻/의미/누구 등
       검색의도 suffix면 -1. "위홀 뜻"이 대표로 뽑혀 사용자에게 부자연스러운
       display가 노출되던 문제를 막는다. generic-only 다음으로 먼저 걸러야 하는
       결함이라 2번에 둔다("위홀"이라는 고유 토큰이 있어 generic-only는 아니므로
       별도 축 필요).
    3. keyword coverage 감점 — keyword 자체가 그룹 기사 절반 미만에만 등장하면(지엽
       엔티티) -1. "보스니아 헤르체고비나"처럼 일부 기사에만 나오는 다어절 엔티티를
       하드코딩 없이 데이터로 감점(Codex diff 리뷰 P2: 공통토큰 수보다 앞).
    4. 공통 사건토큰 포함 수 — coverage가 대등한 후보들 사이에서, 그룹 기사 절반
       이상에 걸쳐 등장하는 핵심어(common_tokens)를 많이 담을수록 대표성↑.
    5. broad 단독어 페널티(-1) — _TOO_BROAD_SINGLE_WORDS 단독 후보 감점.
    6. seed priority(daum>danawa>aux) — 대표성 동률일 때 원 seed 우선(tie-breaker).
    7. 구체성(keyword 토큰 수) — 그래도 동률이면 더 구체적인 표현 우선.
    8. 원 score — 최종 tie-breaker(신호 강도).
    """
    from news.summarizer import _tokens

    kw = member["keyword"]
    kw_toks = set(_tokens(kw))
    common_hits = len(kw_toks & common_tokens)
    generic_penalty = -1 if _is_generic_only_display(kw) else 0
    suffix_penalty = -1 if _ends_with_search_intent_suffix(kw) else 0
    coverage_penalty = -1 if _keyword_coverage(member, group_articles) < DISPLAY_TOKEN_MIN_COVERAGE else 0
    broad_penalty = -1 if kw.strip() in _TOO_BROAD_SINGLE_WORDS else 0
    return (
        generic_penalty,
        suffix_penalty,
        coverage_penalty,
        common_hits,
        broad_penalty,
        _seed_priority(member),
        len(kw_toks),
        member.get("score", 0.0),
    )


def _is_generic_only_display(keyword: str) -> bool:
    """keyword가 display 일반 서술어(_all_display_generic) 토큰만으로 구성됐는지.
    "신임"/"임명"/"발표" 단독, "신임 발표"처럼 일반어 조합도 True. 고유명사/사건어가
    하나라도 섞이면 False(예: "홍석기 치안감"·"국가수사본부장 임명").
    """
    from news.summarizer import _tokens

    # summarizer._tokens는 요약용 stopword인 "뉴스"를 제거한다. display 품질 판정은
    # 그보다 앞선 의미 계약이므로 exact generic label을 먼저 확인해 빈 토큰 우회를 막는다.
    if (keyword or "").strip() in _all_display_generic():
        return True
    toks = set(_tokens(keyword or ""))
    if not toks:
        return False
    return toks <= _all_display_generic()


# display 조합 근거로 인정할 최대 span(한 필드 안에서 검증 토큰 전부를 커버하는 토큰 윈도우).
# 압수수색 ~ 김영환처럼 한 title 안에 근접(수식어·조사 몇 개 사이)한 정상 맥락은 통과시키고,
# "금리 전망 ... (긴 서술) ... 인하"처럼 멀리 흩어진 토큰의 억지 결합은 배제하는 경계값.
_COMBO_SPAN_MAX_TOKENS = 6


def _combo_span_grounded(check_tokens: set, group_articles: List[Dict]) -> bool:
    """display 조합 후보(check_tokens)가 어느 한 기사의 **한 필드**(title 또는 snippet) 안에서
    **제한 거리(span) 이내**에 함께 등장하는지(ChatGPT P1 사전검토 2차, 2026-07-21).

    _display_grounded_by_single_unit(단순 단일-기사 공존)과 달리, "같은 기사에 있지만 멀리
    흩어진 토큰"만으로는 조합을 인정하지 않는다. 계약:
    - title과 snippet을 **합치지 않고 각 필드 별도**로 검사한다(단순 연결로 연속 phrase처럼
      취급 금지). 어느 한 필드가 span 조건을 만족하면 인정.
    - 한 필드 토큰 시퀀스에서 각 check token을 커버하는 위치들의 최소 span(max_index -
      min_index)이 _COMBO_SPAN_MAX_TOKENS 이내여야 한다.
    - 토큰 매칭은 _word_contains_token(조사결합/alias/sibling 복합) 계약을 재사용한다.

    이로써 "공수처 ... 김영환 지사 사무실 압수수색"(한 title 근접)은 통과하고, "금리 전망
    발표 이후 ... 인하 가능성"(멀리 흩어짐)이나 서로 다른 기사 분산은 배제된다.
    """
    import itertools
    from news.summarizer import _tokens

    for a in group_articles or []:
        for field in (a.get("title", ""), a.get("snippet", "")):
            toks = _tokens(field or "")
            if not toks:
                continue
            positions = {}
            ok = True
            for ct in check_tokens:
                pos = [i for i, t in enumerate(toks) if _word_contains_token(t, ct, check_tokens)]
                if not pos:
                    ok = False
                    break
                positions[ct] = pos
            if not ok:
                continue
            # 모든 토큰을 커버하는 최소 span이 상한 이내인지.
            best_span = min(
                max(combo) - min(combo) for combo in itertools.product(*positions.values())
            )
            if best_span <= _COMBO_SPAN_MAX_TOKENS:
                return True
    return False


def _build_display_keyword(members: List[Dict]) -> str:
    """same-issue merge된 후보들에서 display_keyword 생성.

    대표 선택 기준(사용자 확정 2026-07-02): merge group 안에서 "그룹 공통 사건토큰을
    가장 많이 포함한" 후보를 대표(best)로 삼는다(score 1위나 글자 수가 아님 — 이전
    구현은 len 내림차순이라 "보스니아 헤르체고비나"가 "월드컵"을 이기는 문제가 있었다).
    seed 출처/구체성/score는 tie-breaker로만 쓴다(_representative_score).

    canonical 보호(운영 hotfix 2026-07-03): members[0]은 group의 canonical(score 1위,
    movement 비교 기준). display 대표성 점수 1위가 generic-only(신임/임명 등)이면 사용자
    에게 의미가 없으므로, best로 채택하지 않고 canonical을 대신 쓴다. 최종 결과가 그래도
    generic-only가 되면 canonical로 강제 대체한다("홍석기 치안감" 그룹에서 "신임"이
    노출되던 회귀 방지). canonical의 coverage가 낮아도(기사가 canonical과 다른 표기를
    써서) "그룹 원 대표"라는 지위를 존중해 display fallback으로 항상 유지한다.

    조합형 처리:
    1. best가 이미 다른 후보 토큰을 포함하는 조합형이면(예: "김영환 압수수색") 그대로 사용.
    2. 그렇지 않으면 best에 없는 공통 사건토큰을 보완하는 second 후보를 붙여 조합
       (예: "월드컵" + "16강" → "월드컵 16강"). best 단독으로 공통토큰을 충분히
       담고 있으면(second가 새 공통토큰을 못 더하면) 단독 유지.
    3. 12~18자(DISPLAY_KEYWORD_MAX_LEN) 상한.
    """
    from news.summarizer import _tokens

    keywords = [m["keyword"] for m in members]
    canonical = members[0]["keyword"]
    group_articles = _display_group_articles(members)
    common_tokens = _display_common_event_tokens(members)

    # 대표성 점수 내림차순으로 후보 정렬(동률은 원래 순서=score 내림차순 유지).
    members_sorted = sorted(
        members, key=lambda m: _representative_score(m, common_tokens, group_articles), reverse=True
    )
    best = members_sorted[0]["keyword"]
    # best가 generic-only(신임/임명 등)면 대표로 쓰지 않고 canonical로 교체.
    if _is_generic_only_display(best):
        best = canonical
    best_toks = set(best)

    # best가 다른 후보들의 (문자) 토큰을 이미 포함하는 조합형 표현인지 확인
    covers_others = all(
        (not set(k) - best_toks) or k == best for k in keywords if k != best
    )
    if covers_others and len(members_sorted) > 1:
        return best[:DISPLAY_KEYWORD_MAX_LEN]

    # 조합 대상 second 후보 선택. 두 경로 모두 coverage 낮은 지엽 엔티티(상대국/기업명
    # 등)는 second로 붙이지 않는다(Codex diff 재리뷰 P2: 공통토큰 보완 경로에도 동일
    # 방어 필요 — best="월드컵" + 남은 공통토큰 "16강"을 "16강 보스니아"가 담더라도,
    # 그 후보가 일부 기사에만 등장하면 "월드컵 16강 보스니아"가 되어선 안 됨). 유효
    # 기사가 2건 미만이라 coverage 신호가 없는 그룹(같은 기사 1건 공유 merge)에서는
    # coverage 감점이 신뢰할 수 없으므로 방어를 끄고 기존처럼 맥락 후보를 붙인다.
    low_coverage_group = len(group_articles) < 2

    def _second_allowed(m: Dict) -> bool:
        k = m["keyword"]
        if k in best or best in k:
            return False
        # generic-only 후보(신임/임명 등)는 보완 표기로도 붙이지 않는다(hotfix 2026-07-03).
        if _is_generic_only_display(k):
            return False
        # 검색의도 suffix 후보(뜻/의미/누구 등)도 보완 표기로 붙이지 않는다(2026-07).
        if _ends_with_search_intent_suffix(k):
            return False
        if not low_coverage_group and _keyword_coverage(m, group_articles) < DISPLAY_TOKEN_MIN_COVERAGE:
            return False
        return True

    best_word_toks = set(_tokens(best))
    remaining_common = common_tokens - best_word_toks
    second = None
    #  (a) best가 담지 못한 공통 사건토큰을 가장 많이 보완하는 후보 우선.
    if remaining_common:
        best_gain = 0
        for m in members_sorted[1:]:
            if not _second_allowed(m):
                continue
            gain = len(set(_tokens(m["keyword"])) & remaining_common)
            if gain > best_gain:
                best_gain = gain
                second = m["keyword"]
    #  (b) 공통토큰 보완 후보가 없으면(zero-confidence 등) 대표성 순 다음 허용 후보를
    #      붙인다 — 단독 일반어("압수수색")가 아니라 맥락(인명 등)을 함께 드러내기 위함.
    if second is None:
        for m in members_sorted[1:]:
            if not _second_allowed(m):
                continue
            second = m["keyword"]
            break
    if second is None:
        return _display_or_canonical(best, canonical)

    # ── 중복 제거 재설계 v2(ChatGPT P1 사전검토, 2026-07-21): entity/일반명사 구분(person
    #    판정)을 없애고, "best와 겹치는 second 토큰 제거 + 최종 조합의 단일-기사 공존 근거 검증"
    #    단일 규칙으로 통일한다.
    #
    #    이전 v1은 _entity_anchor_tokens(사람명/영문 판정)로 entity 되풀이만 떼어내고 나머지는
    #    원문 근거로 검증하려 했으나, _looks_like_person_name이 "2~4자 한글"을 거의 다 인물명으로
    #    판정해 '금리'/'카드'/'유출' 같은 일반명사도 entity anchor로 잡혔다. 그러면 "금리 전망"+
    #    "금리 인하"에서 '금리'가 제거된 뒤 남은 '인하'가 "순수 보완"으로 처리돼 원문 근거 검증을
    #    건너뛰고 "금리 전망 인하"(원문에 없는 배열)가 생성될 수 있었다(설계 구멍). person 판정을
    #    제거하면 이 구멍이 사라지고, 특정 회사명/제품명 하드코딩도 불필요하다.
    #
    #  규칙: second에서 best와 토큰이 겹치는 부분(되풀이)을 제거해 residual만 남긴다.
    #    - residual이 없으면(second가 best 토큰의 되풀이뿐) 새 정보 없음 → best 단독.
    #    - residual이 있으면 "best + residual"의 전체 검증 토큰이 **어느 한 기사의 한 필드
    #      안에서 제한 거리(span) 이내에 함께 등장**할 때만 채택한다(_combo_span_grounded,
    #      ChatGPT P1 사전검토 2차). 근거가 없으면 best 단독.
    #      "같은 기사 공존"만으로는 부족하다(단순 공존이면 "금리 전망 발표 ... 인하 가능성"처럼
    #      멀리 흩어진 토큰도 억지 결합됨). 대신 근접 span을 요구해:
    #        · "공수처, 김영환 지사 사무실 압수수색"(한 title 근접) → 통과(어순 무관)
    #        · "금리 전망" + "인하"(다른 기사 분리 또는 멀리 흩어짐) → 차단
    #      title/snippet은 합치지 않고 각 필드 별도로 검사한다(단순 연결로 연속 phrase처럼
    #      취급 금지). 원문에 없는 새 토큰 순서·의미 배열은 생성하지 않는다.
    best_word_set = set(_tokens(best))
    residual = [t for t in _tokens(second) if t not in best_word_set]
    if not residual:
        return _display_or_canonical(best, canonical)

    candidate = f"{best} {' '.join(residual)}"
    if len(candidate) <= DISPLAY_KEYWORD_MAX_LEN:
        cand_check = _invariant_check_tokens(candidate)
        if cand_check and _combo_span_grounded(cand_check, group_articles):
            return _display_or_canonical(candidate, canonical)
    return _display_or_canonical(best, canonical)


def _display_or_canonical(display: str, canonical: str) -> str:
    """최종 display 후보가 generic-only(신임/임명 등)거나 검색의도 suffix(뜻/의미 등)로
    끝나면 canonical로 대체한다. canonical 자체가 같은 문제를 가진 극단 케이스에는
    그대로 canonical을 쓴다(그 이상 나은 선택지가 없음). DISPLAY_KEYWORD_MAX_LEN 상한 적용.
    """
    if _is_generic_only_display(display) or _ends_with_search_intent_suffix(display):
        return canonical[:DISPLAY_KEYWORD_MAX_LEN]
    return display[:DISPLAY_KEYWORD_MAX_LEN]


def dedupe_and_merge(ranked: List[Dict]) -> List[Dict]:
    """score 계산된 ranked 리스트에 유사 키워드 dedupe + same-issue merge 적용.

    처리 순서(요구사항 순서 반영):
    1. 유사 키워드 dedupe(문자열/기관명 기준) — score 더 높은 대표만 남기고
       제거된 keyword는 related_keywords에 텍스트로 보존(score 합산 없음).
    2. same-issue merge(article overlap 기준) — merge group의 keyword는 canonical
       (그룹 내 최고 score 후보의 원래 keyword) 유지, display_keyword로 조합 표기.

    반환: ranked와 동일 스키마 + related_keywords/aliases/display_keyword/merge_reason/
          sources(원본 후보 sources 보존, builder lookup 방어) 등 optional 필드 추가.
    입력 순서(score 내림차순)를 유지한 채 selected-set을 누적하는 단일 패스로 처리해
    재중복을 방지한다(§7-2).
    """
    result: List[Dict] = []
    consumed = set()  # 이미 dedupe/merge에 흡수된 keyword
    # 관찰 전용(랭킹 영향 없음): 공유 URL 근거가 있었지만 잔여 근거 교차확인 실패로
    # merge하지 않은 pair 후보. fixed-point 특성상 나중에 다른 멤버를 통해 같은 그룹이
    # 될 수 있으므로 여기서는 수집만 하고, 전체 merge 완료 후 "최종적으로 다른 그룹에
    # 남은 pair"만 집계 로그로 남긴다(Codex 계획리뷰 P2/P3).
    declined_bridge_pairs: Dict[frozenset, int] = {}

    for i, item in enumerate(ranked):
        kw = item["keyword"]
        if kw in consumed:
            continue

        group = [item]
        consumed.add(kw)

        # --- 1) 유사 키워드 dedupe: 그룹 내 이미 흡수된 모든 멤버와 비교(transitive).
        #    대표 keyword 하나와만 비교하면 "배재고"↔"배재고야구부논란"처럼 그룹의
        #    다른 멤버와만 유사한 후보를 놓칠 수 있어 fixed-point까지 반복한다.
        keyword_dedupe_count = 0
        changed = True
        while changed:
            changed = False
            for other in ranked[i + 1:]:
                okw = other["keyword"]
                if okw in consumed:
                    continue
                if any(_is_similar_keyword(m["keyword"], okw) for m in group):
                    group.append(other)
                    consumed.add(okw)
                    keyword_dedupe_count += 1
                    changed = True

        # --- 2) same-issue merge: article overlap 높은 것들 흡수.
        #    그룹 내 "각 멤버와 개별 비교"(OR 조건)로 판정한다 — 여러 멤버의 기사를
        #    하나로 합쳐서 비교(pool 방식)하면 무관한 기사가 섞였을 때 union이
        #    커져 실제로 겹치는 멤버가 있어도 Jaccard가 희석돼 놓칠 수 있다(재발
        #    방지: A가 무관 기사 다수를 갖고 B와만 겹치는 C가 있으면, pool 합산
        #    방식은 C를 놓치지만 멤버별 개별 비교는 B-C overlap을 그대로 잡는다).
        #    새 멤버가 그룹에 들어올 때마다 그 멤버도 비교 대상에 포함해
        #    fixed-point까지 반복한다(transitive).
        article_merge_count = 0
        changed = True
        while changed:
            changed = False
            for other in ranked[i + 1:]:
                okw = other["keyword"]
                if okw in consumed:
                    continue
                matched = any(_is_same_issue(m, other) for m in group)
                if matched:
                    group.append(other)
                    consumed.add(okw)
                    article_merge_count += 1
                    changed = True
                else:
                    # 관찰 수집: 공유 URL이 있었는데 merge하지 않은 모든 (멤버, other)
                    # pair를 기록한다(첫 멤버에서 멈추면 pair 단위 관측이 왜곡됨 —
                    # Codex diff 리뷰 P3). 중복 수집은 frozenset 키가 흡수하고,
                    # 최종 판정은 함수 끝 reconcile에서.
                    for m in group:
                        shared_urls = _shared_evidence_urls(m, other)
                        if shared_urls:
                            declined_bridge_pairs[
                                frozenset((m["keyword"], okw))
                            ] = len(shared_urls)

        if len(group) == 1:
            merged = dict(item)
            merged.setdefault("related_keywords", [])
            merged.setdefault("aliases", [])
            # 단독 후보는 display_keyword = kw(기존 동작 유지 — 길이 절단하지 않음,
            # Codex diff 재리뷰 P3: _build_display_keyword를 태우면 18자 초과 정상
            # 단독 키워드가 잘리는 동작 변경이 생김). 단, generic-only("신임" 등) 단독은
            # 그대로 노출하지 않는 게 맞지만 단독이라 대체 후보가 없으므로 kw를 유지한다
            # (merge group 내부의 generic 대표 회귀는 _build_display_keyword에서 이미
            # 방어됨 — 단독 generic이 상위로 오는 경우는 관찰 항목).
            merged["display_keyword"] = kw
            result.append(merged)
            continue

        # score 최고 항목을 canonical 대표로(그룹은 이미 score 내림차순이므로 group[0]).
        primary = group[0]
        related = [m["keyword"] for m in group[1:]]
        merged = dict(primary)
        merged["related_keywords"] = related
        merged["aliases"] = related
        merged["display_keyword"] = _build_display_keyword(group)
        # merge_reason: article overlap으로 흡수된 멤버가 하나라도 있으면 same_article_cluster,
        # 전부 문자열/기관명 유사도만으로 흡수됐으면 similar_keyword로 정확히 구분(Codex P3 반영).
        merged["merge_reason"] = "same_article_cluster" if article_merge_count > 0 else "similar_keyword"
        result.append(merged)

    # 관찰 로그(랭킹 영향 없음): 수집된 declined bridge 후보 중 "최종적으로 서로 다른
    # 그룹에 남은" pair만 보고한다 — fixed-point에서 나중에 같은 그룹으로 합류한 pair는
    # bridge 보류가 아니다. run당 1회 집계 경고(pair별 남발 방지).
    if declined_bridge_pairs:
        membership: Dict[str, int] = {}
        for gi, merged_item in enumerate(result):
            membership[merged_item["keyword"]] = gi
            for rel in merged_item.get("related_keywords") or []:
                membership[rel] = gi
        separated = []
        for pair, url_count in declined_bridge_pairs.items():
            kws = sorted(pair)
            ga, gb = membership.get(kws[0]), membership.get(kws[1])
            if ga is not None and gb is not None and ga != gb:
                separated.append((kws[0], kws[1], url_count))
        if separated:
            logger.warning(
                "[news] same-issue bridge 보류 %d쌍(공유 URL 있으나 잔여 근거 미교차) 예시=%s",
                len(separated), separated[:5],
            )

    return result


# === singleton sense-mixing display 보정(2026-07) ===
# dedupe_and_merge()의 단독(merge group size 1) 경로는 display_keyword=keyword를
# 그대로 유지한다(길이 절단 방지 — 위 주석 참고). 하지만 "위홀 뜻"처럼 merge가 아예
# 안 일어난 단독 keyword도 검색의도 suffix + 표시 기사와의 의미 불일치 문제를 그대로
# 가질 수 있어, dedupe_and_merge() 이후 별도 함수로 좁게 보정한다(merge group 로직
# 자체는 건드리지 않음 — Codex review-only: singleton 전용 좁은 예외로 제한).
def _boost_short_generic_singleton_display(item: Dict, kw: str) -> Dict:
    """짧은 일반 생활명사 단독(singleton) keyword의 display를 표시 기사에서 keyword
    바로 앞에 반복 등장하는 영문/숫자 modifier로 보강한다(2026-07 운영 관찰: "안경"
    단독 display가 기사에서는 "AI 안경"으로 반복). 보강 대상이 아니면 item 원형 반환.

    canonical keyword(movement 비교 기준)는 건드리지 않고 display_keyword만 바꾼다.
    검색의도 suffix 경로(_resolve_singleton_display)에서 suffix가 아닌 keyword에만
    호출된다. 아래를 모두 만족할 때만 보강한다:
    1. keyword가 단일 토큰이고 글자 수 <= SHORT_GENERIC_SINGLETON_MAX_LEN(짧음).
    2. keyword가 generic-only(신임/수사 등)가 아님 — 그건 exclude_generic_singletons가
       별도 처리하므로 여기서 중복 개입하지 않는다.
    3. keyword 토큰이 표시 기사 절반 이상(coverage>=DISPLAY_TOKEN_MIN_COVERAGE)에 등장.
    4. keyword 토큰 "바로 앞 위치"에 오는 modifier가, keyword 등장 기사 중 절반 이상에서
       동일하게 반복(prev-token 반복률). "태풍 북상"류 뒤 서술어는 안 잡히고, "AI 안경"
       처럼 앞 수식어만 잡힌다. tie는 대표 기사 title 등장 순서로 고정.
    5. modifier에 [A-Za-z0-9]가 최소 1개 포함(사용자 확정): "제주 태풍"/"은행 금리"
       같은 순수 한글 문맥어 과구체화를 이번 범위에서 차단. modifier가 generic-only/
       검색의도 suffix거나 keyword를 문자로 포함(중복형 "오픈AI AI")하면 제외.

    보강 결과는 "{modifier} {keyword}" 한 조합뿐이다(뒤 사건어 "체험/몰카"는 붙이지
    않음 — 혼합 cluster 과구체화 방지). 18자 초과면 원형 유지(토큰 중간 절단 방지).

    prev-token은 실제 화면에 노출되는 표시 기사(dedup → filter_articles_for_display →
    [:ARTICLES_MAX], builder/invariant와 동일 집합) 기준으로 집계한다(Codex diff P1:
    news_meta.articles 원본을 쓰면 중복/미표시 기사가 majority를 왜곡할 수 있음).
    """
    from news.summarizer import _tokens
    from news.dedup import dedup_articles
    from news.candidates import filter_articles_for_display
    from news.builder import ARTICLES_MIN, ARTICLES_MAX

    kw = (kw or "").strip()
    kw_toks = _tokens(kw)
    # 1. 단일 토큰 + 짧은 keyword만 대상.
    if len(kw_toks) != 1 or len(kw) > SHORT_GENERIC_SINGLETON_MAX_LEN:
        return item
    kw_tok = kw_toks[0]
    # 2. generic-only는 별도 방어(exclude_generic_singletons)가 처리 — 개입 안 함.
    if _is_generic_only_display(kw):
        return item

    news_meta = item.get("news_meta") or {}
    displayed = filter_articles_for_display(
        dedup_articles(news_meta.get("articles") or []), min_count=ARTICLES_MIN
    )[:ARTICLES_MAX]
    articles = displayed
    # 기사별 title 토큰열(순서 유지) — prev-token 위치 판정에 순서가 필요하다.
    title_token_lists = [_tokens(a.get("title", "") or "") for a in articles]
    kw_article_toks = [toks for toks in title_token_lists if kw_tok in toks]
    # 3. keyword가 표시 기사 절반 이상에 등장해야 근거가 된다.
    if not articles or (len(kw_article_toks) / len(articles)) < DISPLAY_TOKEN_MIN_COVERAGE:
        return item

    # 4. keyword 토큰 바로 앞(prev) modifier를 기사 단위로 집계한다(발생 횟수가 아니라
    #    기사 hit 수 — 한 기사가 modifier 하나에 최대 1표). 한 기사에 keyword가 여러 번
    #    나와도 title 첫 등장의 prev만 본다(의도적 단순화, Codex diff P2): title은 짧아
    #    핵심 표기가 앞에 오는 게 일반적이고, 첫 등장 prev가 대표 수식어일 확률이 높다.
    #    "안경 시장, AI 안경 공개"처럼 첫 등장에 수식어가 없으면 근거 부족으로 보강하지
    #    않는(보수적) 방향이라 오보강보다 안전하다.
    prev_hits: Dict[str, int] = {}
    prev_order: Dict[str, int] = {}
    order_seq = 0
    for toks in kw_article_toks:
        idx = toks.index(kw_tok)
        if idx == 0:
            continue  # 맨 앞 → 앞 수식어 없음.
        prev = toks[idx - 1]
        prev_hits[prev] = prev_hits.get(prev, 0) + 1
        if prev not in prev_order:
            prev_order[prev] = order_seq
            order_seq += 1

    if not prev_hits:
        return item

    threshold = len(kw_article_toks) * DISPLAY_TOKEN_MIN_COVERAGE

    def _modifier_ok(mod: str) -> bool:
        # 5. 영문/숫자 포함 필수 + generic/suffix/중복형 제외.
        if not any(ch.isascii() and ch.isalnum() for ch in mod):
            return False
        if _is_generic_only_display(mod) or _ends_with_search_intent_suffix(mod):
            return False
        # "오픈AI"+"AI" 같은 중복형 차단. 영문 case 무시(Codex diff P3: "Openai AI"류).
        if kw_tok.casefold() in mod.casefold():
            return False
        return True

    # modifier 채택 조건: (1) 반복률 threshold(keyword 등장 기사의 절반 이상)와 함께
    # (2) 절대 hit 수 >= DISPLAY_ARTICLES_MIN(Codex diff 재리뷰 P1). 절대 근거가 얕으면
    # 보강 후 exclude_insufficient_display_articles(display_articles<2 drop)에 걸려 원래
    # "안경"이면 살아남았을 후보가 탈락해 Top10 개수를 깎을 수 있다. modifier가 표시
    # 기사 최소 DISPLAY_ARTICLES_MIN건에 등장하면 보강된 "{modifier} {keyword}"도 그만큼의
    # 표시 기사에 정합해 drop되지 않는다.
    min_support = max(threshold, DISPLAY_ARTICLES_MIN)
    # 반복률(기사 hit 수) 내림차순, tie는 대표 title 등장 순서(prev_order) 오름차순.
    candidates = sorted(
        (m for m, h in prev_hits.items() if h >= min_support and _modifier_ok(m)),
        key=lambda m: (-prev_hits[m], prev_order[m]),
    )
    if not candidates:
        return item
    modifier = candidates[0]

    boosted = f"{modifier} {kw}"
    if len(boosted) > DISPLAY_KEYWORD_MAX_LEN:
        return item  # 상한 초과 → 원형 유지(토큰 중간 절단 방지).

    item = dict(item)
    item["display_keyword"] = boosted
    return item


def _specify_entity_singleton_display(item: Dict, kw: str) -> Optional[Dict]:
    """entity 단독어를 "엔티티 + 지배적 사건"으로 구체화한다(G, 2026-07). 근거 부족 시 None.

    조건(모두 만족해야 구체화):
    - news_meta.keyword_kind == 'entity' (한화/신천지류 단일 엔티티).
    - has_dominant_event=True 이고 dominant_event_tokens(keyword 제외 공통 사건토큰) 존재.
    - 대표기사 title에 그 사건토큰이 실제 등장(근거 없는 조합 방지).
    구체화 결과는 "엔티티 사건토큰1[ 사건토큰2]"(상한 DISPLAY_KEYWORD_MAX_LEN). 만들 수
    없으면 None을 반환해 호출부가 기존 경로를 타게 한다(억지 대체 없음 — 사용자 지시:
    충분한 근거 없으면 구체화하지 않고, 근거가 아예 없으면 B2가 Top10에서 제외).
    """
    from news.summarizer import _tokens

    news_meta = item.get("news_meta") or {}
    if news_meta.get("keyword_kind") != "entity":
        return None
    if not news_meta.get("has_dominant_event"):
        return None
    event_tokens = news_meta.get("dominant_event_tokens") or []
    if not event_tokens:
        return None

    representative = news_meta.get("representative_article") or {}
    rep_toks = [t for t in _tokens(representative.get("title") or "") if t in set(event_tokens)]
    if not rep_toks:
        return None  # 대표기사 title에 사건토큰이 없으면 근거 부족 → 구체화 안 함.

    # 대표 title 등장 순서로 최대 2개 사건토큰을 붙인다.
    picked = list(dict.fromkeys(rep_toks))[:2]
    candidate = f"{kw} {' '.join(picked)}".strip()
    if len(candidate) > DISPLAY_KEYWORD_MAX_LEN:
        candidate = f"{kw} {picked[0]}".strip()
    if len(candidate) > DISPLAY_KEYWORD_MAX_LEN or candidate == kw:
        return None

    result = dict(item)
    result["display_keyword"] = candidate
    return result


def _resolve_singleton_display(item: Dict) -> Dict:
    """단독(merge 안 된) item의 display_keyword를 sense-mixing 관점에서 재검토한다.

    아래 조건을 모두 만족할 때만 표시 기사 공통 토큰 기반으로 재구성한다(그 외에는
    기존 keyword 그대로 유지 — 정상 singleton 회귀 없음):
    1. keyword 마지막 토큰이 검색의도 suffix(뜻/의미/누구 등)와 정확히 일치.
    2. suffix를 제외한 keyword의 non-generic 토큰이 최소 1개 이상 존재하고
       (Codex review-only P1: 빈 집합이면 vacuous true가 되어 "뜻"/"의미" 단독
       키워드까지 재구성 대상이 될 위험이 있어 반드시 1개 이상을 요구한다),
       그 토큰들이 모두 표시 기사에 최소 1회 등장.
    3. 표시 기사(news_meta.articles)의 공통 토큰(coverage>=0.5)으로 대체 표기를
       구성할 수 있음(대체 후보가 없으면 원래 keyword 유지 — 억지 대체 없음).
    """
    from news.summarizer import _tokens

    kw = item.get("keyword", "")
    if "display_keyword" not in item:
        item = dict(item)
        item["display_keyword"] = kw
    if not _ends_with_search_intent_suffix(kw):
        # entity 단독어(한화/신천지 등)가 dominant event를 가지면 "엔티티 + 사건"으로
        # 구체화한다(G, 2026-07: 한화 → 한화 7연패). 근거가 충분할 때만(has_dominant_event
        # AND dominant_event_tokens 존재) 시도하고, 실패하면 아래 기존 보강 경로로 넘어간다.
        specified = _specify_entity_singleton_display(item, kw)
        if specified is not None:
            return specified
        # 검색의도 suffix가 아니면 "짧은 일반 생활명사 단독" 보강 경로를 시도한다
        # (2026-07: "안경" 단독 → "AI 안경"). 대상이 아니면 원형 그대로 반환.
        return _boost_short_generic_singleton_display(item, kw)

    # suffix 어절(마지막 공백 구분 단어)을 제외한 나머지 문자열을 토큰화한다.
    # summarizer._tokens는 1글자 토큰("뜻")을 만들지 않으므로 kw 전체를 토큰화한
    # 뒤 마지막 원소를 자르면(kw_toks[:-1]) suffix가 애초에 토큰에 없을 때 stem이
    # 통째로 사라진다("위홀 뜻" → ["위홀"] → [:-1] → [] 버그) — 반드시 suffix 어절을
    # 문자열에서 제거한 나머지로 stem을 계산해야 한다.
    stem_text = " ".join(kw.split()[:-1])
    stem_toks = set(_tokens(stem_text)) - _all_display_generic()
    if not stem_toks:
        return item  # suffix 제외 나머지가 없음(예: "뜻" 단독) → 재구성 대상 아님

    news_meta = item.get("news_meta") or {}
    articles = news_meta.get("articles") or []
    article_texts = [f"{a.get('title', '')} {a.get('snippet', '')}" for a in articles]
    article_token_sets = [set(_tokens(t)) for t in article_texts]

    # stem 토큰이 전부 표시 기사에 최소 1회 등장해야 함(근거 없는 재구성 방지).
    if not all(any(st in toks for toks in article_token_sets) for st in stem_toks):
        return item

    common_tokens = _common_event_tokens_from_articles(articles)
    if not common_tokens:
        return item

    representative = news_meta.get("representative_article") or {}
    rep_title = representative.get("title") or ""
    rep_toks = [t for t in _tokens(rep_title) if t in common_tokens]
    if not rep_toks:
        return item

    candidate = " ".join(dict.fromkeys(rep_toks))[:DISPLAY_KEYWORD_MAX_LEN]
    if not candidate or _ends_with_search_intent_suffix(candidate) or _is_generic_only_display(candidate):
        return item

    item = dict(item)
    item["display_keyword"] = candidate
    return item


def resolve_singleton_displays(items: List[Dict]) -> List[Dict]:
    """merge group size 1(단독 후보)에만 _resolve_singleton_display를 적용한다.
    merge된 group(related_keywords 존재)은 대상이 아니다(_build_display_keyword가
    이미 처리) — dedupe_and_merge() 직후, enforce_display_article_consistency() 직전에
    호출한다."""
    result = []
    for item in items:
        if item.get("related_keywords"):
            result.append(item)
            continue
        result.append(_resolve_singleton_display(item))
    return result


def exclude_generic_singletons(merged: List[Dict]) -> tuple:
    """merge group을 이루지 못한 singleton 후보 중 keyword가 generic-only(수사/조사/
    신임 등 일반 행위·인사 서술어만으로 구성)인 항목을 최종 후보에서 제외한다.

    운영 관찰(2026-07-03): canonical=display="수사" singleton이 그대로 news_top에
    노출됨 — merge group의 generic 대표는 _build_display_keyword가 방어하지만
    singleton 경로(display=keyword)는 방어를 타지 않았다. singleton generic은 이슈
    식별이 불가능한 표기이므로 filler로도 쓰지 않는다(제외로 개수가 줄면 줄어든
    채로 보고). "태풍"처럼 generic 집합에 없는 명확한 이슈 단독어는 통과한다.
    merge group에 흡수된 generic keyword는 related_keywords로 유지되므로 무관.

    dedupe_and_merge() 이후, select_top() 이전에 적용한다.
    반환: (kept, excluded_keywords)
    """
    kept: List[Dict] = []
    excluded: List[str] = []
    for item in merged:
        group_size = 1 + len(item.get("related_keywords") or [])
        if group_size == 1 and _is_generic_only_display(item.get("keyword", "")):
            excluded.append(item.get("keyword", ""))
            continue
        kept.append(item)
    return kept, excluded


# === broad category(업종/분야) generic singleton 탐지 — 1차: logging first ===
# 운영 관찰(2026-07-09): "건설"/"게임"처럼 순수 한글 업종/분야어가 단독(singleton)으로
# final에 노출됐는데, 표시 기사는 서로 다른 주체(현대건설/대우건설)의 별개 사건 묶음이었다.
# _is_generic_only_display(신임/수사 등 행위·인사 서술어)에도, §0-4 영문/숫자 modifier
# 보강(_boost_short_generic_singleton_display)에도 안 걸려 어떤 방어도 타지 않는다.
#
# 이 1차 작업은 **탐지·로그만** 한다(제외/강등/순위 변경 없음). Codex 계획 review-only:
# "title 첫 토큰 = 주체" 추출은 [속보]/인용/날짜/기관어 접두 등에 취약해 hard exclude
# 오탐 위험이 크므로, 먼저 관찰 로그로 운영 1~2회 데이터를 쌓은 뒤 제외/강등 기준을
# 별도 PR에서 확정한다. 아래 subject 추출과 dispersion 판정은 전부 shadow(관찰) 전용이며
# 절대 final 결과에 반영하지 않는다.
#
# 주의: _TOO_BROAD_SINGLE_WORDS(41행)와 역할이 다르다 — 그건 substring merge 억제 +
# 대표 선택 감점(tie-breaker) 전용이고, 이 집합은 singleton 탐지(관찰) 전용이다. 이슈
# 단독어(태풍/주담대/금리)는 업종/분야어가 아니라 절대 넣지 않는다(false positive 방어).
_BROAD_CATEGORY_WORDS = {
    "건설", "게임", "금융", "사업", "산업", "기업", "시장", "기술",
    "정책", "병원", "투자", "지원", "공급", "운영",
}

# 주체 추출에서 접두 노이즈로 흔한 토큰(shadow 판정 전용). title 첫머리에 오지만 실제
# 이슈 주체가 아닌 기관어/시점어/서술 접두어(Codex 계획 리뷰 P1: 첫 토큰이 주체가
# 아닌 케이스 방어). 이 목록은 관찰 dispersion 정확도만 높이며 hard exclude에는 쓰지 않는다.
_SUBJECT_NOISE_PREFIXES = {
    "속보", "단독", "특징주", "포토", "영상", "오늘", "내일", "어제", "정부",
    "업계", "국내", "해외", "이번", "관련", "종합",
}


def _extract_subject_token(title: str, kw_tok: str) -> Optional[str]:
    """기사 title에서 "주체 후보" 토큰 1개 추출(shadow dispersion 판정 전용).

    title 첫 토큰을 주체 후보로 본다(대개 주어 "현대건설,"/"대우건설,"). 단:
    - keyword 토큰과 같으면 그 다음 토큰을 본다("건설안전…"이 첫 토큰이면 keyword 자신).
    - 접두 노이즈(_SUBJECT_NOISE_PREFIXES: 속보/정부/업계 등)면 건너뛰고 다음 토큰.
    없으면 None. _tokens는 [속보]/인용부호/날짜 기호를 이미 제거하므로 기호 접두는
    자연히 걸러진다(정규식 `[가-힣A-Za-z0-9]{2,}`). 이건 관찰용 근사이지 정확한 NER이
    아니며(Codex P1), hard exclude 근거로 쓰지 않는다.
    """
    from news.summarizer import _tokens

    toks = _tokens(title or "")
    for tok in toks:
        if tok == kw_tok:
            continue
        if tok in _SUBJECT_NOISE_PREFIXES:
            continue
        return tok
    return None


def detect_broad_category_singletons(items: List[Dict]) -> List[Dict]:
    """broad category generic singleton 후보를 **탐지만** 한다(제외/강등/순위 변경 없음).

    반환값은 관찰용 진단 리스트로, 호출부(main.py)는 이를 로그로만 남기고 파이프라인
    결과에는 절대 반영하지 않는다. 대상 판정(모두 만족):
    - group_size == 1(merge 안 된 단독. related_keywords 없음).
    - keyword가 단일 토큰(_tokens 길이 1).
    - keyword가 _BROAD_CATEGORY_WORDS에 포함(순수 업종/분야어).
    - display_keyword == keyword(§0-4 "AI 안경" 보강 결과물은 제외 — display가 이미
      구체화됐으면 관찰 대상 아님, Codex 계획 P2).

    각 후보에 대해 표시 기사(dedup→filter_articles_for_display→[:ARTICLES_MAX], §0-4/
    builder/invariant와 동일 집합) 기준 subject dispersion(주체 분산)을 shadow로 계산해
    진단에 담는다. dispersion 판정 규칙(shadow):
    - 서로 다른 주체 후보가 2개 이상이고, 최다 주체도 표시 기사의 과반(>50%) 미만이면
      "dispersed=True"(서로 다른 주체가 keyword 하나로 묶임 의심).
    - 단일 주체가 과반이면 dispersed=False(동일 회사/작품 반복 → 정상 이슈 가능).
    - 표시 기사 2건 미만이면 dispersed=None(판정 불가, 보수적).
    """
    from news.summarizer import _tokens
    from news.dedup import dedup_articles
    from news.candidates import filter_articles_for_display, build_display_articles
    from news.builder import ARTICLES_MIN, ARTICLES_MAX

    diagnostics: List[Dict] = []
    for item in items:
        if item.get("related_keywords"):
            continue  # merge group은 대상 아님(_build_display_keyword가 처리)
        kw = (item.get("keyword", "") or "").strip()
        kw_toks = _tokens(kw)
        if len(kw_toks) != 1 or kw not in _BROAD_CATEGORY_WORDS:
            continue
        display = item.get("display_keyword") or kw
        if display != kw:
            continue  # §0-4 등으로 이미 display가 구체화됨 → 관찰 대상 아님
        kw_tok = kw_toks[0]

        # 실제 상세 팝업 노출 기사와 동일 집합으로 subject를 집계한다(Codex diff P2):
        # builder가 display_articles를 만들 때 filter_articles_for_display 이후
        # build_display_articles(anchor 재확인)를 한 번 더 통과시키므로, 관찰 로그가
        # 실제 노출 기사와 어긋나지 않도록 여기서도 동일 단계를 밟는다. 대상은
        # display==keyword이므로 effective_keyword=kw.
        news_meta = item.get("news_meta") or {}
        filtered = filter_articles_for_display(
            dedup_articles(news_meta.get("articles") or []), min_count=ARTICLES_MIN
        )[:ARTICLES_MAX]
        articles = build_display_articles(
            kw, filtered, news_meta.get("representative_article")
        )

        subjects: List[str] = []
        for a in articles:
            subj = _extract_subject_token(a.get("title", "") or "", kw_tok)
            if subj:
                subjects.append(subj)

        subject_counts: Dict[str, int] = {}
        for s in subjects:
            subject_counts[s] = subject_counts.get(s, 0) + 1

        n_articles = len(articles)
        if n_articles < 2:
            dispersed = None  # 판정 불가(보수적)
        else:
            top_subject_hits = max(subject_counts.values()) if subject_counts else 0
            distinct = len(subject_counts)
            dispersed = distinct >= 2 and top_subject_hits <= n_articles / 2

        diagnostics.append({
            "keyword": kw,
            "display_keyword": display,
            "articles": n_articles,
            "subject_dist": dict(sorted(
                subject_counts.items(), key=lambda kv: (-kv[1], kv[0])
            )),
            "shadow_dispersed": dispersed,
        })
    return diagnostics


# === 단일 토큰 keyword 동음이의(homonym entity) sense 탐지 — 1차: logging first (issue #2) ===
# known limitation(issue #2): keyword core가 단일 토큰("워홀")이고 non-primary cluster가
# **동일한 문자열 토큰**을 title/snippet에 포함하는 동음이의 케이스(앤디 워홀 전시 기사)는
# mark_off_primary_sense의 keyword 매칭(common & kw_toks)이 same_sense로 오판하고,
# _display_anchor_allowed의 단일 토큰 예외(장동건류 보존, off-sense 체크보다 먼저 평가)도
# 통과해 display_articles에 혼입된다. PR #1에서 토큰 집합 기반 3가지 접근이 모두 "장동건"
# 회귀 테스트를 깨뜨려(같은 개체의 표현 차이 vs 다른 개체의 동음이의를 집합만으로 구분
# 불가) 이 잔여 리스크로 남았다.
#
# 판별 신호는 토큰 집합이 아니라 **dominant collocation**: 앤디워홀 클러스터에서 "워홀"의
# exact 토큰 등장은 항상 "앤디" 바로 뒤(합성 고유명의 일부)이고 그 partner("앤디")는
# primary cluster 기사에 전혀 등장하지 않는다. 반면 "장동건" 클러스터는 인접 토큰이
# 기사마다 제각각이라 일관 partner가 없다(§0-4 prev-token modifier 보강과 같은 신호 계열).
#
# 이 1차 작업은 **탐지·로그만** 한다(제외/강등/순위 변경 없음). Codex 계획 review-only
# 3라운드 결론: prev-token 일관성만으로 hard exclude하면 역할명 접두("배우 장동건")류
# 오탐 위험이 있어, 먼저 관찰 로그로 운영 데이터를 쌓은 뒤 _display_anchor_allowed 단일
# 토큰 예외의 자격 조건 소비(2차 PR)를 판단한다. 진단은 반환 리스트에만 존재하며 입력
# items/article dict/news_meta에는 어떤 필드도 추가하지 않는다(builder 경유 저장 payload
# 누출을 구조적으로 차단 — Codex 2차 계획 리뷰 P1).
_HOMONYM_WEAK_PARTNER_WORDS = {
    # 역할/직함 접두어 — 같은 인물 기사에서도 "배우 장동건"처럼 일관 반복될 수 있어
    # 동음이의 partner 증거로 쓰지 않는다(Codex 계획 리뷰 1차 P1/2차 P2).
    "배우", "가수", "의원", "감독", "대표", "회장", "장관", "총리", "대통령",
    "선수", "코치", "작가", "셰프", "아나운서", "교수", "기자",
}


def _exact_token_occurrences(articles: List[Dict], kw_tok: str) -> List[tuple]:
    """기사들의 title/snippet에서 kw_tok과 **exact 일치**하는 토큰 등장의 (prev, next)
    인접 토큰 쌍 목록(등장 순서 유지, 문장 시작/끝이면 None).

    exact 기준은 summarizer._tokens 결과 리스트(정규식 `[가-힣A-Za-z0-9]{2,}`, 문자
    span 아님) — 조사 결합형("워홀의")/붙임형("앤디워홀전")은 별도 토큰이라 미집계한다
    (관찰용 한계로 의도된 보수 처리, Codex 계획 리뷰 3차 P2. 영문 partner의 대소문자도
    정규화하지 않는다 — 미집계/불일치는 "탐지 안 함" 방향이라 오탐보다 안전).
    title과 snippet은 따로 토큰화한다(연결 경계에서 가짜 인접쌍이 생기는 것을 방지).
    """
    from news.summarizer import _tokens

    occurrences = []
    for a in articles:
        for text in (a.get("title", "") or "", a.get("snippet", "") or ""):
            toks = _tokens(text)
            for i, t in enumerate(toks):
                if t != kw_tok:
                    continue
                prev_tok = toks[i - 1] if i > 0 else None
                next_tok = toks[i + 1] if i + 1 < len(toks) else None
                occurrences.append((prev_tok, next_tok))
    return occurrences


def _consistent_collocation_partner(
    occurrences: List[tuple], kw_tok: str, primary_tokens_cf: set
) -> Optional[tuple]:
    """모든 exact 등장에서 동일하게 인접하는 partner 토큰을 (partner, direction)으로
    반환(prev 우선, 없으면 next — "앤디 워홀"형이 전형이라 prev를 먼저 본다). 조건:

    - 등장 최소 2회(1회뿐이면 "일관 반복"을 관측할 수 없음 — 보수적 미탐).
    - 전 등장에서 partner가 존재하고 동일(한 번이라도 없거나 다르면 실패).
    - partner가 kw_tok 자신이 아니고, display 일반어/검색의도 suffix/주체 노이즈 접두/
      역할명(_HOMONYM_WEAK_PARTNER_WORDS) 어디에도 속하지 않음.
    - partner가 primary cluster 표시 기사 토큰(casefold)에 미등장 — exact 토큰 기준
      (Codex 3차 P2: prev/next 판정과 동일하게 _tokens exact로 일관).
    기각 사유에 따라 동작이 다르다:
    - partner가 primary에 등장 → 같은 이슈(같은 개체의 표기 변형)라는 **적극적 증거**
      이므로 다른 방향을 더 보지 않고 클러스터 전체를 즉시 None(veto). 한 방향이
      same-sense 증거를 보이는데 다른 방향 partner로 탐지하면 정밀도가 무너진다.
    - generic/역할명/불일치 기각 → 증거가 "없는" 것뿐이므로 다음 방향을 계속 본다.
    조건 미달 시 None — "확신 없으면 탐지 안 함" 원칙(관찰 로그의 정밀도 우선).
    """
    if len(occurrences) < 2:
        return None
    excluded = (
        _all_display_generic() | _SEARCH_INTENT_SUFFIXES
        | _SUBJECT_NOISE_PREFIXES | _HOMONYM_WEAK_PARTNER_WORDS
    )
    for direction, idx in (("prev", 0), ("next", 1)):
        partners = {o[idx] for o in occurrences}
        if len(partners) != 1:
            continue
        partner = next(iter(partners))
        if not partner or partner == kw_tok or partner in excluded:
            continue
        if partner.casefold() in primary_tokens_cf:
            return None  # same-sense 적극적 증거 → 클러스터 전체 veto
        return partner, direction
    return None


def detect_homonym_entity_singletons(items: List[Dict]) -> List[Dict]:
    """단일 토큰 core keyword의 동음이의 혼입 후보를 **탐지만** 한다(제외/강등/순위
    변경 없음 — detect_broad_category_singletons와 동일한 logging-first 구조).

    반환값은 관찰용 진단 리스트로, 호출부(main.py)는 로그로만 남기고 파이프라인 결과에는
    절대 반영하지 않는다. 입력 items를 mutate하지 않으며 article/news_meta에 어떤 필드도
    추가하지 않는다. 대상 판정(모두 만족):
    - related_keywords 없음(merge group 동음이의는 이번 관찰 대상에서 제외 — 1차 범위,
      Codex 3차 P3).
    - keyword core가 단일 토큰(_tokens 기준 1개 — "워홀 뜻"은 1글자 suffix "뜻"이
      토큰화에서 빠져 {워홀} 하나만 남으므로 대상에 포함).

    표시 기사 집합은 실제 노출 파이프라인과 동일하게 산출한다(dedup →
    filter_articles_for_display → [:ARTICLES_MAX] → build_display_articles, effective
    keyword = display_keyword). would_* 진단값이 exclude_insufficient_display_articles와
    같은 입력 기준이 되도록 final(top) 단계에서 호출한다(Codex 2차 계획 리뷰 P1).

    표시 기사 중 non-primary(is_primary_cluster=False — compute_news_signal 당시 판정을
    그대로 신뢰)를 cluster_articles로 재군집하는데, 이 재군집은 원래 primary cluster
    재판정이 아니라 "표시된 non-primary 기사 안의 의미 묶음"을 shadow 진단하기 위한
    보조 단계다(Codex 3차 P2). 각 묶음의 dominant collocation partner가 확인되면 진단에
    담는다. primary 표시 기사가 없으면(baseline 부재) 판정 불가로 skip.

    진단 dict 필드:
    - keyword/display_keyword/displayed_articles(표시 기사 수)
    - clusters: [{partner, direction, articles, exact_occurrences}] — 탐지된 shadow 묶음
    - would_exclude_display_count: 탐지 묶음 기사 합(후속 hard exclude 시 빠질 표시 기사
      수, candidate 누적 기준 — Codex 3차 P2: 묶음별로 따로 보면 누적 drop을 과소평가)
    - would_drop_candidate_by_display_min: 그 결과 표시 기사가 DISPLAY_ARTICLES_MIN
      미만이 되어 후보 자체가 drop됐을지(bool)
    - primary_suspect: primary cluster 기사에도 같은 collocation 판정을 적용한 결과
      ({partner, direction} | None). primary 선택이 뒤집혀 동음이의가 primary가 된
      경우를 관찰하기 위한 별도 키(차단 후보 목록이 아님 — Codex 1차 P1-3/3차 P3).
    """
    from news.summarizer import _tokens
    from news.dedup import dedup_articles
    from news.candidates import (
        build_display_articles, cluster_articles, filter_articles_for_display,
    )
    from news.builder import ARTICLES_MIN, ARTICLES_MAX

    diagnostics: List[Dict] = []
    for item in items:
        if item.get("related_keywords"):
            continue  # merge group은 1차 관찰 대상 아님
        kw = (item.get("keyword", "") or "").strip()
        kw_toks = _tokens(kw)
        if len(kw_toks) != 1:
            continue
        kw_tok = kw_toks[0]

        news_meta = item.get("news_meta") or {}
        effective_keyword = item.get("display_keyword") or kw
        filtered = filter_articles_for_display(
            dedup_articles(news_meta.get("articles") or []), min_count=ARTICLES_MIN
        )[:ARTICLES_MAX]
        displayed = build_display_articles(
            effective_keyword, filtered, news_meta.get("representative_article")
        )

        primary = [a for a in displayed if a.get("is_primary_cluster")]
        non_primary = [a for a in displayed if not a.get("is_primary_cluster")]
        if not primary or not non_primary:
            continue  # baseline(primary 표시 기사) 또는 관찰 대상이 없으면 판정 불가

        primary_tokens_cf: set = set()
        for a in primary:
            primary_tokens_cf |= {
                t.casefold()
                for t in _tokens(f"{a.get('title', '')} {a.get('snippet', '')}")
            }

        clusters_found: List[Dict] = []
        for cluster in cluster_articles(non_primary):
            occurrences = _exact_token_occurrences(cluster, kw_tok)
            found = _consistent_collocation_partner(occurrences, kw_tok, primary_tokens_cf)
            if found:
                partner, direction = found
                clusters_found.append({
                    "partner": partner,
                    "direction": direction,
                    "articles": len(cluster),
                    "exact_occurrences": len(occurrences),
                })

        primary_suspect = None
        found_primary = _consistent_collocation_partner(
            # primary 자신과의 미등장 비교는 성립하지 않으므로 빈 집합을 넘긴다.
            _exact_token_occurrences(primary, kw_tok), kw_tok, frozenset()
        )
        if found_primary:
            primary_suspect = {"partner": found_primary[0], "direction": found_primary[1]}

        if not clusters_found and primary_suspect is None:
            continue

        would_exclude = sum(c["articles"] for c in clusters_found)
        diagnostics.append({
            "keyword": kw,
            "display_keyword": effective_keyword,
            "displayed_articles": len(displayed),
            "clusters": clusters_found,
            "would_exclude_display_count": would_exclude,
            "would_drop_candidate_by_display_min": (
                len(displayed) - would_exclude
            ) < DISPLAY_ARTICLES_MIN,
            "primary_suspect": primary_suspect,
        })
    return diagnostics


# === PR/광고성 클러스터 hard exclude (문제 B) ===
COMMERCIAL_PR_RATIO_THRESHOLD = 0.6
PR_MIN_ARTICLES = 2  # 순수 PR 판정에 필요한 최소 PR 기사 수(단건 stray 마커 노이즈 방어)


def exclude_pr_clusters(ranked: List[Dict]) -> tuple:
    """PR/광고성(공식 후원/체험존/브랜드 캠페인 등) 클러스터를 최종 후보에서 제외(hard exclude).

    dedupe_and_merge() *이전*, per-keyword news_meta 기준으로 적용한다. merge 이후 group 단위
    제외는 canonical=score 1위 고정 구조상 흡수된 정상 이슈까지 삭제하므로 하지 않는다
    (Codex review-only 8·9차: 정상 이슈 오제외 > PR 누수 우선순위). 대신 PR 키워드를 merge 전에
    제거해 PR이 canonical이 되어 정상 이슈를 흡수하는 것 자체를 막는다.

    제외 조건(candidates.compute_news_signal 집계 기준):
    - pr_article_count >= PR_MIN_ARTICLES (순수 PR 기사가 최소 2건)
    - commercial_pr_ratio >= COMMERCIAL_PR_RATIO_THRESHOLD (이슈 정의 기사의 60% 이상이 PR)
    - public_interest_count == 0 (사건성/시장성 override 기사가 하나도 없음)
    public-interest override(강한 사건성 title 1건, 시장성은 PR 마커 없을 때만)는 candidates에서
    이미 public_interest_count에 반영돼 있어, 하나라도 있으면 여기서 제외되지 않는다.

    Top10이 부족해도 이 제외로 빈 자리를 PR filler로 채우지 않는다(개수 감소는 정상).
    반환: (kept, excluded_keywords).
    """
    kept: List[Dict] = []
    excluded: List[str] = []
    for item in ranked:
        nm = item.get("news_meta") or {}
        if (
            nm.get("pr_article_count", 0) >= PR_MIN_ARTICLES
            and nm.get("commercial_pr_ratio", 0.0) >= COMMERCIAL_PR_RATIO_THRESHOLD
            and nm.get("public_interest_count", 0) == 0
        ):
            excluded.append(item.get("keyword", ""))
            continue
        kept.append(item)
    return kept, excluded


# === display_keyword / articles 정합성 invariant (문제 A) ===
# merge된 item은 news_meta(=articles/representative)가 canonical(primary) 것만 실리는데
# display_keyword는 group union coverage로 별도 선택돼, false merge 시 display가 표시 기사와
# 다른 이슈를 가리킬 수 있다("조타 교통사고 사망" display + 구제역 articles). display의 이슈
# 식별 토큰이 표시 기사에 실제 등장하는지 검증해 불일치를 원천 차단한다.
#
# skip-list는 이슈 식별에 무의미한 "약한 수식어"만 담는다. 수사/조사/논란/의혹/진입/충돌/사망/
# 사고/화재 같은 사건 식별 토큰은 검증 대상으로 남긴다(Codex review-only 10·11차: 이들을
# 검증에서 빼면 "A 사망" display가 A만 등장해도 통과해 사건 꼬리표가 기사로 뒷받침되지 않음).
_INVARIANT_SKIP_TOKENS = {
    "신임", "임명", "승진", "취임", "내정", "발탁", "선임", "인사", "전보",
    "발표", "공개", "예정", "오늘", "관련", "진행", "계획",
}


def _invariant_check_tokens(text: str) -> set:
    """정합성 검증 대상 토큰: len>=2 이고 약한 수식어(_INVARIANT_SKIP_TOKENS)가 아닌 것."""
    from news.summarizer import _tokens

    return {t for t in _tokens(text) if len(t) >= 2 and t not in _INVARIANT_SKIP_TOKENS}


def _displayed_articles(articles: List[Dict]) -> List[Dict]:
    """실제 화면에 노출되는 기사 집합을 builder와 동일하게 산출한다(dedup → filter → [:MAX]).
    _displayed_article_units와 문맥 alias(_contextual_alias_forms)가 **동일 노출 집합**을
    보도록 단일 진실원으로 분리한다.
    """
    from news.dedup import dedup_articles
    from news.candidates import filter_articles_for_display
    from news.builder import ARTICLES_MIN, ARTICLES_MAX

    return filter_articles_for_display(dedup_articles(articles or []), min_count=ARTICLES_MIN)[:ARTICLES_MAX]


def _displayed_article_units(articles: List[Dict]) -> List[tuple]:
    """실제 화면에 노출되는 기사 집합을 builder와 동일하게 산출해, 기사별 (토큰집합, 원문)
    리스트로 반환한다. builder.build_ranked_entry가 dedup_articles → filter_articles_for_display
    → [:ARTICLES_MAX] 순으로 노출 집합을 만들므로(Codex diff 리뷰 P2), invariant도 같은
    집합으로 검증해야 "표시 기사" 정합성이 보장된다.

    검증은 기사별 단위로 한다(aggregate 금지). display 검증 토큰이 서로 다른 기사에 흩어져
    있어도 통과하던 split-token 오탐(Codex diff 리뷰 P1: "배우B 사망"이 배우B 기사 + 원로배우
    사망 기사로 각각 존재해도 통과)을 막기 위함이다.
    """
    from news.summarizer import _tokens

    displayed = _displayed_articles(articles)
    units = []
    for a in displayed:
        text = f"{title_evidence_text(a.get('title', ''))} {a.get('snippet', '')}"
        units.append((set(_tokens(text)), text))
    return units


def _supported_by_single_article(check_toks: set, units: List[tuple]) -> bool:
    """검증 대상 토큰 전부를 한 기사(단일 unit)가 커버하는지 — 하나라도 그런 기사가 있으면 True.
    토큰집합 exact 또는 원문 substring(조사/어미 결합 대응) 매칭. substring은 false-reject
    (정상 이슈 버림)를 피하는 안전 방향이다."""
    for art_toks, art_text in units:
        if all(t in art_toks or t in art_text for t in check_toks):
            return True
    return False


def enforce_display_article_consistency(items: List[Dict]) -> List[Dict]:
    """각 item의 display_keyword가 표시 기사(canonical news_meta.articles 중 실제 노출분)와
    정합인지 검증한다.

    - display 검증 토큰 전부를 표시 기사 중 한 기사가 커버하면 그대로 유지.
    - 그런 기사가 없으면 display를 canonical(keyword)로 강등한다(canonical은 quality gate상
      자기 기사에 등장이 보장돼 항상 정합). 단 canonical이 generic-only거나 canonical
      검증 토큰조차 표시 기사에 없으면(방어) 그 item을 reject(노출 안 함).
    dedupe_and_merge() 이후, exclude_generic_singletons()/select_top() 이전에 적용한다.
    """
    result: List[Dict] = []
    for item in items:
        news_meta = item.get("news_meta") or {}
        units = _displayed_article_units(news_meta.get("articles") or [])
        canonical = item.get("keyword", "")
        display = item.get("display_keyword") or canonical
        disp_check = _invariant_check_tokens(display)
        if disp_check and not _supported_by_single_article(disp_check, units):
            if _is_generic_only_display(canonical):
                continue  # 강등 대상이 generic-only(수사/신임 등) → reject
            canon_check = _invariant_check_tokens(canonical)
            if canon_check and not _supported_by_single_article(canon_check, units):
                continue  # canonical조차 표시 기사와 불일치 → reject
            item = dict(item)
            item["display_keyword"] = canonical
        result.append(item)
    return result


# 직함/수량 등 그 자체로는 사건 의미가 없는 부속 토큰(제거돼도 비문 아님). anchor 판정과
# 별개로, grounding에서 "무근거여도 축약 대상이 아닌"(원래 문구 유지) 토큰이 아니라, 반대로
# "무근거면 떼어내도 되는" 부속 토큰이다. 인물 직함·서수 등.
_GROUNDING_DROPPABLE_TOKENS = {
    "9단", "단", "씨", "장관", "대표", "회장", "사장", "감독", "선수", "의원", "총리",
    "대통령", "지사", "시장", "군수", "구청장", "위원장", "청장",
}


def _word_contains_token(word: str, token: str, siblings=None, alias_forms=None) -> bool:
    """원문 어절(word)이 token을 형태소 경계로 포함하는지 — 복합명사 오탐 방지 + 명시적
    표기 변형만 인정(사용자 P1 사전검토 2차, 2026-07-21).

    무제한 접두 포함(word.startswith(token))은 **인정하지 않는다**. 이전 구현은 접두면
    무조건 약칭으로 인정했으나, 그러면 '삼성'←'삼성물산', '카드'←'카드뉴스', '애플'←
    '애플리케이션'처럼 전혀 다른 개념까지 근거로 오인한다(ChatGPT P1 2차). "별도 근거 없는
    단순 접두 포함은 동일 개념으로 인정하지 않는다"는 계약을 따른다.

    인정 조건(모두 명시적 근거):
    1. exact 일치(word == token).
    2. 조사/어미 결합: token으로 시작하거나 끝나되 남는 부분이 1글자 이하('따릉이는'←'따릉이',
       '미국은'←'미국', '카드가'←'카드'). 한국어 조사/어미는 대개 1글자라 이 근사가 안전하다.
    3. alias 근거: word가 token의 명시적 alias form(alias_forms, 예: 교육기관 축약
       '배재고등학교'↔'배재고')과 일치할 때. 하드코딩 기업 약칭이 아니라 기존
       normalization이 제공하는 근거만 쓴다.
    4. 붙여쓰기 복합(exact composition, ChatGPT P1 2차 축소 2026-07-21): 허용된 1글자
       조사·어미만 제거한 정규화 word가 token과 canonical sibling(들)의 **정확한 연결**로
       완전히 설명될 때만 인정한다. 부분 prefix 일치(rest.startswith(s) 등)는 인정하지
       않는다.
         · 2-part exact: normalized_word == token+sibling('갤럭시카드'에서 token='갤럭시',
           sibling='카드') 또는 sibling+token('갤럭시카드'에서 token='카드', sibling='갤럭시').
         · 다토큰 복합: normalized_word가 {token} ∪ (일부 sibling)들의 연결로 **전체가 남김없이**
           설명될 때만. 일부만 맞물리고 잔여 문자가 남으면('갤럭시카드뉴스'의 '뉴스', '삼성카'의
           '카') 불인정.
       외래 복합('개인정보유출'의 '유출'은 앞의 '개인정보'가 sibling이 아님)·'삼성물산'의
       '삼성'(뒤 '물산'이 sibling 아님)·'카드뉴스'의 '카드'(뒤 '뉴스'가 sibling 아님)·
       '갤럭시카드뉴스'(뉴스 잔여)는 전부 배제된다.

    특정 회사명/제품명 하드코딩 없이 구조(조사 길이·sibling exact 연결·명시 alias)로만 판정한다.
    """
    if word == token:
        return True
    # 2. 조사/어미 결합: 남는 1글자가 실제 한국어 조사·어미일 때만('카드가'·'미국은'). 임의
    #    명사 조각('삼성카'의 '카')은 접두 1글자라도 인정하지 않는다(ChatGPT P1 2차: '삼성카'
    #    불인정). 접미('한국의' 같은 조사가 앞에 붙는 경우는 드물지만 대칭 유지).
    if word.startswith(token) and len(word) - len(token) == 1 and word[-1] in _ONE_CHAR_JOSA_EOMI:
        return True
    if word.startswith(token) and len(word) == len(token):
        return True
    if word.endswith(token) and len(word) - len(token) == 1 and word[0] in _ONE_CHAR_JOSA_EOMI:
        return True
    # 3. 명시적 alias form 근거(_institution_alias_forms / 문맥 alias)
    if alias_forms and word in alias_forms:
        return True
    # 4. 붙여쓰기 복합: 허용된 1글자 조사·어미만 제거한 뒤 token+sibling(들)의 exact 연결로만.
    sibs = tuple(s for s in (siblings or ()) if s and s != token)
    if sibs:
        # 허용 조사·어미(1글자)만 뒤에서 제거해 정규화. 정규화형이 token+sibling 조합으로
        # 완전히 성립하는지 확인한다(부분 prefix 매칭 금지).
        for norm in _normalized_word_forms(word):
            if _exact_composition(norm, token, sibs):
                return True
    return False


# 붙여쓰기 복합 판정 전에 제거를 허용하는 1글자 조사·어미(예: '갤럭시카드는'→'갤럭시카드').
# _word_contains_token 4번 규칙에서만 쓰인다. 무제한 접미 1글자 제거는 '삼성카드뉴'→'삼성카드'
# 같은 오인정을 낳으므로, 실제 한국어 1글자 조사·어미에 한정한다(임의 명사 조각 '뉴'는 불허).
_ONE_CHAR_JOSA_EOMI = frozenset("은는이가을를의에도만과와나로랑")


def _normalized_word_forms(word: str):
    forms = [word]
    if len(word) >= 3 and word[-1] in _ONE_CHAR_JOSA_EOMI:
        forms.append(word[:-1])  # 마지막 1글자가 조사·어미일 때만 제거형 추가
    return forms


def _exact_composition(norm: str, token: str, sibs) -> bool:
    """norm이 token과 sibs(일부)의 **정확한 연결**로 전체가 남김없이 설명되는지.

    - token은 반드시 포함되어야 하고, 나머지 조각은 전부 sibs에 속해야 하며, 이어붙인
      결과가 norm과 정확히 일치해야 한다(잔여 문자 불허).
    - 2-part(token+one sibling / one sibling+token)를 우선 검사하고, 그 다음 token을
      포함한 sibs 순열 연결로 완전 일치를 탐색한다(다토큰 복합 지원).
    """
    if not norm or token not in norm:
        return False
    # 빠른 2-part 경로
    for s in sibs:
        if norm == token + s or norm == s + token:
            return True
    # 일반 경로: norm을 앞에서부터 token 또는 sib 조각으로 완전 분해(token 최소 1회 사용).
    pieces = {token} | set(sibs)

    def _decompose(rest: str, used_token: bool) -> bool:
        if not rest:
            return used_token
        for p in pieces:
            if p and rest.startswith(p):
                if _decompose(rest[len(p):], used_token or p == token):
                    return True
        return False

    return _decompose(norm, False)


def _token_grounded_in_unit(token: str, art_toks: set, art_text: str, siblings=None, alias_map=None) -> bool:
    """token이 단일 기사(art_toks/art_text)에 어절 경계로 근거를 갖는지.

    exact 토큰 매칭 우선. 없으면 원문을 공백 단위 어절로 쪼개 _word_contains_token으로
    비교한다(전체 문자열 substring이 아니라 "어절 단위" 비교 — "정보"가 "개인정보 유출"
    문자열 아무 데나 부분매칭되는 것을 막고, 실제 그 어절이 존재하는지만 본다).

    siblings: 같은 canonical/display의 다른 검증 토큰 집합. token이 어절 접미/접두로 등장할
    때 남는 부분이 sibling과 맞물리면(붙여쓰기 복합) 인정하기 위해 _word_contains_token에
    전달한다(예: '삼성 갤럭시 카드'의 '카드'가 기사 '갤럭시카드'에). None이면 조사결합만.

    alias_map: {canonical_token: {expansion, ...}} — 기사 묶음에서 수렴 검증된 문맥 alias
    (_contextual_alias_forms). token에 해당하는 확장형을 _institution_alias_forms(교육기관
    축약)와 합쳐 alias_forms로 넘긴다. 무제한 접두 인정 없이, 이 묶음에서만 검증된 약칭↔정식
    명칭 근거를 인정한다(예: canonical '삼성'이 기사 '삼성전자'에).
    """
    if token in art_toks:
        return True
    alias_forms = set(_institution_alias_forms(token))
    if alias_map and token in alias_map:
        alias_forms |= alias_map[token]
    # alias form의 exact 매칭은 이미 정규 토큰화된 art_toks에도 적용한다(Codex P1, 2026-07-22).
    # art_text.split()(공백 어절)만 보면 '삼성전자·실적'처럼 구두점으로 붙은 원문에서 alias
    # 확장형('삼성전자')이 한 어절에 묻혀 매칭 실패한다. _tokens는 '삼성전자'/'실적'을 정규
    # 분리하므로, alias exact 일치는 art_toks 교집합으로 우선 확인한다(부분 substring 아님).
    if alias_forms & art_toks:
        return True
    # 조사·붙여쓰기 복합 판정이 필요한 경우에만 원문 어절을 보조적으로 검사한다.
    for word in art_text.split():
        w = word.strip("'\"·,.")
        if _word_contains_token(w, token, siblings, alias_forms):
            return True
    return False


def _display_grounded_by_single_unit(check_tokens: set, units: List[tuple], alias_map=None) -> bool:
    """검증 토큰 전부를 표시 기사 중 '한 기사'가 근접 문맥으로 커버하는지.

    _supported_by_single_article과 동일한 단일-기사 커버리지 원칙(근접구문/phrase
    provenance)을 grounding에도 적용한다 — 서로 다른 기사에 흩어진 토큰들이 각자
    다른 기사에서 grounded 판정을 받아 조합 전체가 통과하는 분산 매칭을 막기 위함
    (사용자 지적: A기사의 '정보' + B기사의 '유출'이 합쳐져 통과하면 안 됨. 두 토큰이
    실제로 같은 기사·같은 문맥에 함께 등장해야 그 조합이 근거를 갖는다).

    각 토큰 판정에 나머지 검증 토큰을 siblings로 넘겨, canonical 자신의 인접 토큰이
    기사에서 붙여쓰기된 복합(갤럭시 카드→갤럭시카드)을 정당 근거로 인정한다(과잉 drop 방지).

    alias_map: 기사 묶음에서 수렴 검증된 문맥 alias(_contextual_alias_forms). 약칭 canonical
    토큰이 정식명칭 확장형으로 grounded 되도록 _token_grounded_in_unit에 전달한다.
    """
    for art_toks, art_text in units:
        if all(
            _token_grounded_in_unit(t, art_toks, art_text, siblings=check_tokens, alias_map=alias_map)
            for t in check_tokens
        ):
            return True
    return False


def enforce_display_source_grounding(items: List[Dict]) -> List[Dict]:
    """display_keyword의 주요 비-generic 의미 토큰이 표시 기사에서 근거를 갖는지 확인하고,
    무근거 토큰은 안전하게 축약한다. 축약 후 의미가 성립하지 않으면 그 item을 drop한다.

    배경(따릉이 '정보 명예', 2026-07-21): 깨진 원천 seed가 canonical/display로 유입되면
    '명예'처럼 어느 표시 기사에도 없는(원문 무근거) 조각이 최종 표기에 남는다. 특정 문자열
    하드코딩 없이 "표시 기사에 근거가 있는가"라는 구조로만 판정한다.

    판정 단위(사용자 P1 보완, 2026-07-21): 토큰별 독립 판정이 아니라 "표시 기사 한 건이
    검증 토큰 전부를 근접 문맥으로 커버하는가"를 1차 기준으로 삼는다
    (_display_grounded_by_single_unit — enforce_display_article_consistency의
    _supported_by_single_article과 동일 원칙). 어절 경계 비교(_word_contains_token)로
    "정보"가 "개인정보"의 일부라는 이유만으로 독립 근거 취급되는 것을 막는다.

    canonical 오염 방지(사용자 P1 보완, 2026-07-21): 이 함수는 원래 display_keyword만
    교정하고 canonical(keyword)은 손대지 않았다. 하지만 canonical은 summary 생성
    (builder.build_ranked_entry의 summarize(keyword, ...)), diagnostics 기록
    (diagnostics.py의 row["keyword"]), movement 비교(다음 실행의 canonical 매칭)에
    display_keyword와 무관하게 그대로 재사용된다 — display만 고쳐도 이 세 경로는 여전히
    깨진 canonical을 본다. 그래서 **canonical 자체도 grounding 검증 대상에 포함**한다:
    canonical의 검증 토큰이 표시 기사 어디에도 근거가 없으면(display를 canonical로
    강등해도 구제되지 않는 경우), item 전체를 fail-closed로 drop한다 — display만 축약해
    화면만 가리는 것으로는 진단/요약/다음 실행까지 이어지는 오염을 막을 수 없기 때문이다.

    처리:
    0. canonical의 검증 토큰이 표시 기사 어디에서도 근거가 없으면(단일 기사 기준으로도,
       토큰 단위로도) → item 전체 drop(canonical 자체가 오염 — display 교정으로 구제 불가).
    1. display 토큰 중 검증 대상(_invariant_check_tokens: len>=2, 약한 수식어 제외)을 뽑는다.
    2. 전체 토큰 집합을 한 기사가 통째로 커버하면 그대로 통과(정상 — 대다수 케이스).
    3. 통째로 커버하는 기사가 없으면, 토큰별로 "그 토큰 하나만 넣었을 때 단일 기사가
       커버하는가"를 봐서 무근거 토큰만 골라 제거한다(부분 축약도 같은 단일-기사 원칙 유지).
    4. 무근거 토큰 제거 후 남은 "근거 있는 의미 토큰"(비-generic·비-droppable)이 하나도
       없으면 → drop(fail-closed). 남으면 → 그 축약본으로 display 교체(어순 유지).
    5. 표시 기사가 없으면(근거 판정 불가) 개입하지 않는다(fail-open, 기존 보수 정책과 동일).

    enforce_display_article_consistency 직후, exclude_generic_singletons 이전에 적용한다.
    """
    from news.summarizer import _tokens

    result: List[Dict] = []
    for item in items:
        news_meta = item.get("news_meta") or {}
        units = _displayed_article_units(news_meta.get("articles") or [])
        if not units:
            result.append(item)  # 근거 판정 불가 → fail-open
            continue
        canonical = item.get("keyword", "")
        display_for_alias = item.get("display_keyword") or canonical
        # 문맥 alias(약칭↔정식명칭) — 표시 기사 묶음에서만 수렴 검증한다(_contextual_alias_forms).
        # canonical/display 검증 토큰 전체를 후보로 넘겨, 약칭 canonical '삼성'이 기사 '삼성전자'로
        # grounded 되게 한다(정상 이슈 과잉 drop 방지). 확장형이 충돌·부족하면 매핑에 안 들어가
        # 기존 fail-closed 계약이 유지된다.
        alias_tokens = _invariant_check_tokens(canonical) | _invariant_check_tokens(display_for_alias)
        alias_map = _contextual_alias_forms(alias_tokens, _displayed_articles(news_meta.get("articles") or []))
        # 0. canonical 자체 grounding — display 교정 이전에 canonical 오염을 원천 차단.
        #
        # 계약(ChatGPT P1, 2026-07-21): canonical의 invariant 토큰 조합 "전체"가 **동일
        # 기사(단일 unit) 안에서 함께** 근거를 가져야 한다. 즉 canonical 검증은 오직
        # _display_grounded_by_single_unit(단일 기사가 토큰 전부를 근접 문맥으로 커버)만으로
        # 판정하고, 이것이 false면 무조건 fail-closed drop한다.
        #
        # 토큰별 전체 기사 재검색(각 토큰이 "서로 다른 기사 어딘가에" 있으면 근거로 인정)은
        # canonical 존속 근거로 **절대 쓰지 않는다** — 그렇게 하면 기사 A의 '따릉이 정보' +
        # 기사 B의 '명예'처럼 어느 한 기사도 canonical 전체를 뒷받침하지 못하는데도 canonical이
        # 통과하는 분산 매칭 구멍이 생긴다(ChatGPT 재검토 P1). alias·띄어쓰기·조사 차이는
        # _token_grounded_in_unit이 "동일 기사 안에서" 정규화해 인정하므로(false-reject 방지)
        # 별도 토큰별 재검색 없이도 정상 표기 변형은 구제된다.
        canonical_check = _invariant_check_tokens(canonical)
        if canonical_check and not _display_grounded_by_single_unit(canonical_check, units, alias_map):
            logger.warning(
                "[news] drop %s: canonical_source_grounding(canonical 토큰 조합 '%s'를 "
                "동일 기사가 함께 뒷받침하지 못함 — 분산/무근거, display 교정으로 구제 불가)",
                canonical, " ".join(sorted(canonical_check)),
            )
            continue  # fail-closed drop — 단일 기사 결합 근거 없으면 무조건 drop
        display = item.get("display_keyword") or canonical
        disp_tokens = _tokens(display)
        check_tokens = _invariant_check_tokens(display)
        if not check_tokens or _display_grounded_by_single_unit(check_tokens, units, alias_map):
            result.append(item)
            continue
        # 통짜로 커버하는 기사가 없다 → 토큰별로 단일 기사 근거 여부를 재확인해 무근거만 축약.
        ungrounded = {
            t for t in check_tokens
            if not any(_token_grounded_in_unit(t, art_toks, art_text, alias_map=alias_map) for art_toks, art_text in units)
        }
        if not ungrounded:
            # 개별 토큰은 각자 근거가 있지만 한 기사에 다 같이 등장하지 않는 조합(분산 매칭
            # 의심). 원 display 신뢰 대신 canonical로 강등(article_consistency와 동일 정책).
            if _is_generic_only_display(canonical):
                continue
            canon_check = _invariant_check_tokens(canonical)
            if canon_check and not _display_grounded_by_single_unit(canon_check, units, alias_map):
                continue
            item = dict(item)
            item["display_keyword"] = canonical
            result.append(item)
            continue
        # 무근거 토큰 제거(어순 유지). generic/약한 수식어는 유지(축약 후 자연스러움).
        kept_seq = [t for t in disp_tokens if t not in ungrounded]
        # 축약 후 "근거 있는 의미 토큰"(비-generic·비-droppable)이 남는지 판정.
        generic = _all_display_generic()
        meaningful = [
            t for t in kept_seq
            if len(t) >= 2 and t not in generic and t not in _GROUNDING_DROPPABLE_TOKENS
            and t not in _INVARIANT_SKIP_TOKENS
            and any(_token_grounded_in_unit(t, art_toks, art_text, alias_map=alias_map) for art_toks, art_text in units)
        ]
        if not meaningful:
            logger.warning(
                "[news] drop %s: display_source_grounding(무근거 조각 '%s' 제거 후 의미 소실, display=%r)",
                canonical, ",".join(sorted(ungrounded)), display,
            )
            continue  # fail-closed drop
        new_display = " ".join(kept_seq).strip()
        item = dict(item)
        item["display_keyword"] = new_display[:DISPLAY_KEYWORD_MAX_LEN]
        logger.info(
            "[news] display_source_grounding 축약: %r → %r (무근거 '%s')",
            display, item["display_keyword"], ",".join(sorted(ungrounded)),
        )
        result.append(item)
    return result


def select_top(ranked: List[Dict], top_n: int = TOP_N) -> List[Dict]:
    """dedupe/merge 이후 리스트에서 상위 N개를 뽑는다.

    dedupe_and_merge()가 이미 selected-set 누적 방식으로 재중복을 제거했으므로
    여기서는 단순 슬라이스만 수행한다(부족하면 dedupe_and_merge가 알아서 다음
    후보를 그룹에 포함시키지 않은 나머지 항목이 순서대로 이어져 자연스럽게 backfill됨).
    """
    return ranked[:top_n]


# display_articles 최소 노출 기준(2026-07-05): 상세 팝업에 기사가 1건뿐인 후보는
# Top10 실시간 이슈로 신뢰도가 낮아 최종 후보에서 제외한다(2건은 허용, 감점 없음).
DISPLAY_ARTICLES_MIN = 2


def exclude_insufficient_display_articles(items: List[Dict]) -> tuple:
    """display_articles(사용자 노출 전용)가 DISPLAY_ARTICLES_MIN 미만(<=1)인 후보를 제외.

    canonical_evidence helper(candidates.py)로 builder와 완전 동일한 정제 기사 집합을 얻어
    display 개수를 산출한다(F, 2026-07: drift 방지 단일 진실원).

    **select_top() 이전, 전체 merged 리스트에 적용한다(2026-07 변경, Codex 계획리뷰 P1-4)** —
    이전엔 select_top 이후 적용이라 Top10 통과분이 display 부족으로 빠지면 하위 backfill 없이
    9개가 됐다. select_top 전에 제외하면 그 자리를 하위 정상후보가 채운다. 제외로 개수가
    줄어도 filler는 넣지 않는다. articles 원본/ranking gate/quality·fresh·PR gate는 불변.
    반환: (kept, excluded_keywords).
    """
    from news.candidates import build_display_articles, canonical_evidence

    kept: List[Dict] = []
    excluded: List[str] = []
    for item in items:
        news_meta = item.get("news_meta") or {}
        keyword = item.get("keyword", "")
        articles, _, _ = canonical_evidence(news_meta, keyword)
        effective_keyword = item.get("display_keyword") or keyword
        display = build_display_articles(
            effective_keyword, articles, news_meta.get("representative_article")
        )
        if len(display) < DISPLAY_ARTICLES_MIN:
            logger.warning(
                "[news] drop %s: insufficient_display_articles(%d<%d)",
                effective_keyword, len(display), DISPLAY_ARTICLES_MIN,
            )
            excluded.append(keyword)
            continue
        kept.append(item)
    return kept, excluded


def exclude_no_representative(items: List[Dict]) -> tuple:
    """정제(candidates C) 후 canonical evidence set으로도 대표 사건을 만들 수 없는(summary_type
    =='no_representative') 후보를 Top10에서 제외한다(B2 최종 안전망, F).

    subject/entity-role 정제·cohesion gate를 다 거친 뒤에도 공통 하위주제가 없어 대표기사를
    만들 수 없으면, summary는 비는데 키워드만 Top10에 노출되는 정책 불일치가 생긴다. 이를
    막는 최종 게이트다. **select_top() 이전, 전체 merged에 적용**(하위 정상후보 backfill 보장).

    canonical_evidence helper로 builder와 동일한 (articles, summary_type)을 얻는다 — 사전 판정과
    실제 발행 summary_type이 어긋나지 않는다(Codex 계획리뷰 P1-5). summarize는 canonical
    keyword로 호출(helper 내부). representative 판정은 select_representative가 이미 최선 시도
    (정제된 primary cluster 기준)이므로 그 결과(summary_type)가 canonical이다 — 별도 재시도 없음.

    반환: (kept, excluded_keywords).
    """
    from news.candidates import canonical_evidence

    kept: List[Dict] = []
    excluded: List[str] = []
    for item in items:
        news_meta = item.get("news_meta") or {}
        keyword = item.get("keyword", "")
        _, _, summary_type = canonical_evidence(news_meta, keyword)
        if summary_type == "no_representative":
            logger.warning("[news] drop %s: no_representative(정제 후 대표 사건 없음)", keyword)
            excluded.append(keyword)
            continue
        kept.append(item)
    return kept, excluded


def run_selection_stages(
    candidates: List[Dict],
    signals: Dict[str, Dict],
    observe: Optional[Callable[[str, List[str]], None]] = None,
) -> Dict:
    """뉴스 선정 게이트 시퀀스의 단일 진실원.

    main._rank_and_select(운영)와 news.replay.replay_selection(재생)이 이 함수 하나를
    호출한다 — 게이트 추가/순서 변경은 반드시 여기서만 일어나야 두 경로가 갈라지지
    않는다(news/dryrun.py가 자체 복제본으로 2단계 누락 + 순서 역전으로 drift한 전례가
    이 통합의 근거. dryrun 재동기는 별도 PR).

    observe(stage, excluded_keywords): 각 제외 단계 직후 호출되는 순수 관찰 hook.
    main이 기존과 동일한 시점(다음 stage의 내부 로그보다 앞)에 자기 logger로 집계
    경고를 찍기 위해 존재한다 — 파이프라인 결과에 영향을 주면 안 된다.
    stage ∈ {"pr_excluded", "generic_excluded", "display_excluded", "no_rep_excluded"}.

    반환 dict(단계별 중간 산출물 — 호출자가 로깅/진단/재구성에 소비):
      gate_passed: compute_scores 통과 직후 keyword 리스트(이후 단계와 무관하게
                   이 시점 값을 보존하기 위해 즉시 추출)
      ranked: exclude_pr_clusters 통과 후 리스트
      pr_excluded / generic_excluded / display_excluded / no_rep_excluded:
                   각 단계의 제외 keyword 리스트
      merged: display 변환 3단계(resolve/consistency/grounding)까지 끝난 merge 결과
      kept: 모든 제외 완료 후 리스트(= main의 selected_pre_display)
      top: select_top 결과
    """
    notify = observe or (lambda stage, excluded: None)
    ranked = compute_scores(candidates, signals)
    gate_passed = [r["keyword"] for r in ranked]
    # PR/광고성 클러스터 hard exclude — merge 이전 per-keyword 기준.
    ranked, pr_excluded = exclude_pr_clusters(ranked)
    notify("pr_excluded", pr_excluded)
    merged = dedupe_and_merge(ranked)
    # singleton sense-mixing display 보정 — merge 후, invariant 검증 전.
    merged = resolve_singleton_displays(merged)
    # display_keyword/articles 정합성 invariant — merge 후, generic guard 전.
    merged = enforce_display_article_consistency(merged)
    # display source grounding — 무근거 조각 축약/drop. consistency 직후.
    merged = enforce_display_source_grounding(merged)
    kept, generic_excluded = exclude_generic_singletons(merged)
    notify("generic_excluded", generic_excluded)
    # display 부족 / no_representative 제외는 select_top *이전* 전체 리스트에 적용한다
    # (2026-07, Codex 계획리뷰 P1-4). 제외 후 select_top이 슬라이스만 하므로 하위
    # 정상후보가 자연히 그 자리를 채운다. 순서: generic → display부족 → no_rep → select_top.
    kept, display_excluded = exclude_insufficient_display_articles(kept)
    notify("display_excluded", display_excluded)
    kept, no_rep_excluded = exclude_no_representative(kept)
    notify("no_rep_excluded", no_rep_excluded)
    top = select_top(kept)
    return {
        "gate_passed": gate_passed,
        "ranked": ranked,
        "pr_excluded": pr_excluded,
        "merged": merged,
        "generic_excluded": generic_excluded,
        "display_excluded": display_excluded,
        "no_rep_excluded": no_rep_excluded,
        "kept": kept,
        "top": top,
    }
