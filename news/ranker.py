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
# article 그룹 간 공유 사건 토큰(문서빈도>=2 근사) 기반 보조 merge 신호.
# article overlap(개별 기사 pairwise Jaccard)이 표현 차이로 낮게 나오는 same-issue 케이스를
# 보완한다. "겹치는 사건 토큰 최소 개수" + "keyword anchor 교차 등장" 조건을 모두 만족해야
# merge 근거로 인정한다(일반 서술어 1~2개 겹침으로 오탐 병합되는 것을 막기 위함).
REPRESENTATIVE_OVERLAP_MIN_SHARED_TOKENS = 2
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

    news_map = signals.get("news") or {}
    datalab_map = signals.get("datalab") or {}
    google_map = signals.get("google") or {}
    daum_map = signals.get("daum") or {}

    # --- keyword-level quality gate (범위 한정: 이번에 새로 추가하는 gate 대상만 정규화
    # 이전에 제외한다. "news 신호 자체가 없는 후보"가 rc_raw/delta_raw/g_raw/d_raw 등
    # 정규화 입력에 섞였다가 메인 루프 사후 continue로만 걸러지는 기존 구조(290163d 이전
    # 부터 존재)는 이번 스코프 밖으로 분리하고 후속 이슈로 남긴다 — compute_scores()
    # 정규화 파이프라인 전체 재작성은 이번 작업 범위가 아니다(사용자 승인).
    #
    # quality gate 조건: 고관련 기사(candidates.HIGH_RELEVANCE_THRESHOLD 이상)가 2건 미만
    # 이고 quality_cluster_size(고관련 기사만의 primary cluster 크기)도 2 미만이면, 그
    # keyword는 관련 기사가 사실상 없는 것으로 보고 후보에서 완전히 제외한다(감점이 아니라
    # hard exclude — 감점만으로는 score가 높은 다른 신호 덕에 여전히 Top10에 남을 수 있음).
    #
    # news_available_before_gate를 quality gate 필터링 *이전*에 원본 keywords 기준으로
    # 먼저 확정한다(Codex review-only P1: quality gate로 news 있는 후보가 전부 걸러지면
    # available["news"] 판정 자체가 꺼져, 이후 news-required 최종 제외(메인 루프의
    # "if 'news' in available and not news_map.get(k): continue")도 무력화되고 datalab/
    # google/daum만으로 결과에 들어오는 새로운 회귀가 생긴다).
    news_available_before_gate = any(news_map.get(k) for k in keywords)

    def _passes_keyword_quality_gate(news_meta: Dict) -> bool:
        hrc = news_meta.get("high_relevance_count", 0)
        qcs = news_meta.get("quality_cluster_size", 0)
        return hrc >= 2 or qcs >= 2

    candidates = [
        c for c in candidates
        if news_map.get(c["keyword"]) is None or _passes_keyword_quality_gate(news_map.get(c["keyword"]))
    ]
    keywords = [c["keyword"] for c in candidates]
    if not keywords:
        return []

    # --- 가용 신호 판정 (소스 단위) ---
    available = {}
    if news_available_before_gate:
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
_GENERIC_EVENT_PREDICATE_WORDS = {"발표", "오늘", "내용", "관련", "예정", "공개", "진행"}


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
    """item의 keyword 자체를 토큰화한 집합(2자 이상). same-issue merge의 precision
    게이트로 쓴다 — "정부 오늘 새 정책 발표" vs "기업 오늘 실적 발표"처럼 사건 자체가
    다른데 흔한 서술어만 겹치는 경우, 상대 keyword가 서로의 기사/키워드에 전혀
    등장하지 않으므로 이 게이트에서 막힌다.
    """
    from news.summarizer import _tokens

    return {t for t in _tokens(item.get("keyword", "")) if len(t) >= 2}


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

    # 단편적 키워드들 → 서로 다른 상위 두 키워드를 조합(중복 없이). best(가장 길고 이미
    # 사건 맥락을 담고 있을 가능성이 높은 키워드)를 앞에, 보조 키워드를 뒤에 붙인다
    # (Codex review-only 조언: same-issue merge 확장으로 "배재고 출전정지"+"권오영 감독"처럼
    # 서로 무관한 문자 집합을 가진 키워드가 merge될 때, 기존 "{k} {combined}" 순서는
    # 인명을 사건 키워드 앞에 붙여 "권오영 감독 배재고 출전정지"처럼 부자연스러워졌다.
    # "{combined} {k}"로 순서를 반전하면 이미 완결된 사건 표현이 앞에 오고 보조 정보가
    # 뒤에 붙어 "배재고 출전정지 권오영 감독"이 되고, 기존 "압수수색 영장"+"김영환" 같은
    # 케이스도 "압수수색 영장 김영환"으로 정보 손실 없이 조합된다).
    combined = best
    for k in pool_sorted[1:]:
        if k in combined:
            continue
        candidate = f"{combined} {k}"
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
