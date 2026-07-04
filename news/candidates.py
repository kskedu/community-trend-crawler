"""후보 수집/병합 + News 신호 산출.

설계 계약 (docs/news-ranking-plan.md §3, §4-2, docs/news-ranking-quality-plan.md §7):
- 후보 pool = 홈/트렌드 seed(google_trends/daum_home/nate_home/bing_home)
  + 경량 보조후보(naver_news_aux: 뉴스 title 토큰, naver_news_phrase: 사건형 phrase).
- normalize/dedup 후 상한(기본 30)으로 자른다.
- News 신호(recent_count/latest_age_hours/domain_diversity/title_relevance)는
  normalizer 결과에서 파생 — 기사 본문 전문 저장 없음.
- 다양성 guard: 독립 홈/트렌드 source family 종수 < MIN_SOURCE_FAMILIES 이면
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
from news.summarizer import _tokens, summarize  # 기존 토크나이저/요약 로직 재사용(신규 의존성 없음)

logger = logging.getLogger(__name__)

CANDIDATE_MAX = 30
AUX_SEED_TOP = 5        # 보조후보 추출에 쓸 daum_home 상위 키워드 수
AUX_MAX = 8             # 보조후보 최대 개수
RECENT_HOURS = 12       # News 최근성 기준
MIN_SOURCE_FAMILIES = 2   # 독립 홈/트렌드 source family 최소 종수(다양성 guard)

# === backfill pass(최소 10개 확보) 전용 상수 — strict pass(pass1)에는 영향 없음 ===
BACKFILL_CANDIDATE_MAX = 45   # pass2 병합 pool 상한(daum10+danawa10+aux12+phrase10 수용)
AUX_SEED_TOP_BACKFILL = 10    # pass2 aux 확장: daum 전체(Top10)에서 보조후보 추출
AUX_MAX_BACKFILL = 12
PHRASE_MAX = 10               # phrase 후보 상한(신규 API fetch 증가 억제)
PHRASE_NGRAM_MIN = 2
PHRASE_NGRAM_MAX = 4
PHRASE_MIN_DF = 2             # phrase가 서로 다른 기사 몇 건에 등장해야 후보로 인정하는가

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

# object/side-mention 판정 마커 — keyword가 기사 핵심 주제가 아니라 "조치 대상 물품/
# 소지품"으로만 언급되는 문맥(예: "노트북 회수까지 지시" — 실제 주제는 쿠팡-국정원 갈등).
# 경품/판촉(_INCIDENTAL_MARKERS_*)과는 다른 의미 축이라 별도 상수로 분리한다.
# "물품"/"장비"/"지급"/"지원"/"전달"/"차원"/"조치"/"대상"은 일반 기사에도 매우 흔해
# ("노트북 지원 사업", "장비 지원금" 등) 단독으로는 오탐 위험이 커서 제외한다.
_SIDE_MENTION_MARKERS = ("회수", "압수", "제출", "반납", "수거", "압수수색", "소지품", "대상으로")

# clustering 시 token overlap 임계값(Jaccard). 이 이상이면 같은 클러스터.
CLUSTER_JACCARD_THRESHOLD = 0.3

# 상세 articles 노출 최소 relevance_score 기준 — 미만이면 기본 제외(filter_articles_for_display).
LOW_RELEVANCE_ARTICLE_THRESHOLD = 0.3

# keyword-level quality gate: 이 값 이상인 기사만 "고관련"으로 집계(compute_news_signal).
HIGH_RELEVANCE_THRESHOLD = 0.7

# fresh relevance gate: 고관련 기사 중 이 시간 이내인 것만 "신선한 고관련"으로 집계.
# 관련성은 높지만 전부 오래된 기사(제품 리뷰/도입기 등)만 있는 키워드가 Top10에
# backfill되는 것을 막기 위한 hard gate 전용 기준(ranker._passes_keyword_quality_gate).
# 기존 RECENT_HOURS(12h, News 서브스코어 freshness용)와는 목적이 달라 별도 상수로 둔다.
FRESH_RELEVANCE_HOURS = 72

# select_representative()가 대표로 인정하는 최소 relevance_score.
REPRESENTATIVE_MIN_RELEVANCE = 0.5

# select_representative()에서 description hygiene 가산점/감점 폭(2026-07-04).
# relevance_score 자체(ranking/gate가 쓰는 값)는 건드리지 않고, 대표 기사 "선택" 시에만
# clean_description 사용 가능 여부로 소폭 가감한다 — 오염된 description을 가진 기사가
# 근소한 relevance 차이로 대표가 되는 것을 방지.
_DESC_QUALITY_BONUS = 0.05

# === PR/광고성 클러스터 판정 (실시간 이슈 품질 — 문제 B) ===
# PR 마커는 title에서만 substring 판정한다. snippet까지 보면 경기 결과 기사의 후원 boilerplate가
# 오탐되어 정상 스포츠 이슈가 제외될 수 있다(Codex review-only 3·6차: 정상 이슈 오제외가 PR
# 누수보다 나쁘다는 우선순위). keyword 자체가 될 수 있는 단독어(팝업/콜라보/프로모션)는 제외하고
# phrase형만 둔다(Codex 6차 P2). 실기사 표기 변형(공식후원/공식 후원, 체험부스/체험 부스)을 함께
# 등록하고 매칭 시 양쪽 공백 제거로 spacing 변형을 포섭한다.
_PR_MARKERS = (
    "공식 후원", "후원사", "후원 협약", "협찬사",
    "체험존", "체험 부스", "체험 마케팅", "브랜드관",
    "팝업스토어", "플래그십스토어", "플래그십", "스위트라운지", "VIP 라운지",
    "앰버서더", "홍보대사", "시승 행사", "나눔 행사", "사회공헌",
    "ESG 경영", "신제품 출시", "브랜드 캠페인", "협업 이벤트",
)

# public-interest override — 두 등급(사용자 확정 2026-07-03).
# (1) 강한 사건성: title에 1건만 있어도 PR hard exclude를 무효화하고 유지한다(PR 마커 동반해도).
#     브랜드 PR 문맥에 우연히 섞일 가능성이 낮고, 정상 사건 이슈 오제외가 더 나쁘다.
_STRONG_PUBLIC_INTEREST_TOKENS = {
    "사고", "리콜", "소송", "규제", "파업", "범죄", "사망", "재난",
    "화재", "기소", "수사", "제재",
}
# 길어서 substring FP가 없는 compound는 substring + 공백 제거 변형("압수 수색")으로 포섭.
_STRONG_PUBLIC_INTEREST_COMPOUNDS = ("압수수색", "보안사고", "과징금")
# (2) 약한 시장성: 마케팅 title에도 흔하므로, 같은 기사(title+snippet)에 PR 마커가 없을 때만
#     override로 인정한다("브랜드 캠페인 효과로 실적 기대"류를 PR로 유지 — 사용자 확정).
_MARKET_TOKENS = {"실적", "주가", "매출", "영업이익", "순이익", "전망", "기대"}


def _text_has_pr_marker(text: str) -> bool:
    """text(주로 title)에 PR 마커가 substring으로 있는지. 양쪽 공백 제거로 spacing 변형 포섭."""
    t = text or ""
    t_nospace = "".join(t.split())
    for m in _PR_MARKERS:
        if m in t or "".join(m.split()) in t_nospace:
            return True
    return False


def _has_strong_public_interest(title: str) -> bool:
    if set(_tokens(title)) & _STRONG_PUBLIC_INTEREST_TOKENS:
        return True
    t_nospace = "".join((title or "").split())
    return any(c in t_nospace for c in _STRONG_PUBLIC_INTEREST_COMPOUNDS)


def is_public_interest(article: Dict) -> bool:
    """공익/사건 override 대상 기사인지(두 등급, title 기준).

    강한 사건성 토큰이 title에 있으면 무조건 True. 약한 시장성 토큰(실적/주가 등)은 같은
    기사(title+snippet)에 PR 마커가 없을 때만 True(마케팅 hype 제외 — 사용자 확정 2026-07-03).
    토큰 매칭(summarizer._tokens)이라 "무사고"⊅"사고", "주가지수"⊅"주가" 오탐이 없다.
    """
    title = article.get("title") or ""
    if _has_strong_public_interest(title):
        return True
    if set(_tokens(title)) & _MARKET_TOKENS:
        snippet = article.get("snippet") or ""
        if not _text_has_pr_marker(title) and not _text_has_pr_marker(snippet):
            return True
    return False


def is_promotional_pr(article: Dict) -> bool:
    """title에 PR 마커가 있고 공익 override 대상이 아닌 기사(PR/광고성)."""
    if not _text_has_pr_marker(article.get("title") or ""):
        return False
    return not is_public_interest(article)


def _is_issue_defining_article(article: Dict) -> bool:
    """PR ratio 분모로 쓸 "그 keyword의 이슈를 정의하는 기사"인지.
    부수 언급(is_incidental)·조치 대상 물품 언급(object_side_mention)·저관련 기사는 제외."""
    if article.get("is_incidental"):
        return False
    if article.get("relevance_reason") == "object_side_mention":
        return False
    return article.get("relevance_score", 0.0) >= LOW_RELEVANCE_ARTICLE_THRESHOLD


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


def _has_all_keyword_tokens(keyword: str, text: str) -> bool:
    """keyword의 모든 토큰이 text에 (소문자 substring으로) 등장하는지 — phrase 후보 전용
    strict 판정. exact token subset이 아니라 substring 포함으로 본다(Codex 계획 리뷰 P2:
    "국가수사본부장에"처럼 조사/어미가 붙은 정상 제목이 exact 비교로 대량 탈락하면
    backfill이 무력화됨)."""
    kw_toks = [t.lower() for t in set(_tokens(keyword))]
    text_low = (text or "").lower()
    if not kw_toks:
        return (keyword or "").lower() in text_low
    return all(kt in text_low for kt in kw_toks)


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


def _has_marker_near_keyword(keyword: str, title: str, snippet: str, markers=None) -> bool:
    """마커가 keyword와 근접(interval distance)하고, keyword가 title 첫 절 전체와
    일치하는 "완전한 주체"가 아닐 때만 True를 반환한다.

    - keyword == title 첫 절(완전 일치) → marker 존재와 무관하게 항상 주체로
      보고 incidental/side-mention 처리하지 않는다(예: "쿠팡, 선풍기 증정 이벤트"의 "쿠팡").
    - 그 외(keyword가 첫 절의 일부이거나, 뒤쪽 절에 있거나, 구분자 자체가
      없는 title)에는 marker와의 순수 거리 판정을 적용한다. 짧은 상품명이
      marker와 붙어 있으면(예: "다이슨 선풍기, 증정 이벤트"의 "선풍기") 여전히
      incidental로 낮아진다.

    markers: 판정에 쓸 마커 목록. None이면 기존 경품/판촉 마커
    (_INCIDENTAL_MARKERS_STRONG + _INCIDENTAL_MARKERS_PROXIMITY_ONLY)를 기본값으로 쓴다
    (object/side-mention 판정 등 다른 마커 집합 재사용을 위한 일반화 — 기존 호출부는
    인자를 생략하면 이전과 동일하게 동작).
    """
    if _is_keyword_the_whole_first_clause(keyword, title):
        return False

    text = f"{title} {snippet}"
    text_low = text.lower()

    kw_positions = _keyword_positions_in(keyword, text_low)
    if not kw_positions:
        return False

    if markers is None:
        markers = _INCIDENTAL_MARKERS_STRONG + _INCIDENTAL_MARKERS_PROXIMITY_ONLY
    marker_positions = _marker_positions_in(text_low, markers)

    for m_start, m_end in marker_positions:
        for kw_start, kw_end in kw_positions:
            if _interval_distance(m_start, m_end, kw_start, kw_end) <= _INCIDENTAL_PROXIMITY_CHARS:
                return True
    return False


def compute_article_relevance(keyword: str, article: Dict, require_all_tokens: bool = False) -> Dict:
    """단일 기사의 키워드 중심성 판정 → {relevance_score, relevance_reason, is_incidental}.

    판정 기준(가벼운 규칙 기반, docs/news-ranking-quality-plan.md 개선4/5):
    - title에 keyword 토큰이 등장 + incidental 마커 없음 → 높은 점수(keyword_main_topic)
    - title에 keyword 없고 description에만 등장 → snippet_only_incidental_mention(낮은 점수)
    - title/description에 incidental 마커가 keyword 근처에 있음 → incidental_giveaway_mention(낮은 점수)
    - 그 외 title에 keyword 있으나 마커도 있음 → 마커 우선(낮은 점수)

    require_all_tokens(phrase 후보 전용, Codex 계획 리뷰 P1): 다어절 phrase 후보는
    토큰 하나만 title에 있어도 0.9가 되는 기존 판정으로는 일부 토큰만 겹치는 기사로
    quality gate를 통과할 수 있다. True면 keyword의 모든 토큰이 존재해야 등장으로
    인정한다(기존 seed/aux 후보는 기본값 False — 동작 불변).
    """
    title = article.get("title") or ""
    snippet = article.get("snippet") or ""

    _matcher = _has_all_keyword_tokens if require_all_tokens else _has_keyword_token
    in_title = _matcher(keyword, title)
    in_desc = _matcher(keyword, snippet)
    # marker가 keyword와 근접(_INCIDENTAL_PROXIMITY_CHARS 이내)할 때만 incidental로
    # 낮춘다(keyword-relative 판정). 같은 기사라도 keyword마다 marker와의 거리가
    # 다르므로("한국투자증권"은 멀고 "선풍기"는 가까움), 별도 절 구분 없이 순수
    # 거리 기준만으로 주체/부속물이 자연히 구분된다.
    has_marker = _has_marker_near_keyword(keyword, title, snippet)
    # object/side-mention: keyword가 기사 핵심 주제가 아니라 "조치 대상 물품"으로만
    # 언급되는 문맥(예: "노트북 회수까지 지시" — 실제 주제는 쿠팡-국정원 갈등). 경품/판촉
    # 마커와는 다른 의미 축이므로 별도 마커 목록(_SIDE_MENTION_MARKERS)으로 판정한다.
    has_side_mention = _has_marker_near_keyword(keyword, title, snippet, markers=_SIDE_MENTION_MARKERS)

    if not in_title and not in_desc:
        return {"relevance_score": 0.0, "relevance_reason": "keyword_not_found", "is_incidental": True}

    if in_title and has_marker:
        return {"relevance_score": 0.25, "relevance_reason": "incidental_giveaway_mention", "is_incidental": True}

    if in_title and has_side_mention:
        # 완전히 무관하지는 않음(keyword가 실제로 언급됨)이나 기사 핵심 주제는 아니므로
        # is_incidental=True로 두지 않는다(요구사항: incidental과는 다른 신호). 대신
        # relevance_score를 낮춰 representative 선택/keyword quality gate에서 불리하게
        # 반영한다(select_representative의 REPRESENTATIVE_MIN_RELEVANCE, ranker의
        # HIGH_RELEVANCE_THRESHOLD 둘 다 이 값 미만).
        return {"relevance_score": 0.35, "relevance_reason": "object_side_mention", "is_incidental": False}

    if in_title and not has_marker:
        return {"relevance_score": 0.9, "relevance_reason": "keyword_main_topic", "is_incidental": False}

    # title에는 없고 description에만 등장
    if has_marker:
        return {"relevance_score": 0.15, "relevance_reason": "incidental_giveaway_mention", "is_incidental": True}
    if has_side_mention:
        # title에 keyword가 없어 이미 relevance가 낮지만(0.2 미만), reason을
        # object_side_mention으로 명시해 _is_same_issue_evidence_article()의 same-issue
        # merge 근거 배제 대상에도 포함시킨다(Codex review-only P2: in_title 조건에만
        # 걸리면 snippet-only side-mention 기사가 snippet_only_incidental_mention으로
        # 분류돼 merge 근거로 남는 우회 경로가 생김).
        return {"relevance_score": 0.1, "relevance_reason": "object_side_mention", "is_incidental": True}
    return {"relevance_score": 0.2, "relevance_reason": "snippet_only_incidental_mention", "is_incidental": True}


def score_articles_relevance(keyword: str, articles: List[Dict], require_all_tokens: bool = False) -> List[Dict]:
    """articles 각 원소에 relevance_score/relevance_reason/is_incidental 필드를 부여한 복사본 반환.

    relevance_score 내림차순 정렬(동점이면 원 순서 유지 — stable sort).
    """
    scored = []
    for a in articles:
        rel = compute_article_relevance(keyword, a, require_all_tokens=require_all_tokens)
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
    - relevance_score가 REPRESENTATIVE_MIN_RELEVANCE 미만인 기사도 제외한다(object_side_mention
    (0.35)처럼 is_incidental=False이지만 기사 핵심 주제가 아닌 기사가 대표로 뽑히는 것을 방지 —
    예: "노트북 회수까지 지시" 기사가 keyword="노트북"의 대표로 선택되던 문제).
    - 남은 기사 중 relevance_score가 가장 높은 기사(동점 시 먼저 나온 기사).
    """
    candidates_ = [
        a for a in primary_cluster
        if not a.get("is_incidental") and a.get("relevance_score", 0.0) >= REPRESENTATIVE_MIN_RELEVANCE
    ]
    if not candidates_:
        return None
    return max(candidates_, key=_representative_sort_key)


def _representative_sort_key(a: Dict) -> float:
    """대표 기사 선택 전용 정렬 키 — relevance_score + description hygiene 가감(2026-07-04)."""
    score = a.get("relevance_score", 0.0)
    if a.get("is_description_usable_for_summary"):
        score += _DESC_QUALITY_BONUS
    elif a.get("description_drop_reason"):
        score -= _DESC_QUALITY_BONUS
    return score


def build_representative_summary(primary_cluster: List[Dict], representative: Optional[Dict]) -> Optional[str]:
    """대표 소개글(representative_summary) 산출 — description hygiene 정책(2026-07-04).

    - 대표 기사의 clean_description이 있으면 사용(단일 기사 그대로 노출해도 안전).
    - 없으면(캡션만 있어 제외됐거나 description 자체가 없으면) primary cluster의
      여러 clean title 기반 공통 이슈 문장(summarizer.summarize 재사용)으로 대체 —
      단일 기사의 raw description 복붙을 피한다(요구사항: clean title 여러 개 우선,
      clean_description은 보조).
    - 그마저 없으면 대표 기사 title.
    """
    if representative and representative.get("is_description_usable_for_summary") and representative.get("clean_description"):
        return representative["clean_description"]
    # select_representative()와 동일한 최소 relevance 기준을 적용한다 — 그렇지 않으면
    # object_side_mention(0.35)처럼 대표 자격이 없는 기사(예: "노트북 회수까지 지시")가
    # 다중 title 합의 재료로 섞여 들어가 대표 소개글에 우회 노출될 수 있다
    # (Codex review-only P2 6차, 2026-07-04).
    usable_articles = [
        a for a in (primary_cluster or [])
        if not a.get("is_incidental") and a.get("relevance_score", 0.0) >= REPRESENTATIVE_MIN_RELEVANCE
    ]
    if usable_articles:
        text, _ = summarize("", usable_articles)
        if text:
            return text
    return (representative or {}).get("title")


# === display_articles(2026-07-04) — 상세 팝업 노출 전용 필터 =================================
# 문제: 여러 seed/aux 후보가 same-issue merge로 하나의 keyword로 묶이면, 그 keyword 문자열
# 자체가 "도깨비 10주년 여행 공유"처럼 여러 단어의 조합이 된다. _has_keyword_token()은
# keyword 토큰 "하나만" 겹쳐도 매칭으로 보므로("공유"라는 흔한 단어), 배우 "공유"와 무관한
# "성과 공유"/"계획 공유" 기사가 relevance_score만 높게 나와 evidence용 articles에 섞여
# 들어올 수 있다. display_articles는 이 articles(랭킹/게이트 근거, 변경 없음)를 사용자
# 노출 직전에 한 번 더 걸러내는 별도 레이어다 — ranking/gate 로직 자체는 건드리지 않는다.
_GENERIC_SINGLE_TOKENS = {
    "공유", "조사", "수사", "계획", "지원", "성과", "호황", "반등", "논란", "관련", "발표",
}


def _matched_tokens(tokens: set, text: str, text_tokens: set) -> set:
    """tokens 중 text에 실제 등장하는 것 — 정확 토큰 매칭 또는 substring 포함(조사/어미
    결합 대응, 예: "도깨비에서"에 "도깨비"가 포함). 기존 _has_keyword_token과 동일한
    substring 완화를 적용해, 정상 연관 기사가 조사 결합 때문에 억울하게 빠지지 않게 한다."""
    text_low = (text or "").lower()
    return {t for t in tokens if t in text_tokens or t.lower() in text_low}


def _display_anchor_allowed(effective_keyword: str, article: Dict, representative: Optional[Dict]) -> bool:
    """primary cluster에 속하지 않는 기사가 display_articles에 남을 수 있는 예외 조건.

    - display_keyword 비-모호(non-generic) 토큰만으로 2개 이상 겹치면 허용.
    - 비-모호 토큰 1개 + 모호 토큰 포함 전체 2개 이상 겹치는 경우("공유"+"도깨비")는,
      대표 기사와도 비-모호 토큰을 1개 이상 공유할 때만 허용한다. 이 이중 확인이 없으면
      "여행"처럼 keyword엔 있지만 그 자체로 매우 흔한 단어가 "공유"와 우연히 함께 등장하는
      무관 기사("여행 계획 공유 앱 출시")까지 새어나간다(Codex review-only P2, 2026-07-04).
    - 위 조건이 안 되면, 대표 기사 title과 비-모호 토큰을 2개 이상 공유하는지로 재확인.
    - "공유"처럼 모호한 단일 토큰 하나만 겹치는 기사는 제외.
    """
    text = f"{article.get('title', '')} {article.get('clean_description') or article.get('snippet', '')}"
    text_tokens = set(_tokens(text))

    rep_title = (representative or {}).get("title") or ""
    rep_tokens = set(_tokens(rep_title))
    shared_with_rep_all = _matched_tokens(rep_tokens, text, text_tokens)
    shared_with_rep = shared_with_rep_all - _GENERIC_SINGLE_TOKENS

    kw_tokens = set(_tokens(effective_keyword))
    matched_kw = _matched_tokens(kw_tokens, text, text_tokens)
    non_generic_matched = matched_kw - _GENERIC_SINGLE_TOKENS
    if len(non_generic_matched) >= 2:
        return True
    if len(non_generic_matched) >= 1 and len(matched_kw) >= 2 and len(shared_with_rep) >= 1:
        return True

    if len(shared_with_rep) >= 2:
        return True
    return False


def build_display_articles(
    effective_keyword: str, articles: List[Dict], representative: Optional[Dict] = None
) -> List[Dict]:
    """상세 팝업 노출 전용 목록 — articles(랭킹/게이트 근거)는 그대로 두고 별도로 생성한다.

    - 대표 기사와 같은 primary cluster 기사는 우선 포함(compute_news_signal이 미리 표시한
      is_primary_cluster 사용).
    - 그 외는 _display_anchor_allowed()로 키워드/대표 title과의 실질적 연관성을 재확인한다.
    - 부족해도 엉뚱한 기사로 채우지 않는다(하한 backfill 없음 — 사용자 정책).
    """
    out = []
    for a in articles or []:
        if a.get("is_primary_cluster"):
            out.append(a)
            continue
        if _display_anchor_allowed(effective_keyword, a, representative):
            out.append(a)
    return out


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
    seed_sources: Dict[str, List[dict]],
    aux_keywords: List[str],
    phrase_keywords: Optional[List[str]] = None,
    limit: int = CANDIDATE_MAX,
) -> List[dict]:
    """홈/트렌드 seed + 보조후보를 병합/dedup → [{keyword, sources:{...}}] (상한 적용).

    seed_sources: {family: [{keyword, rank}]}. family 키는 그대로 candidate.sources 키가 된다
      (google_trends/daum_home/nate_home/bing_home 등). aux/phrase는 naver_news_* 로 표기.
    phrase_keywords: backfill pass 전용 phrase 후보(derive_phrase_candidates 결과).
    strict pass(pass1) 호출부는 phrase_keywords를 생략하면 기존과 동일하게 동작한다.
    """
    pool: Dict[str, dict] = {}
    for family, ranked in (seed_sources or {}).items():
        for item in ranked or []:
            _merge(pool, item.get("keyword"), family, item.get("rank"))
    for kw in aux_keywords or []:
        _merge(pool, kw, "naver_news_aux", None)
    for kw in phrase_keywords or []:
        _merge(pool, kw, "naver_news_phrase", None)

    candidates = list(pool.values())
    # 후보 pool 절단(truncation) 방지용 정렬: 특정 family 단독 기준(예전 daum_home rank)으로
    # 정렬하면 홈 3종이 모두 fresh할 때 뒤에 병합된 google_trends가 상한에 밀려 통째로
    # 잘려나간다(Codex diff 리뷰 P1). 독립 family 중 "최상 rank"를 1차 키로 삼아 각 family의
    # 상위 후보가 고르게 생존하도록 한다. 동률이면 family priority → keyword로 안정 정렬.
    # 이 정렬은 어디까지나 truncation 방지용이고, 최종 순위는 ranker가 다시 계산한다.
    candidates.sort(key=lambda c: (_best_family_rank(c), _best_family_priority(c), c["keyword"]))
    return candidates[:limit]


# 독립 홈/트렌드 source family — 다양성 guard / source consensus는 이 집합만 센다.
#   naver_news_aux/phrase는 Daum 상위 키워드/기사에서 파생되므로 독립 검색 source가 아니다
#   (Gate 7: naver_news_* 는 독립 검색 source로 계산하지 않는다).
#   naver_home은 수집 소스가 아직 없어 예약만 해 둔다(현재 후보에 등장하지 않음).
_INDEPENDENT_SEARCH_FAMILIES = {
    "google_trends", "naver_home", "daum_home", "nate_home", "bing_home",
}

# family 동률 tie-breaker 우선순위(작을수록 우선). truncation 정렬 전용 — 최종 ranking과 무관.
_FAMILY_SORT_PRIORITY = {
    "google_trends": 0, "bing_home": 1, "daum_home": 2, "nate_home": 3,
}


def _best_family_rank(candidate: dict) -> int:
    """후보의 독립 홈/트렌드 family rank 중 최상(최소)값. rank 없으면 9999.
    naver_news_aux/phrase의 True(bool)는 rank가 아니므로 제외."""
    ranks = [
        r for fam, r in (candidate.get("sources") or {}).items()
        if fam in _INDEPENDENT_SEARCH_FAMILIES
        and isinstance(r, int) and not isinstance(r, bool)
    ]
    return min(ranks) if ranks else 9999


def _best_family_priority(candidate: dict) -> int:
    """후보가 rank를 가진 독립 family 중 tie-break 우선순위 최상값(작을수록 우선). 없으면 99.

    _best_family_rank과 동일하게 "유효 int rank를 가진 family"만 센다 — rank 없는 항목
    (naver_news_aux/phrase의 True, 또는 rankless 독립 family)이 우선순위에 끼어들어
    (best_rank=9999, priority=0)처럼 꼬리 후보를 잘못 앞세우는 것을 막는다(Codex 2차 P2)."""
    prios = [
        _FAMILY_SORT_PRIORITY[fam]
        for fam, r in (candidate.get("sources") or {}).items()
        if fam in _FAMILY_SORT_PRIORITY
        and isinstance(r, int) and not isinstance(r, bool)
    ]
    return min(prios) if prios else 99


def count_source_families(candidates: List[dict]) -> int:
    """후보 pool 전체에서 등장한 독립 홈/트렌드 source family 종수(다양성 guard용).

    naver_news_aux/phrase만 가진 후보는 새 family를 더하지 않는다 → 진짜 독립 source만 카운트.
    """
    families: set = set()
    for c in candidates:
        families |= set(c["sources"].keys()) & _INDEPENDENT_SEARCH_FAMILIES
    return len(families)


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


def compute_news_signal(keyword: str, raw_items: List[dict], require_all_tokens: bool = False) -> Optional[dict]:
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
    scored_articles = score_articles_relevance(keyword, normalized, require_all_tokens=require_all_tokens)

    # clustering(개선2) → primary cluster 기준 representative 선택
    clusters = cluster_articles(scored_articles)
    primary = select_primary_cluster(clusters)
    representative = select_representative(primary)
    topic_coherence = compute_topic_coherence(clusters, len(scored_articles))

    # display_articles(2026-07-04) 판정용 — 대표 이슈와 같은 primary cluster에 속하는지
    # 기사별로 표시해둔다. dedup/filter 단계가 같은 dict 참조를 그대로 넘기므로 이 필드는
    # builder까지 보존된다(build_display_articles 참고).
    primary_ids = {id(a) for a in primary}
    for a in scored_articles:
        a["is_primary_cluster"] = id(a) in primary_ids

    representative_title = (representative or {}).get("title")
    # representative_summary: description hygiene 정책 적용(build_representative_summary,
    # 2026-07-04) — 대표 기사 raw snippet을 그대로 쓰지 않는다(이미지 캡션 노출 방지).
    representative_summary = build_representative_summary(primary, representative)

    # title_relevance: 기존 ranker penalty가 쓰는 집계 신호. relevance_score 평균으로 강화.
    title_relevance = (
        sum(a["relevance_score"] for a in scored_articles) / len(scored_articles)
        if scored_articles else 0.0
    )

    # keyword-level quality gate 집계(고관련 기사 수 / 고관련 기사만의 primary cluster 크기).
    # filter_articles_for_display()의 min_count 하한 보충 *이전*(원본 scored_articles) 기준
    # 으로 계산해야 한다 — 하한 보충으로 채워진 결과를 품질 판단 근거로 쓰면 안 되므로
    # (예: "선풍기"처럼 5건 전부 incidental인 키워드가 보충 로직 덕에 정상처럼 보이는 문제).
    high_relevance_articles = [
        a for a in scored_articles if a.get("relevance_score", 0.0) >= HIGH_RELEVANCE_THRESHOLD
    ]
    high_relevance_count = len(high_relevance_articles)
    quality_clusters = cluster_articles(high_relevance_articles)
    quality_primary = select_primary_cluster(quality_clusters)
    quality_cluster_size = len(quality_primary)

    # fresh relevance gate 집계: 고관련 기사 중 published_at 파싱이 되고 FRESH_RELEVANCE_HOURS
    # 이내인 것만 "신선한 고관련"으로 좁힌다. published_at이 없거나 파싱 실패한 기사는
    # 최근성을 증명할 수 없으므로 보수적으로 fresh 판정에서 제외한다.
    fresh_high_relevance_articles = [
        a for a in high_relevance_articles
        if _age_hours(a.get("published_at")) is not None
        and _age_hours(a.get("published_at")) <= FRESH_RELEVANCE_HOURS
    ]
    fresh_high_relevance_count = len(fresh_high_relevance_articles)
    fresh_quality_clusters = cluster_articles(fresh_high_relevance_articles)
    fresh_quality_primary = select_primary_cluster(fresh_quality_clusters)
    fresh_quality_cluster_size = len(fresh_quality_primary)
    high_relevance_ages = [
        _age_hours(a.get("published_at")) for a in high_relevance_articles
        if _age_hours(a.get("published_at")) is not None
    ]
    latest_relevant_age_hours = min(high_relevance_ages) if high_relevance_ages else None

    # PR/광고성 집계(문제 B). 분모 = 이슈 정의 기사 전체 pool(_is_issue_defining_article).
    # primary cluster로 좁히면 relevance score 합 기준 primary 선택의 약점 때문에 "PR 소수지만
    # relevance 높은 클러스터"가 primary가 되어 정상 keyword를 오제외할 수 있어(Codex review-only
    # 11·12차) 전체 pool을 분모로 둔다 — 정상 이슈 오제외 최소화 우선. per-article 플래그도
    # 남겨(is_promotional_pr/is_public_interest) 디버깅/후속 판단에 쓴다.
    for a in scored_articles:
        a["is_promotional_pr"] = is_promotional_pr(a)
        a["is_public_interest"] = is_public_interest(a)
    issue_defining = [a for a in scored_articles if _is_issue_defining_article(a)]
    issue_article_count = len(issue_defining)
    pr_article_count = sum(1 for a in issue_defining if a["is_promotional_pr"])
    public_interest_count = sum(1 for a in issue_defining if a["is_public_interest"])
    commercial_pr_ratio = (pr_article_count / issue_article_count) if issue_article_count else 0.0

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
        "high_relevance_count": high_relevance_count,
        "quality_cluster_size": quality_cluster_size,
        "fresh_high_relevance_count": fresh_high_relevance_count,
        "fresh_quality_cluster_size": fresh_quality_cluster_size,
        "latest_relevant_age_hours": latest_relevant_age_hours,
        "pr_article_count": pr_article_count,
        "public_interest_count": public_interest_count,
        "issue_article_count": issue_article_count,
        "commercial_pr_ratio": round(commercial_pr_ratio, 4),
    }


def build_news_signals(
    candidates: List[dict],
    fetch_news: Callable[[str], List[dict]],
) -> Dict[str, dict]:
    """후보별 News 신호맵 + normalized articles 보관.

    phrase 후보(sources에 "naver_news_phrase")는 strict relevance(require_all_tokens)로 산출한다 —
    다어절 phrase가 일부 토큰만 겹치는 기사로 quality gate를 통과하는 것을 방지
    (Codex 계획 리뷰 P1). seed/aux 후보는 기존 판정 그대로.
    """
    out = {}
    for c in candidates:
        kw = c["keyword"]
        strict = bool((c.get("sources") or {}).get("naver_news_phrase"))
        sig = compute_news_signal(kw, fetch_news(kw), require_all_tokens=strict)
        if sig:
            out[kw] = sig
    return out


def _is_phrase_source_article(article: Dict) -> bool:
    """phrase 후보 발굴 원천으로 쓸 수 있는 기사인지(Codex 계획 리뷰 P2 반영).

    - incidental/side-mention/keyword_not_found 기사 배제(부수 언급에서 phrase가
      만들어지는 것을 차단 — ranker._is_same_issue_evidence_article와 동일 기준).
    - published_at 파싱 가능 + FRESH_RELEVANCE_HOURS 이내 기사만(오래된 기사 기반
      phrase 배제 — 최근성을 증명 못 하면 보수적으로 제외).
    """
    reason = article.get("relevance_reason")
    if reason in ("incidental_giveaway_mention", "keyword_not_found", "object_side_mention"):
        return False
    if reason is None and article.get("is_incidental"):
        return False
    age = _age_hours(article.get("published_at"))
    if age is None or age > FRESH_RELEVANCE_HOURS:
        return False
    return True


def derive_phrase_candidates(
    news_signals: Dict[str, dict],
    existing_keywords: List[str],
    phrase_max: int = PHRASE_MAX,
) -> List[str]:
    """backfill pass 전용: 이미 수집된 뉴스 기사 title에서 사건형 phrase 후보 추출.

    signal.bz류 "뉴스 title 기반 이슈 phrase"를 경량으로 근사한다(형태소 분석 없음,
    추가 API 호출 없음 — pass1에서 fetch한 news_signals의 기사만 사용).

    추출/채택 조건:
    - 원천 기사: _is_phrase_source_article 통과분만. URL 기준 dedupe(같은 기사가 여러
      keyword 신호에 중복 등장해 DF를 부풀리는 것 방지). 경품/판촉 마커가 title에
      있으면 그 title 전체를 원천에서 제외.
    - title 토큰의 연속 n-gram(PHRASE_NGRAM_MIN~MAX어절). 숫자 단독 토큰 포함 배제.
    - 서로 다른 기사 PHRASE_MIN_DF건 이상에서 등장(단일 기사 파편 배제).
    - generic-only 조합 배제(ranker._is_generic_only_display 재사용 — "수사"/"신임
      발표" 같은 일반 서술어만의 phrase 금지).
    - existing_keywords(pass1 랭킹 생존 keyword)와 유사(_is_similar_keyword)하면 배제 —
      생존 이슈의 변형 표기는 어차피 same-issue merge로 흡수되므로 재발굴 불필요.
      gate에서 탈락한 seed의 phrase 확장형은 배제하지 않는다(정밀한 phrase 검색으로
      통과할 새 기회 — backfill의 핵심 발굴 경로).
    - DF 내림차순, 동률이면 더 긴(구체적) phrase 우선. 이미 채택된 phrase와 유사하면
      skip(겹치는 n-gram 파편 정리). 상한 phrase_max.
    """
    from news.ranker import _is_generic_only_display, _is_similar_keyword

    source_articles: Dict[str, Dict] = {}
    for sig in (news_signals or {}).values():
        for a in sig.get("articles") or []:
            if not _is_phrase_source_article(a):
                continue
            key = a.get("url") or a.get("title")
            if key and key not in source_articles:
                source_articles[key] = a

    df: Dict[str, set] = {}
    for key, a in source_articles.items():
        title = a.get("title") or ""
        if any(m in title for m in _INCIDENTAL_MARKERS_STRONG):
            continue
        toks = _tokens(title)
        for n in range(PHRASE_NGRAM_MIN, PHRASE_NGRAM_MAX + 1):
            for i in range(len(toks) - n + 1):
                gram = toks[i:i + n]
                if any(t.isdigit() for t in gram):
                    continue
                phrase = " ".join(gram)
                df.setdefault(phrase, set()).add(key)

    ranked = sorted(
        ((p, len(keys)) for p, keys in df.items() if len(keys) >= PHRASE_MIN_DF),
        key=lambda x: (-x[1], -len(x[0].split()), x[0]),
    )
    out: List[str] = []
    for phrase, _count in ranked:
        if _is_generic_only_display(phrase):
            continue
        if any(_is_similar_keyword(phrase, kw) for kw in existing_keywords or []):
            continue
        if any(_is_similar_keyword(phrase, p) for p in out):
            continue
        out.append(phrase)
        if len(out) >= phrase_max:
            break
    return out
