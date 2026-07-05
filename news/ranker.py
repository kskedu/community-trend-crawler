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
from typing import Dict, List, Optional

from news.candidates import _INDEPENDENT_SEARCH_FAMILIES

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
    """
    if _is_horoscope_candidate(keyword, news_meta):
        return "horoscope_content"
    hrc = news_meta.get("high_relevance_count", 0)
    qcs = news_meta.get("quality_cluster_size", 0)
    if not (hrc >= 2 or qcs >= 2):
        return "low_quality_news"
    if news_meta.get("fresh_high_relevance_count", 0) < 1:
        return "stale_only"
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


def _has_cross_keyword_anchor(item_a: Dict, item_b: Dict, shared_tokens: set) -> bool:
    """겹치는 사건 토큰(shared_tokens) 중, 두 keyword 중 하나의 anchor 토큰이 포함되거나
    한쪽 keyword의 anchor 토큰이 상대 article 그룹에 등장하는지 확인.

    "배재고 출전정지" ↔ "권오영 감독" 케이스: "배재고"가 양쪽 article 그룹에 반복
    등장하므로 anchor 조건을 만족한다(권오영 감독 그룹의 기사에도 "배재고"가 실제로
    반복 등장). 반대로 "정부 오늘 새 정책 발표" ↔ "기업 오늘 실적 발표"는 keyword
    anchor("정부"/"기업")가 서로의 그룹에 등장하지 않아 걸러진다.
    """
    anchors_a = _keyword_anchor_tokens(item_a)
    anchors_b = _keyword_anchor_tokens(item_b)
    if shared_tokens & (anchors_a | anchors_b):
        return True

    articles_a = (item_a.get("news_meta") or {}).get("articles") or []
    articles_b = (item_b.get("news_meta") or {}).get("articles") or []
    tokens_in_a = set()
    for a in articles_a:
        if _is_same_issue_evidence_article(a):
            tokens_in_a |= set(_tokens_of(a))
    tokens_in_b = set()
    for b in articles_b:
        if _is_same_issue_evidence_article(b):
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


def _is_same_issue(item_a: Dict, item_b: Dict) -> bool:
    """same-issue merge 판정: article overlap(기존) OR 사건 토큰 overlap(신규, 옵션 B+C).

    신규 신호는 아래 조건을 모두 만족해야 인정한다(Codex review-only 조언: B를 recall
    신호로, C를 precision 게이트로 결합):
    1. 두 keyword 그룹 다 유효 근거 기사가 0건이 아니고, 양쪽 다 singleton(1건)은
       아니어야 한다 — 양쪽 다 근거가 1건뿐이면 "반복 등장"을 전혀 관측할 수 없어
       흔한 서술어만 겹쳐도 신호가 발생하는 오탐 위험이 가장 크다(Codex review-only
       재지적: "정부 오늘 새 정책 발표" vs "기업 오늘 실적 발표" 단일 기사끼리).
    2. 두 keyword의 article 그룹에서 문서빈도 2 이상(또는 singleton fallback)인 토큰이
       최소 REPRESENTATIVE_OVERLAP_MIN_SHARED_TOKENS개 이상 겹친다.
    3. 겹치는 토큰 중 하나가 두 keyword 중 하나의 anchor 토큰이거나, 한쪽 keyword의
       anchor 토큰이 상대 article 그룹에 실제로 등장한다(정부/기업처럼 서로 무관한
       주체의 흔한 서술어만 겹치는 오탐을 차단).
    4. 겹치는 토큰 중 흔한 서술어(_GENERIC_EVENT_PREDICATE_WORDS)가 아닌 것이 최소
       1개 포함돼야 한다(anchor 게이트를 우회하는 일반 서술어만의 조합 방지).
    """
    articles_a = (item_a.get("news_meta") or {}).get("articles") or []
    articles_b = (item_b.get("news_meta") or {}).get("articles") or []
    if _article_overlap(articles_a, articles_b) >= MERGE_ARTICLE_OVERLAP_THRESHOLD:
        return True

    evidence_count_a = _count_same_issue_evidence_articles(articles_a)
    evidence_count_b = _count_same_issue_evidence_articles(articles_b)
    if evidence_count_a == 0 or evidence_count_b == 0:
        return False
    if evidence_count_a == 1 and evidence_count_b == 1:
        return False

    shared = _representative_overlap(item_a, item_b)
    if len(shared) < REPRESENTATIVE_OVERLAP_MIN_SHARED_TOKENS:
        return False
    if not (shared - _GENERIC_EVENT_PREDICATE_WORDS):
        return False
    return _has_cross_keyword_anchor(item_a, item_b, shared)


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
}


def _all_display_generic() -> set:
    """display 판정에서 제외할 일반어 = 기존 event predicate + 인사 서술어."""
    return _GENERIC_EVENT_PREDICATE_WORDS | _DISPLAY_GENERIC_WORDS


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
    articles = _display_group_articles(members)
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
    2. keyword coverage 감점 — keyword 자체가 그룹 기사 절반 미만에만 등장하면(지엽
       엔티티) -1. "보스니아 헤르체고비나"처럼 일부 기사에만 나오는 다어절 엔티티를
       하드코딩 없이 데이터로 감점(Codex diff 리뷰 P2: 공통토큰 수보다 앞).
    3. 공통 사건토큰 포함 수 — coverage가 대등한 후보들 사이에서, 그룹 기사 절반
       이상에 걸쳐 등장하는 핵심어(common_tokens)를 많이 담을수록 대표성↑.
    4. broad 단독어 페널티(-1) — _TOO_BROAD_SINGLE_WORDS 단독 후보 감점.
    5. seed priority(daum>danawa>aux) — 대표성 동률일 때 원 seed 우선(tie-breaker).
    6. 구체성(keyword 토큰 수) — 그래도 동률이면 더 구체적인 표현 우선.
    7. 원 score — 최종 tie-breaker(신호 강도).
    """
    from news.summarizer import _tokens

    kw = member["keyword"]
    kw_toks = set(_tokens(kw))
    common_hits = len(kw_toks & common_tokens)
    generic_penalty = -1 if _is_generic_only_display(kw) else 0
    coverage_penalty = -1 if _keyword_coverage(member, group_articles) < DISPLAY_TOKEN_MIN_COVERAGE else 0
    broad_penalty = -1 if kw.strip() in _TOO_BROAD_SINGLE_WORDS else 0
    return (
        generic_penalty,
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

    toks = set(_tokens(keyword or ""))
    if not toks:
        return False
    return toks <= _all_display_generic()


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

    candidate = f"{best} {second}"
    if len(candidate) <= DISPLAY_KEYWORD_MAX_LEN:
        return _display_or_canonical(candidate, canonical)
    return _display_or_canonical(best, canonical)


def _display_or_canonical(display: str, canonical: str) -> str:
    """최종 display 후보가 generic-only(신임/임명 등)면 canonical로 대체한다.
    canonical 자체가 generic-only인 극단 케이스에는 그대로 canonical을 쓴다(그 이상
    나은 선택지가 없음). DISPLAY_KEYWORD_MAX_LEN 상한 적용.
    """
    if _is_generic_only_display(display):
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


def _displayed_article_units(articles: List[Dict]) -> List[tuple]:
    """실제 화면에 노출되는 기사 집합을 builder와 동일하게 산출해, 기사별 (토큰집합, 원문)
    리스트로 반환한다. builder.build_ranked_entry가 dedup_articles → filter_articles_for_display
    → [:ARTICLES_MAX] 순으로 노출 집합을 만들므로(Codex diff 리뷰 P2), invariant도 같은
    집합으로 검증해야 "표시 기사" 정합성이 보장된다.

    검증은 기사별 단위로 한다(aggregate 금지). display 검증 토큰이 서로 다른 기사에 흩어져
    있어도 통과하던 split-token 오탐(Codex diff 리뷰 P1: "배우B 사망"이 배우B 기사 + 원로배우
    사망 기사로 각각 존재해도 통과)을 막기 위함이다.
    """
    from news.dedup import dedup_articles
    from news.candidates import filter_articles_for_display
    from news.builder import ARTICLES_MIN, ARTICLES_MAX
    from news.summarizer import _tokens

    displayed = filter_articles_for_display(dedup_articles(articles or []), min_count=ARTICLES_MIN)[:ARTICLES_MAX]
    units = []
    for a in displayed:
        text = f"{a.get('title', '')} {a.get('snippet', '')}"
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

    builder가 issues를 조립할 때 계산하는 display_articles와 동일한 입력
    (news_meta.articles → dedup → filter_articles_for_display → build_display_articles)으로
    노출 개수를 미리 산출해, build 이전에 제외한다. build 이전에 적용해야 run_news_briefing의
    recent guard / partial publish 판단과 저장 로그가 실제 발행 개수와 정합한다
    (Codex review-only P2, 2026-07-05: builder 단계에서 줄이면 이미 끝난 recent guard/
    로그와 어긋남). articles 원본/ranking gate/quality·fresh·PR gate는 건드리지 않는다 —
    이 gate는 "사용자 노출 품질" 방어용이며, 제외로 개수가 줄어도 filler는 넣지 않는다.

    dedupe_and_merge()/generic·PR gate/enforce_display_article_consistency 이후,
    select_top() 이후에 적용한다(최종 노출 후보 확정 단계).
    반환: (kept, excluded_keywords).
    """
    from news.dedup import dedup_articles
    from news.builder import ARTICLES_MIN, ARTICLES_MAX
    from news.candidates import build_display_articles, filter_articles_for_display

    kept: List[Dict] = []
    excluded: List[str] = []
    for item in items:
        news_meta = item.get("news_meta") or {}
        articles = filter_articles_for_display(
            dedup_articles(news_meta.get("articles") or []), min_count=ARTICLES_MIN
        )[:ARTICLES_MAX]
        effective_keyword = item.get("display_keyword") or item.get("keyword")
        display = build_display_articles(
            effective_keyword, articles, news_meta.get("representative_article")
        )
        if len(display) < DISPLAY_ARTICLES_MIN:
            logger.warning(
                "[news] drop %s: insufficient_display_articles(%d<%d)",
                effective_keyword, len(display), DISPLAY_ARTICLES_MIN,
            )
            excluded.append(item.get("keyword", ""))
            continue
        kept.append(item)
    return kept, excluded
