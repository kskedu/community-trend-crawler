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
from typing import Dict, List, Optional

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
DISPLAY_KEYWORD_MAX_LEN = 18

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
    if "datalab" in available and breakdown["datalab"] > 0:
        parts.append("검색 관심 상승")
    if "google" in available and breakdown["google"] > 0:
        parts.append("구글 신호")
    if "daum" in available and breakdown["daum"] > 0:
        parts.append("실검 보정")
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

    incidental mention 기사(candidates.compute_article_relevance가 is_incidental=True로
    판정한 부수 언급/판촉/증정 기사)는 비교 대상에서 제외한다(Codex diff 리뷰 P2:
    "선풍기 증정" 같은 부수 언급 기사가 다른 후보와 URL/문구를 공유한다는 이유만으로
    same-issue merge되면, article relevance 필터링(개선4/5)의 설계 의도와 충돌한다 —
    부수 언급은애초에 "그 키워드의 핵심 이슈"가 아니므로 이슈 동일성 판정 근거가 될 수 없다).

    기사들을 하나의 token union으로 합쳐서 비교하지 않는다(이전 리뷰 P2 재발 방지: 한
    키워드가 무관 기사를 여러 건 가지고 있으면 union이 커져, 실제로 겹치는 기사 쌍이
    있어도 전체 Jaccard가 희석돼 놓칠 수 있었음). 대신 A의 기사 하나하나를 B의 기사
    하나하나와 짝지어 비교해 가장 높은 pair의 overlap을 채택한다.
    """
    relevant_a = [a for a in (articles_a or []) if not a.get("is_incidental")]
    relevant_b = [b for b in (articles_b or []) if not b.get("is_incidental")]

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


def _build_display_keyword(members: List[Dict]) -> str:
    """same-issue merge된 후보들에서 display_keyword 생성.

    우선순위(요구사항 대표 키워드 선택 기준):
    1. 너무 일반적인 단독 단어(_TOO_BROAD_SINGLE_WORDS)는 단독 대표로 쓰지 않는다.
    2. 기존 후보 중 하나가 이미 다른 후보의 토큰을 포함하는 조합형 키워드면
       (예: "김영환 압수수색"이 "압수수색"/"김영환"을 모두 포함) 그걸 그대로 쓴다.
    3. 그렇지 않으면(다들 단편적 단일 개념) 서로 다른 두 키워드를 조합해
       사건 맥락을 드러낸다(12~18자 제한).
    """
    keywords = [m["keyword"] for m in members]
    specific = [k for k in keywords if k not in _TOO_BROAD_SINGLE_WORDS]
    pool = specific or keywords
    pool_sorted = sorted(pool, key=len, reverse=True)

    best = pool_sorted[0]
    best_toks = set(best)
    # best가 다른 후보들의 토큰을 이미 포함하는 조합형 표현인지 확인
    covers_others = all(
        (not set(k) - best_toks) or k == best for k in pool_sorted[1:]
    )
    if covers_others and len(pool_sorted) > 1:
        return best[:DISPLAY_KEYWORD_MAX_LEN]

    # 단편적 키워드들 → 서로 다른 상위 두 키워드를 조합(중복 없이)
    combined = best
    for k in pool_sorted[1:]:
        if k in combined:
            continue
        candidate = f"{k} {combined}"
        if len(candidate) <= DISPLAY_KEYWORD_MAX_LEN:
            combined = candidate
        break
    return combined[:DISPLAY_KEYWORD_MAX_LEN]


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
                other_articles = (other.get("news_meta") or {}).get("articles") or []
                matched = any(
                    _article_overlap((m.get("news_meta") or {}).get("articles") or [], other_articles)
                    >= MERGE_ARTICLE_OVERLAP_THRESHOLD
                    for m in group
                )
                if matched:
                    group.append(other)
                    consumed.add(okw)
                    article_merge_count += 1
                    changed = True

        if len(group) == 1:
            merged = dict(item)
            merged.setdefault("related_keywords", [])
            merged.setdefault("aliases", [])
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


def select_top(ranked: List[Dict], top_n: int = TOP_N) -> List[Dict]:
    """dedupe/merge 이후 리스트에서 상위 N개를 뽑는다.

    dedupe_and_merge()가 이미 selected-set 누적 방식으로 재중복을 제거했으므로
    여기서는 단순 슬라이스만 수행한다(부족하면 dedupe_and_merge가 알아서 다음
    후보를 그룹에 포함시키지 않은 나머지 항목이 순서대로 이어져 자연스럽게 backfill됨).
    """
    return ranked[:top_n]
