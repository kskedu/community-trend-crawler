"""후보 수집/병합 + News 신호 산출.

설계 계약 (docs/news-ranking-plan.md §3, §4-2, docs/news-ranking-quality-plan.md §7):
- 후보 pool = Daum seed + Danawa seed + Google(stub) + 경량 보조후보(뉴스 title 토큰).
- normalize/dedup 후 상한(기본 30)으로 자른다.
- News 신호(recent_count/latest_age_hours/domain_diversity/title_relevance)는
  normalizer 결과에서 파생 — 기사 본문 전문 저장 없음.
- 다양성 hard guard: Daum 단독 출처가 아닌 후보 수 < MIN_NON_DAUM_CANDIDATES 이면
  상위에서 upsert skip (이 모듈은 카운트만 제공).

품질 개선(article relevance / clustering / representative selection):
- relevance는 score 계산 전에 산출한다(ranker의 title_relevance penalty가 이 값을 사용).
- 무거운 NLP/형태소 분석기 도입 없음. summarizer._tokens(정규식 토크나이저) 재사용.
- clustering은 Jaccard token overlap 기반 경량 그룹핑만 수행.
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

# incidental mention(부수적 언급) 판정 문맥 마커 — 경품/판촉/부가 물품 문맥.
# "제공"/"지급"처럼 일반 기사에도 흔한 단어는 keyword 근접(proximity) 조건과
# 함께일 때만 incidental로 본다(Codex diff 리뷰 P2: 전체 텍스트 any-match는
# "자료 제공"/"지원금 지급" 같은 정상 기사까지 오탐시킴).
# "선물"은 단독 어간으로 넣지 않는다(Codex review-only P1: keyword="닌텐도 스위치 2" +
# title="닌텐도 스위치 2 어린이날 선물 추천" 같은 진짜 주제 기사까지 오탐시킴). 대신
# 증정/지급이 명시적으로 붙은 구(phrase) 단위로만 마커에 등록한다.
# "페스티벌"/"문화축제"/"참가신청"/"참가 접수"는 행사 기사 자체의 주제어로도 흔히 쓰여
# marker로 넣지 않는다(예: keyword="문화축제", title="부산 문화축제 참가신청 시작"가
# 오탐될 위험 — 요구사항에 명시된 패턴이라도 marker 리스트에는 넣지 않고, 경품/증정
# 계열과 결합된 구 단위 마커로만 흡수한다).
_INCIDENTAL_MARKERS_STRONG = (
    "증정", "사은품", "판촉물", "경품", "당첨", "상품 제공",
    "선물로 제공", "선물로 지급", "선물 증정",
)
_INCIDENTAL_MARKERS_PROXIMITY_ONLY = ("이벤트", "제공", "지급")
_INCIDENTAL_PROXIMITY_CHARS = 15  # marker가 keyword 앞뒤 이 범위 안에 있어야 근접으로 인정

# clustering 시 token overlap 임계값(Jaccard). 이 이상이면 같은 클러스터.
CLUSTER_JACCARD_THRESHOLD = 0.3

# 상세 articles 노출 최소 relevance_score 기준 — 미만이면 기본 제외(filter_articles_for_display).
LOW_RELEVANCE_ARTICLE_THRESHOLD = 0.3


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _has_keyword_token(keyword: str, text: str) -> bool:
    """keyword의 토큰들이 text 토큰에 (부분적으로라도) 등장하는지."""
    kw_toks = set(_tokens(keyword))
    if not kw_toks:
        return (keyword or "").lower() in (text or "").lower()
    text_toks = set(_tokens(text))
    return bool(kw_toks & text_toks) or any(
        kt in (text or "") for kt in kw_toks
    )


def _find_all(needle: str, haystack: str) -> List[tuple]:
    """needle의 모든 등장 구간(start, end)을 반환(첫 등장만이 아니라 전부 —
    keyword/marker가 반복 등장할 때 실제로 근접한 등장 쌍을 놓치지 않기 위함)."""
    spans = []
    start = 0
    while True:
        idx = haystack.find(needle, start)
        if idx < 0:
            break
        spans.append((idx, idx + len(needle)))
        start = idx + len(needle)
    return spans


def _interval_distance(a_start, a_end, b_start, b_end) -> int:
    """두 구간 사이 문자 거리(겹치면 0)."""
    if a_end <= b_start:
        return b_start - a_end
    if b_end <= a_start:
        return a_start - b_end
    return 0


def _keyword_positions_in(keyword: str, text_low: str) -> List[tuple]:
    kw_toks = [t.lower() for t in set(_tokens(keyword)) if t]
    positions = []
    for kt in kw_toks:
        positions.extend(_find_all(kt, text_low))
    return positions


def _marker_positions_in(text_low: str, markers) -> List[tuple]:
    positions = []
    for marker in markers:
        positions.extend(_find_all(marker, text_low))
    return positions


_TITLE_CLAUSE_BREAKS = (",", "…", "...", "·", "|")


def _title_first_clause(title: str) -> str:
    """title의 첫 절(첫 구분자 이전)을 공백 trim해서 반환. 구분자 없으면 title 전체."""
    t = title or ""
    cut = len(t)
    for b in _TITLE_CLAUSE_BREAKS:
        idx = t.find(b)
        if idx >= 0:
            cut = min(cut, idx)
    return t[:cut].strip()


def _is_keyword_the_whole_first_clause(keyword: str, title: str) -> bool:
    """keyword가 title의 첫 절 "전체"와 정확히 일치하는지(부분 포함이 아니라
    완전 일치) 판정.

    "완전 일치"만 주체로 인정하는 이유(Codex 8차 diff 리뷰 P2 반영):
    - "쿠팡, 선풍기 증정 이벤트" → 첫 절 "쿠팡" == keyword "쿠팡" (완전 일치) →
      쿠팡은 문장의 진짜 주어이므로 marker와 무관하게 주체로 본다.
    - "다이슨 선풍기, 증정 이벤트" → 첫 절 "다이슨 선풍기" != keyword "선풍기"
      (keyword는 첫 절의 일부일 뿐, 절 전체가 아님) → "선풍기"는 "다이슨"이
      수식하는 상품명이지 문장의 독립 주어가 아니므로 marker 판정에서 제외하지
      않는다. 부분 포함까지 주체로 인정하면(6차/7차 시도) "다이슨 선풍기,
      증정 이벤트"의 "선풍기" 같은 부속물까지 marker를 무시해버리는 회귀가
      재발한다 — 완전 일치만 주체로 좁혀야 두 케이스가 동시에 성립한다.
    """
    first_clause = _title_first_clause(title)
    if not first_clause:
        return False
    return _norm_for_loose_compare(first_clause) == _norm_for_loose_compare(keyword)


def _norm_for_loose_compare(s: str) -> str:
    return "".join((s or "").split()).lower()


def _has_marker_near_keyword(keyword: str, title: str, snippet: str) -> bool:
    """incidental 마커가 keyword와 근접(interval distance)하고, keyword가
    title 첫 절 전체와 일치하는 "완전한 주체"가 아닐 때만 incidental로 인정한다.

    - keyword == title 첫 절(완전 일치) → marker 존재와 무관하게 항상 주체로
      보고 incidental 처리하지 않는다(예: "쿠팡, 선풍기 증정 이벤트"의 "쿠팡").
    - 그 외(keyword가 첫 절의 일부이거나, 뒤쪽 절에 있거나, 구분자 자체가
      없는 title)에는 marker와의 순수 거리 판정을 적용한다. 짧은 상품명이
      marker와 붙어 있으면(예: "다이슨 선풍기, 증정 이벤트"의 "선풍기") 여전히
      incidental로 낮아진다.
    """
    if _is_keyword_the_whole_first_clause(keyword, title):
        return False

    text = f"{title} {snippet}"
    text_low = text.lower()

    kw_positions = _keyword_positions_in(keyword, text_low)
    if not kw_positions:
        return False

    all_markers = _INCIDENTAL_MARKERS_STRONG + _INCIDENTAL_MARKERS_PROXIMITY_ONLY
    marker_positions = _marker_positions_in(text_low, all_markers)

    for m_start, m_end in marker_positions:
        for kw_start, kw_end in kw_positions:
            if _interval_distance(m_start, m_end, kw_start, kw_end) <= _INCIDENTAL_PROXIMITY_CHARS:
                return True
    return False


def compute_article_relevance(keyword: str, article: Dict) -> Dict:
    """단일 기사의 키워드 중심성 판정 → {relevance_score, relevance_reason, is_incidental}.

    판정 기준(가벼운 규칙 기반, docs/news-ranking-quality-plan.md 개선4/5):
    - title에 keyword 토큰이 등장 + incidental 마커 없음 → 높은 점수(keyword_main_topic)
    - title에 keyword 없고 description에만 등장 → snippet_only_incidental_mention(낮은 점수)
    - title/description에 incidental 마커가 keyword 근처에 있음 → incidental_giveaway_mention(낮은 점수)
    - 그 외 title에 keyword 있으나 마커도 있음 → 마커 우선(낮은 점수)
    """
    title = article.get("title") or ""
    snippet = article.get("snippet") or ""

    in_title = _has_keyword_token(keyword, title)
    in_desc = _has_keyword_token(keyword, snippet)
    # marker가 keyword와 근접(_INCIDENTAL_PROXIMITY_CHARS 이내)할 때만 incidental로
    # 낮춘다(keyword-relative 판정). 같은 기사라도 keyword마다 marker와의 거리가
    # 다르므로("한국투자증권"은 멀고 "선풍기"는 가까움), 별도 절 구분 없이 순수
    # 거리 기준만으로 주체/부속물이 자연히 구분된다.
    has_marker = _has_marker_near_keyword(keyword, title, snippet)

    if not in_title and not in_desc:
        return {"relevance_score": 0.0, "relevance_reason": "keyword_not_found", "is_incidental": True}

    if in_title and has_marker:
        return {"relevance_score": 0.25, "relevance_reason": "incidental_giveaway_mention", "is_incidental": True}

    if in_title and not has_marker:
        return {"relevance_score": 0.9, "relevance_reason": "keyword_main_topic", "is_incidental": False}

    # title에는 없고 description에만 등장
    if has_marker:
        return {"relevance_score": 0.15, "relevance_reason": "incidental_giveaway_mention", "is_incidental": True}
    return {"relevance_score": 0.2, "relevance_reason": "snippet_only_incidental_mention", "is_incidental": True}


def score_articles_relevance(keyword: str, articles: List[Dict]) -> List[Dict]:
    """articles 각 원소에 relevance_score/relevance_reason/is_incidental 필드를 부여한 복사본 반환.

    relevance_score 내림차순 정렬(동점이면 원 순서 유지 — stable sort).
    """
    scored = []
    for a in articles:
        rel = compute_article_relevance(keyword, a)
        merged = dict(a)
        merged.update(rel)
        scored.append(merged)
    scored.sort(key=lambda a: a["relevance_score"], reverse=True)
    return scored


def filter_articles_for_display(articles: List[Dict], min_count: int = 5) -> List[Dict]:
    """상세 노출용 articles 필터링 — incidental/저관련 기사를 기본 제외한다.

    - 기본: is_incidental=True 이거나 relevance_score < LOW_RELEVANCE_ARTICLE_THRESHOLD인 기사는 제외.
    - 예외(ARTICLES_MIN 하한 보호): 제외 후 남은 기사 수가 min_count 미만이면, 제외했던 기사 중
      relevance_score 높은 순으로 부족분만큼 보충한다. 보충 순서는 비incidental 기사를 먼저,
      incidental 기사는 마지막 우선순위로 둔다(그래도 relevance_score/relevance_reason/
      is_incidental 필드는 그대로 유지 — 프론트/후속 로직이 여전히 판별 가능).
    - 입력은 이미 relevance_score 내림차순 정렬된 상태를 가정(score_articles_relevance 결과).
    """
    def _is_kept(a: Dict) -> bool:
        return not a.get("is_incidental") and a.get("relevance_score", 0.0) >= LOW_RELEVANCE_ARTICLE_THRESHOLD

    kept = [a for a in articles if _is_kept(a)]
    if len(kept) >= min_count:
        return kept

    excluded = [a for a in articles if not _is_kept(a)]
    excluded_sorted = sorted(
        excluded,
        key=lambda a: (bool(a.get("is_incidental")), -a.get("relevance_score", 0.0)),
    )
    need = min_count - len(kept)
    return kept + excluded_sorted[:need]


def cluster_articles(articles: List[Dict]) -> List[List[Dict]]:
    """relevance 반영된 articles를 title/description token overlap으로 경량 클러스터링.

    - 각 기사를 기존 클러스터 중 하나와 비교(Jaccard >= CLUSTER_JACCARD_THRESHOLD)해 병합,
      없으면 새 클러스터 생성.
    - 무거운 NLP 없이 O(n*clusters) 그리디 방식.
    """
    clusters: List[Dict] = []  # [{"tokens": set, "articles": [...]}]
    for a in articles:
        text = f"{a.get('title', '')} {a.get('snippet', '')}"
        toks = set(_tokens(text))
        best_idx = -1
        best_score = 0.0
        for i, c in enumerate(clusters):
            score = _jaccard(toks, c["tokens"])
            if score > best_score:
                best_score = score
                best_idx = i
        if best_idx >= 0 and best_score >= CLUSTER_JACCARD_THRESHOLD:
            clusters[best_idx]["tokens"] |= toks
            clusters[best_idx]["articles"].append(a)
        else:
            clusters.append({"tokens": toks, "articles": [a]})
    return [c["articles"] for c in clusters]


def select_primary_cluster(clusters: List[List[Dict]]) -> List[Dict]:
    """relevance 합/개수 기준으로 primary cluster 선택.

    - relevance가 반영된 articles를 대상으로 하므로, 클러스터별 relevance_score 합이
      가장 큰 클러스터를 primary로 본다(단순 크기만 보면 incidental 기사가 많은
      클러스터가 이길 수 있음 — relevance 가중 필요).
    """
    if not clusters:
        return []
    return max(clusters, key=lambda c: sum(a.get("relevance_score", 0.0) for a in c))


def compute_topic_coherence(clusters: List[List[Dict]], total_articles: int) -> float:
    """primary cluster 비중 기반 topic_coherence(0~1). 기사 주제가 분산될수록 낮음."""
    if not clusters or total_articles <= 0:
        return 0.0
    primary = select_primary_cluster(clusters)
    return round(len(primary) / total_articles, 4)


def select_representative(primary_cluster: List[Dict]) -> Optional[Dict]:
    """primary cluster 안에서 대표 기사 선택.

    - incidental mention 기사는 대표 후보에서 제외.
    - 남은 기사 중 relevance_score가 가장 높은 기사(동점 시 먼저 나온 기사).
    """
    candidates_ = [a for a in primary_cluster if not a.get("is_incidental")]
    if not candidates_:
        return None
    return max(candidates_, key=lambda a: a.get("relevance_score", 0.0))


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

    반환: {recent_count, latest_age_hours, domain_diversity, title_relevance, articles,
           representative_title, representative_summary, representative_article,
           primary_cluster_size, topic_coherence}
    (articles는 relevance_score/relevance_reason/is_incidental 부여 + relevance 내림차순 정렬,
     후속 build 단계 재사용용. 본문 전문 미포함)

    relevance/clustering은 score 계산 전에 여기서 산출한다(ranker의 title_relevance
    penalty가 이 값을 사용하므로 순서가 뒤바뀌면 score에 반영되지 않는다 —
    docs/news-ranking-quality-plan.md §7 Codex P1 반영).
    """
    normalized = []
    for raw in raw_items or []:
        art = normalize_article(raw)
        if art:
            normalized.append(art)
    if not normalized:
        return None

    domains = set()
    recent_count = 0
    ages = []
    for a in normalized:
        url = a.get("url") or ""
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

    # relevance 산출(개선4/5) → articles는 relevance 내림차순으로 재배열됨
    scored_articles = score_articles_relevance(keyword, normalized)

    # clustering(개선2) → primary cluster 기준 representative 선택
    clusters = cluster_articles(scored_articles)
    primary = select_primary_cluster(clusters)
    representative = select_representative(primary)
    topic_coherence = compute_topic_coherence(clusters, len(scored_articles))

    representative_title = (representative or {}).get("title")
    # representative_summary: 대표 기사의 snippet(있으면), 없으면 title로 대체
    representative_summary = None
    if representative:
        representative_summary = representative.get("snippet") or representative.get("title")

    # title_relevance: 기존 ranker penalty가 쓰는 집계 신호. relevance_score 평균으로 강화.
    title_relevance = (
        sum(a["relevance_score"] for a in scored_articles) / len(scored_articles)
        if scored_articles else 0.0
    )

    return {
        "recent_count": recent_count,
        "latest_age_hours": min(ages) if ages else None,
        "domain_diversity": len(domains),
        "title_relevance": title_relevance,
        "articles": scored_articles,
        "representative_title": representative_title,
        "representative_summary": representative_summary,
        "representative_article": representative,
        "primary_cluster_size": len(primary),
        "topic_coherence": topic_coherence,
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
