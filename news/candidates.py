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

from news.normalizer import normalize_article, PRESS_UNKNOWN
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
PHRASE_MAX = 20               # phrase 후보 상한(2026-07 상향 10→20: pass2 원천 확장 대응.
                              # Codex 계획 리뷰 권고로 보수적 상향 — 원천이 pass1 signals ∪
                              # pass2 pre-signals로 넓어져 유효 phrase 후보가 더 많이 뽑히므로
                              # 상한을 20으로 올려 그 확장분을 담는다. 신규 phrase 키워드는
                              # 기존과 동일하게 cached_search_news 경유 fetch라 캐시 미스 시에만
                              # 검색 호출이 늘 수 있다(기존 phrase 메커니즘과 동일 리스크 유형).
PHRASE_NGRAM_MIN = 2
PHRASE_NGRAM_MAX = 4
PHRASE_MIN_DF = 2             # phrase가 서로 다른 기사 몇 건에 등장해야 후보로 인정하는가
PHRASE_RESERVE_BACKFILL = 10  # pass2 최종 candidates2에서 순수 phrase 후보 truncation 보호
                              # 예약분(Codex diff 리뷰 P1). seed가 상한을 가득 채워도 최소
                              # 이만큼의 phrase는 보존해 4안 원천 확장 효과를 유지한다.

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


# === sense-mixing 방어(2026-07) — non-primary cluster 기사의 "다른 의미" 판별 ===
# "위홀 뜻" 사례: keyword가 이효리/워홀커플 기사와 앤디워홀 기사를 모두 substring
# 매칭으로 흡수. cluster_articles()가 두 그룹으로 나누지만, non-primary cluster
# 기사도 anchor 토큰 overlap 조건만 통과하면 표시 articles에 그대로 남는다
# (Codex review-only 계획 리뷰: primary cluster 선택 로직 자체(select_primary_cluster)는
# 이번 범위에서 변경하지 않고, non-primary가 이미 명확히 다른 의미로 판별될 때만
# 추가로 배제하는 완화책으로 좁힌다).
_OFF_PRIMARY_SENSE_MIN_DF = 2  # 문서빈도(반복 등장) 최소 기준. 미만이면 singleton fallback.


def _cluster_common_tokens(articles: List[Dict], min_df: int = _OFF_PRIMARY_SENSE_MIN_DF) -> set:
    """기사 그룹에서 문서빈도(DF) >= min_df인 토큰 집합. 유효 기사가 min_df 미만이면
    (반복 관측 불가) 전체 토큰을 후보로 반환한다(ranker._group_df_tokens와 동일 원리를
    candidates.py 내부에 소규모 복제 — ranker.py가 candidates.py를 import하는 순환
    구조라 역참조 불가)."""
    if not articles:
        return set()
    if len(articles) < min_df:
        toks = set()
        for a in articles:
            toks |= set(_tokens(f"{a.get('title', '')} {a.get('snippet', '')}"))
        return toks
    df: Dict[str, int] = {}
    for a in articles:
        text = f"{a.get('title', '')} {a.get('snippet', '')}"
        for tok in set(_tokens(text)):
            df[tok] = df.get(tok, 0) + 1
    return {t for t, c in df.items() if c >= min_df}


def mark_off_primary_sense(keyword: str, scored_articles: List[Dict], primary: List[Dict]) -> None:
    """non-primary cluster 기사 중 keyword와 "다른 의미"로 판별되는 기사에
    is_off_primary_sense=True를 부여한다(scored_articles를 in-place 수정).

    판별 기준: non-primary cluster의 공통 토큰(_cluster_common_tokens)이
    (a) keyword 토큰과 1개 이상 겹치거나, (b) primary cluster(대표 이슈)
    기사들의 title/snippet 토큰 전체와 2개 이상 겹치면 "같은 의미"로 보아
    False. 둘 다 아니면 "다른 의미"로 True. "위홀 뜻" keyword 토큰({위홀})과
    앤디워홀 클러스터 공통 토큰({앤디, 워홀, 미술관, 대구, 전시})은 keyword와도
    0개, primary(이효리/워홀 커플) 기사 토큰과도 겹치는 게 없거나 1개 이하라
    "다른 의미"로 판정된다.

    primary와도 비교하는 이유(Codex review-only diff 리뷰 P2 1·2·3차): keyword
    토큰 하나만으로 판정하면, 같은 이슈인데 표현이 달라 클러스터가 쪼개진
    경우("다어절 검색어의 일부만 한 기사에 등장, 나머지는 동의어")까지 keyword
    literal과 안 겹친다는 이유로 과잉 배제될 위험이 있다(1차 지적). primary
    앵커를 DF>=2 공통 토큰으로 좁히면 대표 기사 title과의 same-issue 증거를
    놓칠 수 있어(2차 지적) primary 기사 전체 title/snippet 토큰 합집합으로
    확장했으나, 1토큰만 겹쳐도 통과시키면 primary snippet의 흔한 사건어
    하나만으로 과다 허용된다(3차 지적). 그래서 keyword 매칭(1개 이상, 원래
    keyword 자체가 짧을 수 있어 임계를 낮게 유지)과 primary 매칭(2개 이상,
    _display_anchor_allowed의 shared_with_rep>=2와 동일 강도)을 분리한다.
    primary cluster 기사는 항상 False(대상 아님).
    """
    kw_toks = set(_tokens(keyword or ""))
    primary_ids = {id(a) for a in primary}

    non_primary = [a for a in scored_articles if id(a) not in primary_ids]
    if not non_primary:
        return

    primary_all_tokens: set = set()
    for a in primary:
        primary_all_tokens |= set(_tokens(f"{a.get('title', '')} {a.get('snippet', '')}"))

    non_primary_clusters = cluster_articles(non_primary)
    for cluster in non_primary_clusters:
        common = _cluster_common_tokens(cluster)
        same_sense = bool(common & kw_toks) or len(common & primary_all_tokens) >= 2
        is_off_sense = bool(common) and not same_sense
        for a in cluster:
            a["is_off_primary_sense"] = is_off_sense
    for a in scored_articles:
        a.setdefault("is_off_primary_sense", False)


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

# ============================================================================
# 키워드 유형 분류(A) + subject/entity-role 판정(B) — 넓은 단일 엔티티 오염 방어
# ----------------------------------------------------------------------------
# 배경(2026-07): "신천지"가 교단 사건과 정치 수사("반명·신천지와의 대결")에 동시 등장,
# "한화"가 야구단/그룹/오션 등 서로 다른 사건에 각각 등장하는데도 제목에 키워드 문자열만
# 있으면 relevance 0.9로 quality gate·primary cluster를 통과해 오염/무근거 이슈가 발생.
# 정책(사용자 확정): 관련성 최우선. entity 키워드에 한해 subject/entity-role로 오염 기사를
# 정제한 뒤(compute_news_signal 내부, return 전) 모든 news_meta 값을 재계산한다.
# event(산불/폭우/지진 등 사건·현상 단독어)는 강화 gate 미적용(정상 기사 오탈락 방지).
# 판별 불가는 unknown → 과잉 제외 금지(기존 경로 보존).
#
# 경량 규칙 기반 tri-state이며 100% 정확하지 않다. 강한 신호만 hard 판정하고 나머지는
# unknown으로 보수 처리한다(단독 정규식 hard gate 금지 — 사용자 지시).
# ============================================================================

# event(사건·현상) 화이트리스트 — 단일 토큰이라도 keyword 자체가 사건을 뜻하므로
# entity 전용 cohesion 강화 대상에서 제외한다(정상 기사 보호). 하드코딩된 "결과"가 아니라
# entity 오분류로부터 event 키워드를 보호하기 위한 최소 보호 집합이다.
_EVENT_KEYWORDS = {
    # 자연재해·사고
    "산불", "폭우", "지진", "태풍", "홍수", "침수", "정전", "폭발", "화재", "붕괴",
    "파업", "지진해일", "쓰나미", "가뭄", "한파", "폭염", "미세먼지", "장마", "낙뢰",
    "총격", "테러", "지진동", "여진", "산사태", "누출", "감전", "폭설", "산불진화",
    # 정치·사회 현상어(단일 토큰이지만 특정 엔티티가 아니라 사건·현상 — entity cohesion
    # 강화 대상에서 제외해 정상 이슈 오탈락 방지, Codex 최종리뷰 P2). 여러 주체가 등장하는
    # 것이 정상인 이슈들이라 "단일 엔티티 사건 응집"을 요구하면 안 된다.
    "대선", "총선", "탄핵", "개각", "인사청문회", "국정감사", "전당대회", "경선",
    "집회", "시위", "파병", "휴전", "종전", "정상회담", "선거",
    # 경제 현상어
    "환율", "금리", "물가", "인플레이션", "증시", "주가", "유가", "코스피", "코스닥",
    "부동산", "집값", "전세", "대출", "감세", "증세",
    # 보건·기타
    "코로나", "독감", "폭동", "내전", "쿠데타",
}

# subject 신호: keyword 뒤에 자주 붙는 주격/속격 조사(보조 신호 — 단독 hard gate 아님).
_SUBJECT_JOSA = ("이", "가", "은", "는", "의", "을", "를", "에게", "께서")

# non_subject 신호: keyword가 "비교/대결/구도" 대상으로 쓰이는 수사적 문맥 마커.
# "반명·신천지와의 대결"처럼 keyword가 사건 주체가 아니라 비유/정치 수사로 언급되는 경우.
# keyword 바로 뒤(조사 포함) 근접 위치에 이 마커가 오는지로 판정한다(위치 무관 any-match는
# "신천지 재판 대결" 같은 정상 기사를 오탐할 수 있어 근접 조건을 둔다).
_RHETORIC_SUFFIX_MARKERS = (
    "와의 대결", "과의 대결", "와의 전쟁", "과의 전쟁", "와의 대회전", "과의 대회전",
    "와의 대립", "과의 대립", "와의 구도", "과의 구도", "와의 갈등", "과의 갈등",
    "와의 전면전", "과의 전면전",
)
# 나열/비유 접속 문맥(keyword가 다른 개체와 병렬 나열되는 정치 수사).
_RHETORIC_LIST_MARKERS = ("위장", "반명", "친명", "적통", "계파", "계승")

# === comparison target 방어(A, 2026-07-21) — 비교/맞불 대상의 주체 오승격 방지 ===
# 배경: "삼성전자, 첫 신용카드 출시…애플카드에 맞불" 묶음에서 비교 대상 '애플 카드'가
# 기사에 자주 등장해 canonical(대표)로 승격됐다(Apple이 카드를 낸 이슈로 오인). keyword가
# 비교 마커의 "대상 위치(앞)"에 오고, 같은 title에 keyword가 아닌 "별도의 실제 주체"가
# 확인될 때만 non_subject(NONSUBJECT_COMPARISON)로 강등한다. 강한 조건이므로 event/entity/
# unknown(다토큰 '애플 카드' 포함) 어디에나 적용해도 정상 주체를 깎지 않는다.
#
# 원칙(사용자 확정):
# - 강한 대상 마커(대상 위치가 명확)만 comparison 근거로 쓴다: keyword 뒤 근접에 아래.
# - "대결/대응/대체/라이벌/경쟁"은 단독 강등 근거로 쓰지 않는다(양측 공동 주체 가능) —
#   이 목록에 넣지 않는다.
# - 별도 주체가 확인되지 않으면 강등하지 않고 기존 판정(unknown 등)으로 흐른다(fail-open).
_COMPARISON_TARGET_MARKERS = (
    "맞불", "맞서", "맞선", "겨냥", "대항", "도전장",
)
# comparison-dominant(전체 강등) 최소 표본/비율(사용자 P1 보완, 2026-07-21): 기사 1건의
# comparison 언급만으로 keyword 전체를 비주체로 확정하면 단일 기사 오탐/서술 습관에 의해
# 정상 후보가 통째로 탈락할 위험이 크다. title에 keyword가 등장하는 기사 최소 2건 이상,
# 그중 과반 이상이 comparison이어야만 "이 keyword는 이 묶음에서 일관되게 비교 대상"으로
# 인정한다(애플카드 사례: title 노출 기사 다수 중 대부분이 "맞불/맞선다" 표현 — 통과.
# 기사 1건만 comparison 표현을 쓰고 나머지는 무관하거나 subject인 경우는 강등 안 함).
COMPARISON_DOMINANT_MIN_ARTICLES = 2
COMPARISON_DOMINANT_MIN_RATIO = 0.5


def _dedup_by_url_identity(articles: List[dict]) -> List[dict]:
    """comparison-dominant 증거 모수용 URL identity dedup(Codex P2, 2026-07-22).

    news.dedup.dedup_articles와 동일한 winner 계약(동일 url은 처음 1건만, 입력순서 유지)을
    쓰되, 한 가지만 다르다: dedup_articles는 url 없는 기사를 제거하지만, comparison 모수에선
    url 없는 기사도 각각 독립 evidence로 세야 하므로(fallback = 객체 identity) 제거하지 않고
    그대로 남긴다. 서로 다른 url은 절대 합치지 않고, 제목이 같아도 url이 다르면 2건으로 센다.

    이 dedup은 오직 comparison-dominant 최소표본/비율 계산에만 쓰고, 반환 기사 목록이나
    다른 ranking 계약은 바꾸지 않는다.
    """
    seen_urls = set()
    result: List[dict] = []
    for a in articles:
        url = a.get("url")
        if url:
            if url in seen_urls:
                continue
            seen_urls.add(url)
        # url 없는 기사는 dedup하지 않고 각각 독립으로 보존(뭉치지 않음).
        result.append(a)
    return result
# 위 마커는 "keyword ~ 마커" 근접 구간에서만 본다(_comparison_target_role). "대응"은
# "정부 대응/사고 대응"처럼 사건 서술로도 흔해 단독 마커에서 제외한다. "대결/대체/라이벌/
# 경쟁"도 공동 주체 가능성이 있어 제외(사용자 확정). keyword와 마커 사이에는 keyword의
# 잔여 토큰(예: '애플 카드'의 '카드')과 조사 정도만 허용한다.


def _has_separate_subject(keyword: str, title: str) -> bool:
    """title에 keyword가 아닌 "별도의 실제 주체"가 있는지(comparison 강등 전제).

    보수적 판정(강한 신호만): title 선두에 keyword와 겹치지 않는 주체가
    (a) 콤마로 끊기거나("삼성전자, ...") (b) 주격/속격 josa가 바로 붙는("네이버가 ...")
    형태로 등장하면 True. 그 선두 주체는 keyword보다 앞(왼쪽)에 있어야 한다(비교 대상은
    보통 뒤에 온다). keyword 토큰과 문자적으로 겹치는 선두("삼성 갤럭시 카드")는 자기
    자신이므로 별도 주체가 아니다. 애매하면 False(fail-open — 강등 안 함).
    """
    if not title:
        return False
    kw_toks = {t.lower() for t in _tokens(keyword or "")}
    lead_full = _title_first_clause(title)   # 첫 구분자(콤마 등) 이전
    lead_toks = _tokens(lead_full)
    if not lead_toks:
        return False
    first = lead_toks[0]
    # 선두 토큰이 keyword 자신(또는 파생)이면 별도 주체 아님.
    if first.lower() in kw_toks or _keyword_derived_token(first, kw_toks):
        return False
    # 선두 주체는 keyword보다 앞에 있어야 한다(비교 대상은 통상 문장 뒤).
    kw0 = next(iter(_tokens(keyword or "")), "")
    kw_pos = title.find(kw0) if kw0 else -1
    subj_pos = title.find(first)
    if kw_pos >= 0 and subj_pos >= 0 and subj_pos > kw_pos:
        return False
    # (a) 콤마 절로 끊긴 선두 주체: title이 first_clause보다 길다 = 콤마 등으로 잘렸다.
    if len(lead_full) < len(title.strip()) and title.strip().startswith(first):
        return True
    # (b) 선두 주체 뒤 주격/속격 josa 근접.
    after_first = title[subj_pos + len(first):subj_pos + len(first) + 2] if subj_pos >= 0 else ""
    if any(after_first.startswith(j) for j in ("가", "는", "은", "이", "의", "도")):
        return True
    return False


def _keyword_derived_token(tok: str, kw_toks: set) -> bool:
    low = tok.lower()
    return any(k in low or low in k for k in kw_toks if len(k) >= 2)


def _keyword_is_title_subject(keyword: str, title: str) -> bool:
    """keyword가 title의 선두 주체인지(comparison-dominant 판정 보조).

    keyword 첫 토큰이 title 선두 절(첫 구분자 이전)에 있고, 그 뒤에 주격/속격 josa 또는
    콤마가 붙거나 선두 위치(<=4자)면 True. 비교 대상이 아니라 실제 주체 신호. 애매하면
    False(comparison-dominant 오강등 방지 쪽으로 보수).
    """
    kw0 = next(iter(_tokens(keyword or "")), "")
    if not kw0:
        return False
    lead = _title_first_clause(title)
    pos = lead.find(kw0)
    if pos < 0:
        return False
    after = lead[pos + len(kw0):pos + len(kw0) + 2]
    if pos <= 4:  # 선두 근접
        return True
    return any(after.startswith(j) for j in ("가", "는", "은", "이", "의", ",")) or "," in lead[pos:]


def _comparison_target_role(keyword: str, title: str) -> Optional[str]:
    """keyword가 비교 마커의 대상 위치에 오고 별도 주체가 확인되면 'NONSUBJECT_COMPARISON'.

    판정(모두 충족해야 강등):
    1. keyword가 title에 등장하고, keyword 끝 뒤 근접(<=6자)에 비교 마커가 온다. 근접
       구간에는 keyword 잔여 토큰(예: '애플 카드'의 '카드')과 조사 정도만 낀다.
    2. 같은 title에 _has_separate_subject(별도 실제 주체).
    미충족이면 None(강등 안 함 — 상위 로직이 기존 판정 유지).
    """
    kw_toks = _tokens(keyword or "")
    if not kw_toks:
        return None
    kw0 = kw_toks[0]
    pos = title.find(kw0)
    if pos < 0:
        return None
    # keyword의 마지막 토큰이 title에서 끝나는 지점을 anchor로 삼는다(다토큰 keyword 대응:
    # '애플' 뒤 '카드'까지 소비한 위치부터 마커를 찾는다).
    scan = pos + len(kw0)
    for t in kw_toks[1:]:
        nxt = title.find(t, scan)
        if 0 <= nxt <= scan + 4:  # 잔여 토큰이 근접에 이어질 때만 anchor 전진
            scan = nxt + len(t)
    tail_head = title[scan:scan + 6]  # keyword(+잔여토큰) 직후 근접 구간
    if not any(m in tail_head for m in _COMPARISON_TARGET_MARKERS):
        return None
    if not _has_separate_subject(keyword, title):
        return None
    return "NONSUBJECT_COMPARISON"


def classify_keyword_kind(keyword: str, news_meta: Optional[Dict] = None) -> str:
    """키워드 유형 tri-state: 'event' | 'entity' | 'unknown'.

    - event: _EVENT_KEYWORDS(사건·현상 단독어). cohesion 강화 미적용.
    - entity: 단일 non-generic 토큰이면서 event 아님(인물/기업/조직/구단/종교/작품/브랜드
      후보). cohesion 강화 적용 대상.
    - unknown: 다토큰이거나 generic 토큰이 섞여 단일 엔티티로 확정 불가 → 보수적 경로.

    news_meta는 현재 미사용(향후 사건 분산 신호 확장 여지). 시그니처만 열어둔다.
    """
    kw = (keyword or "").strip()
    if not kw:
        return "unknown"
    toks = _tokens(kw)
    if kw in _EVENT_KEYWORDS or (len(toks) == 1 and toks[0] in _EVENT_KEYWORDS):
        return "event"
    # 단일 토큰 + non-generic 이면 entity 후보. 다토큰/ generic 은 unknown(보수).
    if len(toks) == 1 and toks[0] not in _GENERIC_SINGLE_TOKENS:
        return "entity"
    return "unknown"


def _keyword_pos_in_title(keyword: str, title: str) -> int:
    """title에서 keyword(첫 토큰 기준) 위치 index. 없으면 -1."""
    toks = _tokens(keyword or "")
    if not toks:
        return -1
    return (title or "").find(toks[0])


def classify_entity_role(keyword: str, article: Dict) -> tuple:
    """기사에서 keyword의 역할 tri-state: ('subject'|'non_subject'|'unknown', reason_code).

    단독 정규식 hard gate 금지(사용자 지시). 여러 신호를 조합하되 강한 패턴만 확정 판정하고
    애매하면 unknown으로 둔다.

    strong non_subject(제외 대상):
    - NONSUBJECT_RHETORIC: keyword 바로 뒤에 "~와의 대결/전쟁/구도" 등 수사 마커 근접.
    - NONSUBJECT_LIST_RHETORIC: keyword가 정치 나열 문맥(위장/반명/친명/적통…)과 근접 병렬.
    - NONSUBJECT_SNIPPET_ONLY: title에 keyword 없고 snippet에만 등장(이미 relevance 낮음).

    strong subject(보존 대상):
    - SUBJECT_JOSA_PREDICATE: keyword 뒤 주격/속격 조사(+ title 주제) → 행위 주체.
    - SUBJECT_LEADING: keyword가 title 앞부분(첫 절)에서 사건 명사와 결합, incidental 아님.

    그 외 unknown(UNKNOWN_AMBIGUOUS): 한국어 주어생략/도치 등으로 확정 불가.
    """
    title = article.get("title") or ""
    snippet = article.get("clean_description") or article.get("snippet") or ""
    kw_toks = _tokens(keyword)
    if not kw_toks:
        return "unknown", "UNKNOWN_NO_KEYWORD"
    kw0 = kw_toks[0]

    title_low = title.lower()
    in_title = kw0.lower() in title_low or _has_keyword_token(keyword, title)

    # snippet-only: 제목에 keyword 없음 → non_subject(정치 계파 기사 snippet 언급 등).
    if not in_title:
        if _has_keyword_token(keyword, snippet) or kw0.lower() in (snippet or "").lower():
            return "non_subject", "NONSUBJECT_SNIPPET_ONLY"
        return "unknown", "UNKNOWN_AMBIGUOUS"

    pos = title.find(kw0)
    tail = title[pos + len(kw0):] if pos >= 0 else ""

    # strong non_subject: keyword 바로 뒤 수사 마커(조사 붙은 형태 포함, 근접 판정).
    tail_head = tail[:12]  # keyword 직후 근접 구간
    for m in _RHETORIC_SUFFIX_MARKERS:
        if m in tail_head or m.lstrip("와과") in tail_head:
            return "non_subject", "NONSUBJECT_RHETORIC"
    # 정치 나열 수사: keyword 근처(앞뒤 근접)에 나열 마커.
    near = title[max(0, pos - 12):pos + len(kw0) + 12]
    if any(m in near for m in _RHETORIC_LIST_MARKERS):
        return "non_subject", "NONSUBJECT_LIST_RHETORIC"

    # comparison 대상(A): keyword가 "~에 맞불/맞서/겨냥/대항" 대상 위치 + 별도 주체 확인.
    # "대결/대응/대체/라이벌/경쟁" 단독은 마커에 없어 여기 안 걸린다(공동 주체 보호).
    comp = _comparison_target_role(keyword, title)
    if comp:
        return "non_subject", comp

    # incidental/side-mention 기사는 subject 아님(대표 자격 없음) — 기존 신호 재사용.
    if article.get("is_incidental"):
        return "unknown", "UNKNOWN_INCIDENTAL"

    # strong subject: keyword 뒤 주격/속격 조사.
    tail_stripped = tail.lstrip()
    if any(tail.startswith(j) or tail_stripped.startswith(j) for j in _SUBJECT_JOSA):
        return "subject", "SUBJECT_JOSA_PREDICATE"
    # 콤마/공백 뒤 술어 절이 이어지는 선두 주체("장동건, ~했다" / "장동건 별세").
    if pos <= 6 and article.get("relevance_reason") == "keyword_main_topic":
        return "subject", "SUBJECT_LEADING"

    return "unknown", "UNKNOWN_AMBIGUOUS"


# === crime-attribution safety gate(G, 2026-07-21) — 명예·법적 위험 방어 =============
# 배경(운영 재현): "박나래 공갈미수 구속"이 실시간 이슈에 노출됐다. 실제 기사는 "박나래를
# 협박한 전 매니저가 공갈미수로 구속"이라, 범죄·처분의 주체는 박나래가 아니라 전 매니저다.
# 이름과 범죄어를 직결한 키워드는 유명인을 범죄 주체로 오인시키는 명예·법적 위험이 있다.
#
# 기존 entity-role 정제는 (1) 다토큰 키워드(kind=unknown)엔 미적용이고 (2) "키워드 엔티티가
# 기사 주제/주어인가"만 봐서 제목 앞머리 인물명은 통과했다. "범죄·처분의 실제 대상이
# 누구인가"를 보는 게이트가 없었다.
#
# 설계 원칙(fail-closed): 범죄·처분어를 포함한 키워드는 **기본 위험**으로 두고, 고관련
# 기사들이 "이름 엔티티가 실제 범죄 주체"임을 적극 입증할 때만 안전(노출)한다. 입증 못
# 하면 drop. 경량 규칙(근접·조사·관계명사)만 쓰고 형태소 분석·인물명 하드코딩·금칙어
# 사전은 쓰지 않는다. 100% 정확이 아니라, 위험 쪽으로 보수적(과소차단보다 과잉차단 선호)
# 이다 — 정상 본인 사건은 verified_self 경로로 보존한다.
# ============================================================================

# 처분·수사어(법적 처리 신호). 이 토큰 없이 범죄어만 있으면 사건성이 약해 트리거하지 않는다.
_DISPOSITION_TOKENS = (
    "구속", "송치", "기소", "체포", "구인", "입건", "구속영장", "압수수색", "피의자", "실형",
)
# 범죄어(혐의 종류).
_CRIME_TOKENS = (
    "공갈", "협박", "사기", "횡령", "배임", "폭행", "성폭행", "성추행", "마약", "뇌물",
    "스토킹", "살인", "절도", "강도", "밀수", "도박", "음주운전", "불법촬영", "감금", "유괴",
)
# 이름 엔티티에 종속된 "제3자 관계명사". 이름 직후 근접에 오면 그 관계인이 별개 주체.
# 주의: 이 명사가 이름과 근접하지 않고 단독 주체로 등장하면(예: "남편 살인 구속") crime
# gate 는 그 키워드에서 유명인 이름 anchor 가 없어 애초에 트리거되지 않는다(정상 이슈 보존).
_RELATION_PERSON_MARKERS = (
    "전 매니저", "전매니저", "前 매니저", "前매니저", "매니저", "소속사", "대표", "직원",
    "지인", "친구", "동생", "형", "누나", "오빠", "언니", "가족", "부모", "아들", "딸",
    "남편", "아내", "부인", "전 남친", "전 여친", "前 남친", "前 여친", "전 남자친구",
    "전 여자친구", "내연남", "내연녀", "측근", "동업자", "투자자", "운전기사", "경호원",
    "팬", "유튜버", "연인", "교제 상대", "일당",
)
# 익명 주체(이름과 별개로 등장하는 실제 범죄 주체). "이유명 협박한 40대 남성 구속" → 남성.
_CRIME_SUBJECT_GENERIC = (
    "남성", "여성", "남자", "여자", "40대", "30대", "20대", "50대", "60대", "10대",
    "일당", "일가", "A씨", "B씨", "C씨", "무직", "회사원",
)
# victim-context 마커: 기사 제목에서 이름이 피해자·상대방임을 시사(기사 role 판정용,
# subject 판정보다 우선). "협박한"(관형형)은 "A를 협박한 B" 구조라 victim 신호지만,
# bare "협박"은 범죄어 자체라 아래 keyword-level 억제에는 쓰지 않는다(P1-A).
_VICTIM_CONTEXT_MARKERS = (
    "협박한", "협박당한", "상대로", "피해", "고소한", "고소당한", "고발한", "고발당한",
    "법적공방", "법적 공방", "스토킹당한", "노린", "노려", "노렸다",
)
# keyword 문자열 자체에 이미 관계·피해 맥락이 담겨 "안전명"임을 뜻하는 마커(트리거
# 억제 전용). bare 범죄어("협박"/"고소")는 제외한다 — "박나래 협박 구속"처럼 범죄어+
# 처분어 조합은 오히려 위험 키워드라 반드시 트리거돼야 한다(Codex P1-A). 관형형/명사구
# ("협박한"/"협박당한"/"법적공방"/"상대로")만 안전 맥락으로 인정한다.
_KEYWORD_SAFE_CONTEXT_MARKERS = (
    "협박한", "협박당한", "상대로", "법적공방", "법적 공방", "스토킹당한", "당한",
    "피해자",
)
# crime keyword 선두 토큰이 사람/엔티티 anchor 가 아님을 뜻하는 non-person 명사(범죄
# 도구·행위·직업 카테고리·익명 주체). 이런 토큰이 선두면 "이름+범죄어" 구조가 아니라
# 사건 자체가 키워드이므로 crime-attribution gate 를 트리거하지 않는다(예: "흉기 난동
# 구속", "배우 마약 구속"의 '배우'). 인물명 하드코딩이 아니라, 사람 anchor 배제용
# 일반 명사 집합이다(_EVENT_KEYWORDS 와 같은 역할).
_NON_PERSON_LEAD_TOKENS = (
    "흉기", "난동", "칼", "차량", "차", "총", "총기", "방화", "화재", "사고", "폭행",
    "성폭행", "성추행", "음주운전", "보이스피싱", "전세사기", "마약", "도박",
    "남성", "여성", "남자", "여자", "일당", "부부", "모녀", "부자", "형제",
)
# 직업/신분 접두어. 키워드 선두에 오면 사람 anchor 가 아니라 그 "다음 토큰"이 실제 이름
# anchor 다("배우 김유명 공갈 구속" → 김유명). 접두어 뒤 이름을 건너뛰지 않고 anchor 를
# 한 칸 전진시킨다(Codex P1-B). 이름 없이 접두어 단독이면 anchor 없음 처리된다.
_OCCUPATION_PREFIX_TOKENS = (
    "배우", "가수", "감독", "의원", "시장", "지사", "회장", "사장", "대표", "교수",
    "목사", "판사", "검사", "변호사", "경찰", "군인", "소방관", "학생", "교사", "아이돌",
    "래퍼", "코미디언", "개그맨", "방송인", "유튜버", "전 의원", "前 의원", "가수 겸",
)
# name~crime 사이의 약한 연결어(주체 확정 보류). "A 관련 수사", "A 연루 의혹"처럼
# 이름이 사건에 언급될 뿐 주체임이 확정되지 않는 문맥.
_WEAK_LINKAGE_MARKERS = ("관련", "연루", "연관", "언급", "거론", "의혹")
# 기관·직함·수사주체 등 "이름 사이에 흔히 끼는 비-피의자 명사". subject 확정 시 이름과
# 처분어 사이에 이런 토큰이 있으면 그 자체로는 다른 주체 신호가 아니지만(수사 주체),
# 이 목록 밖의 2~4자 한글 토큰이 끼면 "다른 실명 주체"일 수 있어 본인 확정을 보류한다.
_INSTITUTION_ROLE_TOKENS = (
    "특검", "검찰", "경찰", "법원", "공수처", "검찰청", "경찰청", "지검", "지청", "수사팀",
    "수사본부", "합수단", "국수본", "전 대통령", "대통령", "전 장관", "장관", "총리",
    "위원장", "이사장", "구속영장", "체포영장", "압수수색", "영장", "혐의", "사건", "재판",
)


# 이름·주체와 무관한 흔한 서술/명사 토큰(other-name 오탐 방지). 범죄 기사 제목에 자주
# 등장하지만 인물명이 아닌 일반어. lexicon 밖 2~4자 한글이라도 이 목록이면 이름 후보에서
# 제외한다(보수적 fail-closed 유지하되, "투약/혐의/조사/수사/투자/사건" 같은 일반어가
# 다른 인물명으로 오인돼 본인 실제 사건을 unknown 으로 떨구는 회귀를 막는다).
_COMMON_NONNAME_TOKENS = (
    "투약", "혐의", "조사", "수사", "투자", "사건", "재판", "판결", "선고", "송치", "적발",
    "검거", "적용", "청구", "발부", "신청", "기각", "인정", "부인", "주장", "진술", "출석",
    "소환", "압수", "증거", "피해", "가해", "공범", "일부", "결국", "당시", "이후", "관련",
    "혐의로", "상대로", "대상", "행위", "범행", "수법", "정황", "의심", "포착", "확인",
    # 마약·범죄 기사 일반어(인명 오인 방지, Codex 3R P2). 이름처럼 2~4자 한글이라 lexicon
    # 밖이면 other-name 후보로 잡혀 본인 실제 사건이 과잉 차단된다.
    "필로폰", "대마", "코카인", "케타민", "엑스터시", "마약류", "상습", "판매", "유통",
    "밀반입", "구매", "복용", "흡입", "구입", "성범죄", "촬영물", "동영상", "불법", "혐의점",
    "구속기소", "불구속", "재범", "초범", "가담", "모의", "은닉", "도주", "잠적",
)


def _looks_like_person_name(tok: str) -> bool:
    """tok 이 한국 인물명 후보로 보이는가(NER 없이 근사, 보수적)."""
    if not (2 <= len(tok) <= 4):
        return False
    if not all("가" <= ch <= "힣" for ch in tok):
        return False  # 순수 한글만
    if tok in _COMMON_NONNAME_TOKENS:
        return False
    return True


def _has_other_name_candidate(segment: str, anchor: str) -> bool:
    """segment(이름 anchor 와 처분어 사이 구간)에 anchor 와 다른 "실명 후보" 토큰이 있는가.

    NER 없이 근사한다: 인물명처럼 보이는(_looks_like_person_name) 2~4자 순수 한글 토큰 중
    anchor 가 아니고 알려진 lexicon(기관·직함·관계·익명주체·범죄·처분·약한연결·직업접두·
    일반 서술어)에 속하지 않는 토큰이 있으면 "다른 인물명일 수 있음"으로 본다(보수적 —
    애매하면 본인 확정 보류해 fail-closed). "김건희 특검 윤석열 구속영장 청구"에서
    anchor=김건희, 구간의 '윤석열'이 걸려 subject 확정을 막는다.
    """
    _known = (
        set(_INSTITUTION_ROLE_TOKENS) | set(_RELATION_PERSON_MARKERS)
        | set(_CRIME_SUBJECT_GENERIC) | set(_CRIME_TOKENS) | set(_DISPOSITION_TOKENS)
        | set(_WEAK_LINKAGE_MARKERS) | set(_OCCUPATION_PREFIX_TOKENS)
        | set(_NON_PERSON_LEAD_TOKENS)
    )
    for tok in _tokens(segment or ""):
        if tok == anchor or not _looks_like_person_name(tok):
            continue
        if tok in _known or any(tok in k or k in tok for k in _known):
            continue
        return True
    return False
# 주격/속격 조사(subject 판정용, 이름 직후).
_CRIME_SUBJECT_JOSA = ("이", "가", "은", "는", "의", "을", "를", "에게", "께서", ",")


def _kw_name_anchor(keyword: str) -> str:
    """keyword 에서 이름/엔티티 anchor 토큰을 고른다.

    선두 토큰이 직업/카테고리 접두어(_OCCUPATION_PREFIX_TOKENS: 배우/가수/의원 등)면 그
    다음 토큰을 anchor 로 쓴다("배우 김유명 공갈 구속" → 김유명, Codex P1-B). 다음 토큰도
    없거나 그 자체가 범죄·처분·익명주체 토큰이면 anchor 없음(빈 문자열).
    """
    toks = _tokens(keyword or "")
    if not toks:
        return ""
    idx = 0
    if toks[0] in _OCCUPATION_PREFIX_TOKENS and len(toks) >= 2:
        idx = 1
    anchor = toks[idx]
    # anchor 가 사람 고유명이 아니면(사건·익명주체·도구·범죄·처분·관계명사) anchor 없음.
    # 관계명사 단독 선두("남편 음주운전 입건")는 유명인 이름이 아니라 그 관계인 본인
    # 사건이므로 crime gate 대상이 아니다(정상 이슈 보존).
    if (anchor in _EVENT_KEYWORDS or anchor in _CRIME_SUBJECT_GENERIC
            or anchor in _NON_PERSON_LEAD_TOKENS
            or anchor in _RELATION_PERSON_MARKERS
            or any(anchor == t or anchor in t for t in (_CRIME_TOKENS + _DISPOSITION_TOKENS))):
        return ""
    return anchor


def _kw_lead_token(keyword: str) -> str:
    """호환용 별칭 — 이름 anchor 추출은 _kw_name_anchor 를 쓴다."""
    return _kw_name_anchor(keyword)


def crime_keyword_requires_check(keyword: str) -> bool:
    """crime-attribution 검증이 필요한 키워드 후보인가(트리거 전용, 최종 판정 아님).

    조건: (a) 처분·수사어 토큰 포함 AND (b) 범죄어 OR 처분어가 사건성을 갖추고
    (c) 표기에 관계명사·victim 맥락이 아직 없다(있으면 안전명이라 검증 불필요).

    이 predicate 는 "검증 대상"만 고른다. True 라고 해서 위험(unsafe)이 아니다 — 최종
    판정은 기사 증거 기반 has_unsafe_crime_attribution 이 한다(본인 실제 사건은 통과).
    """
    kw = keyword or ""
    has_disp = any(t in kw for t in _DISPOSITION_TOKENS)
    if not has_disp:
        return False
    # 이름 anchor 가 없으면(선두가 사건·도구·익명주체·범죄어이고 뒤에 이름 없음) 트리거
    # 안 함 — "흉기 난동 구속" 처럼 사건 자체가 키워드인 경우. 직업 접두어 뒤 이름은
    # _kw_name_anchor 가 한 칸 전진해 잡는다("배우 김유명 공갈 구속" → 김유명, P1-B).
    anchor = _kw_name_anchor(kw)
    if not anchor:
        return False
    # 안전명 억제: 관계명사가 이름 anchor "뒤"에 올 때만 안전명으로 본다("박나래 전
    # 매니저 …" → 전 매니저가 제3자). 이름 "앞"의 관계·직업 다의어(대표/유튜버)는 접두어일
    # 뿐이라 억제하지 않는다(Codex P1: "유튜버 김유명 사기 구속"이 우회하던 문제). anchor
    # 위치 이후 부분 문자열에서만 관계명사를 찾는다.
    anchor_pos = kw.find(anchor)
    after_anchor = kw[anchor_pos + len(anchor):] if anchor_pos >= 0 else ""
    if any(m in after_anchor for m in _RELATION_PERSON_MARKERS):
        return False
    # 안전 맥락(관형형 victim 표현: 협박한/상대로/법적공방 등) → 검증 불필요.
    # bare 범죄어("협박"/"고소")는 여기에 포함하지 않는다(P1-A): "박나래 협박 구속"처럼
    # 범죄어+처분어 조합은 오히려 위험 키워드라 반드시 트리거돼야 한다.
    if any(m in kw for m in _KEYWORD_SAFE_CONTEXT_MARKERS):
        return False
    return True


def classify_crime_subject_role(keyword: str, article: Dict) -> str:
    """기사에서 keyword 선두 엔티티(이름)가 실제 범죄·처분 주체인가.

    반환: 'subject' | 'victim_or_bystander' | 'unknown'.

    우선순위(경량 규칙, 단독 정규식 hard gate 금지):
    1. victim-context 마커가 이름 근처 → victim_or_bystander (이름이 피해자/상대방).
    2. 이름 직후 근접(≤6자)에 관계명사 → 그 관계인이 제3자 주체 → victim_or_bystander.
    3. 이름과 별개로 익명 주체(_CRIME_SUBJECT_GENERIC)가 범죄·처분어와 결합 → victim.
    4. 이름 직후 주격/속격 조사 + 범죄·처분어 직결 & 관계·victim 마커 없음 → subject(본인).
    5. 그 외 → unknown (과잉판정 금지).
    """
    title = article.get("title") or ""
    snippet = article.get("clean_description") or article.get("snippet") or ""
    text = f"{title} {snippet}"
    low = text.lower()
    lead = _kw_lead_token(keyword)
    if not lead:
        return "unknown"
    # 선두 토큰이 사람 anchor 가 아니면(도구·직업 카테고리·익명 주체 등) 이름 기준 주체
    # 판정 자체가 성립하지 않는다 → unknown(중립). 트리거 단계에서 이미 걸러지지만,
    # 함수 단독 호출·다른 경로 재사용 대비 방어적으로 둔다.
    if lead in _NON_PERSON_LEAD_TOKENS or lead in _CRIME_SUBJECT_GENERIC:
        return "unknown"

    lead_spans = _find_all(lead, title) or _find_all(lead, text)
    if not lead_spans:
        return "unknown"
    # 이름의 가장 앞 등장을 anchor 로 삼는다.
    anchor_start, anchor_end = lead_spans[0]

    has_crime = any(t in text for t in (_CRIME_TOKENS + _DISPOSITION_TOKENS))
    if not has_crime:
        return "unknown"

    # (1) victim-context 마커 근접(앞뒤 24자) → 이름은 피해자/상대방.
    victim_spans = _marker_positions_in(low, [m.lower() for m in _VICTIM_CONTEXT_MARKERS])
    for vs, ve in victim_spans:
        if _interval_distance(anchor_start, anchor_end, vs, ve) <= 24:
            return "victim_or_bystander"

    # (2) 이름 직후 근접(≤6자)에 관계명사 → 종속 제3자가 주체.
    tail = title[anchor_end:anchor_end + 14] if anchor_end <= len(title) else ""
    tail_stripped = tail.lstrip(" 의,·")
    for rel in _RELATION_PERSON_MARKERS:
        if tail_stripped.startswith(rel) or rel in tail[:8]:
            return "victim_or_bystander"

    # (3) 이름과 별개로 익명 주체 + 범죄·처분어 결합 → 익명 주체가 실제 주체.
    #     이름 뒤 구간에 익명 주체가 등장하고 그 뒤/근처에 처분어가 있으면 victim.
    after = title[anchor_end:]
    if any(g in after for g in _CRIME_SUBJECT_GENERIC) and any(
        d in after for d in _DISPOSITION_TOKENS
    ):
        return "victim_or_bystander"

    # 약한 연결어("관련"/"연루"/"의혹")가 이름 근처에 있으면 주체 확정 보류(unknown).
    # "최유명 관련 수사 계속…구속영장 검토"처럼 이름이 사건에 언급될 뿐 주체가 아님.
    weak_spans = _marker_positions_in(low, _WEAK_LINKAGE_MARKERS)
    for ws, we in weak_spans:
        if _interval_distance(anchor_start, anchor_end, ws, we) <= 10:
            return "unknown"

    # (4) subject(본인) 확정은 보수적으로 — 이름이 범죄·처분어와 **직접 결합**할 때만.
    #     lead_is_early "단독"으로는 확정하지 않는다(Codex P1-C): "김건희 특검, 윤석열
    #     전 대통령 구속영장 청구"처럼 이름이 선두 수식어/기관명이고 실제 처분 대상은
    #     다른 인물인 경우 subject 오확정 → verified_self 오통과를 막는다.
    josa_tail = title[anchor_end:anchor_end + 3]
    has_josa = any(josa_tail.startswith(j) or tail_stripped[:2].startswith(j)
                   for j in _CRIME_SUBJECT_JOSA)
    # 이름과 처분어 사이 절 구분자(",", "…", "·" 등)가 있으면 다른 절의 주체일 수 있어
    # 본인 확정 보류. 이름 직후~처분어 구간에 clause break 가 없어야 한다.
    disp_spans = _marker_positions_in(title, _DISPOSITION_TOKENS)
    nearest_disp = min((ds for ds, de in disp_spans if ds >= anchor_end), default=None)
    name_bound_to_disp = False
    if nearest_disp is not None:
        between = title[anchor_end:nearest_disp]
        # 이름과 처분어 사이가 짧고(≤16자) 절 구분자·관계명사·익명주체·다른 실명 후보가
        # 끼지 않음. 다른 실명 후보(_has_other_name_candidate)가 끼면 그 인물이 실제 처분
        # 대상일 수 있어 본인 확정을 보류한다(Codex P1-C: "김건희 특검 윤석열 구속영장").
        if (len(between) <= 16
                and not any(b in between for b in _TITLE_CLAUSE_BREAKS)
                and not any(rel in between for rel in _RELATION_PERSON_MARKERS)
                and not any(g in between for g in _CRIME_SUBJECT_GENERIC)
                and not _has_other_name_candidate(between, lead)):
            name_bound_to_disp = True
    # 제목 전체(처분어 이전)에 관계명사·익명 주체·다른 실명 후보가 이름과 별개로 있으면
    # 본인 확정 보류. 처분어 이후는 "청구/발부" 등 절차어라 주체 판단에 무의미하므로 제외.
    head = title[:nearest_disp] if nearest_disp is not None else title
    other_subject_present = (
        any(rel in title for rel in _RELATION_PERSON_MARKERS)
        or any(g in after for g in _CRIME_SUBJECT_GENERIC)
        or _has_other_name_candidate(head[anchor_end:], lead)
    )
    if (has_josa or name_bound_to_disp) and not other_subject_present:
        return "subject"

    return "unknown"


def aggregate_crime_attribution(keyword: str, high_articles: List[Dict]) -> Dict:
    """고관련 기사군에서 이름 엔티티의 crime subject role 집계 → 안전 판정 신호.

    반환 dict:
    - crime_check_triggered: crime_keyword_requires_check(keyword)
    - crime_subject_count / crime_victim_count / crime_role_unknown_count
    - crime_attribution_verified_self: 이름이 실제 주체임이 적극 입증됐는가.
        (subject>=2 AND subject>victim AND victim<=1)
    - has_unsafe_crime_attribution: triggered AND NOT verified_self (fail-closed).
    """
    triggered = crime_keyword_requires_check(keyword)
    if not triggered:
        return {
            "crime_check_triggered": False,
            "crime_subject_count": 0,
            "crime_victim_count": 0,
            "crime_role_unknown_count": 0,
            "crime_attribution_verified_self": False,
            "has_unsafe_crime_attribution": False,
        }
    subject = victim = unknown = 0
    for a in high_articles:
        role = classify_crime_subject_role(keyword, a)
        if role == "subject":
            subject += 1
        elif role == "victim_or_bystander":
            victim += 1
        else:
            unknown += 1
    verified_self = subject >= 2 and subject > victim and victim <= 1
    return {
        "crime_check_triggered": True,
        "crime_subject_count": subject,
        "crime_victim_count": victim,
        "crime_role_unknown_count": unknown,
        "crime_attribution_verified_self": verified_self,
        "has_unsafe_crime_attribution": not verified_self,
    }


# === entity cohesion 신호(E) — dominant event / same-event burst ===
# BURST_HOURS: 서로 다른 언론사의 "같은 속보" 인정 시간창(published_at 간격 상한).
BURST_HOURS = 6.0
# 사건 토큰 판정에서 제외할 generic 서술어(cohesion 근거로 부적합). summarizer 생성어와
# 별개로 여기선 keyword cohesion 전용 최소 집합만 둔다(_GENERIC_SINGLE_TOKENS 재사용 + 확장).
_EVENT_TOKEN_STOPWORDS = _GENERIC_SINGLE_TOKENS | {
    "오늘", "내일", "어제", "기자", "종합", "속보", "단독", "인터뷰", "포토", "영상",
}


def _keyword_derived(tok: str, kw_tokens: set) -> bool:
    low = tok.lower()
    return any(k in low or low in k for k in kw_tokens)


def _dominant_event_tokens(keyword: str, high_articles: List[Dict]) -> set:
    """고관련 기사군에서 서로 다른 기사 2건+ 이 실제로 공유하는(DF>=2) 사건 토큰
    (keyword 파생/generic 제외).

    핵심(2026-07): dominant cluster 하나의 토큰이 아니라 "전체 고관련 기사에서 문서빈도
    >=2인 공통 토큰"을 본다. 이래야 한화(야구 vs 그룹, 공통 토큰 없음)는 빈 집합이 되고
    세라젬(3건이 '롯데오픈' 공유)이나 한화 7연패(여러 기사가 '7연패/탈출' 공유)는 사건
    토큰이 남는다. cluster 하나만 보면 단건 cluster의 기사 토큰이 전부 잡혀 다중사건도
    통과하는 오류가 생긴다(회귀 발견 2026-07). summarizer.subtopic_tokens와 같은 원리다.
    """
    if len(high_articles) < 2:
        return set()
    kw_tokens = {t.lower() for t in _tokens(keyword or "")}
    df: Dict[str, int] = {}
    for a in high_articles:
        text = f"{a.get('title', '')} {a.get('clean_description') or a.get('snippet', '')}"
        for tok in set(_tokens(text)):
            df[tok] = df.get(tok, 0) + 1
    return {
        t for t, c in df.items()
        if c >= 2 and not _keyword_derived(t, kw_tokens) and t not in _EVENT_TOKEN_STOPWORDS
    }


# dominant event 인정에 필요한 최소 독립 언론사 수(일반 이슈 기준). 속보 예외는
# _same_event_burst로 2개 언론사까지 별도 완화한다(사용자 지시: 일반 3곳+, 속보 2곳 예외).
DOMINANT_EVENT_MIN_PRESS = 3


def _has_dominant_event(keyword: str, high_articles: List[Dict]) -> bool:
    """고관련 기사군에 "지배적 단일 사건"이 있는가 — keyword 제외 공통 사건 토큰을
    공유하는 기사가 **서로 다른 언론사 DOMINANT_EVENT_MIN_PRESS(3)곳 이상**이면 True.

    단순히 DF>=2 토큰 존재만 보면(구 구현) 같은 언론사 연속보도나 timestamp 없는 2건도
    통과해 _same_event_burst의 press/시간 계약이 무의미해진다(Codex 최종리뷰 P1). 일반
    이슈는 서로 다른 언론사 3곳+의 직접관련 기사를 요구하고(사용자 지시), 그 미만이라도
    시간근접·사건일치가 충분한 속보는 _same_event_burst가 별도로 통과시킨다.

    한화 다중사건(야구/그룹, 공통 사건토큰 없음)은 dominant도 burst도 아니어서 False.
    표현이 달라 Jaccard cluster가 쪼개진 정상 이슈도 사건토큰 공유 언론사 3곳+면 구제된다.
    """
    event_tokens = _dominant_event_tokens(keyword, high_articles)
    if not event_tokens:
        return False
    kw_tokens = {t.lower() for t in _tokens(keyword or "")}
    # 토큰별 독립 언론사 수를 센다. event_tokens 합집합의 press union이 아니라, **하나의
    # 사건 토큰**이 서로 다른 언론사 3곳+에서 등장해야 dominant로 인정한다(Codex 최종리뷰 P1
    # 잔여: alpha/beta가 press를 나눠 가지면 합집합은 3곳이어도 단일 사건이 아님). 이래야
    # "같은 사건을 3개 언론사가 보도"만 통과하고, 서로 다른 사건이 토큰 브리징으로 합산되는
    # 오탐을 막는다.
    token_presses: Dict[str, set] = {t: set() for t in event_tokens}
    for a in high_articles:
        press = a.get("press")
        if not press or press == PRESS_UNKNOWN:
            continue
        toks = {
            t for t in _tokens(f"{a.get('title', '')} {a.get('clean_description') or a.get('snippet', '')}")
            if not _keyword_derived(t, kw_tokens) and t not in _EVENT_TOKEN_STOPWORDS
        }
        for t in (toks & event_tokens):
            token_presses[t].add(press)
    return any(len(p) >= DOMINANT_EVENT_MIN_PRESS for p in token_presses.values())


def _same_event_burst(keyword: str, high_articles: List[Dict]) -> bool:
    """속보 예외 독립 신호(Codex P2-E): 고관련 기사쌍 중 (a) keyword 제외 공통 non-generic
    사건 토큰 1개+ 공유 AND (b) 서로 다른 press(PRESS_UNKNOWN 제외) AND (c) published_at
    파싱되고 시간차 <= BURST_HOURS 인 쌍이 존재하면 True.

    "한화"만 공유(사건 토큰 없음)/같은 press 연속보도/timestamp 누락은 자격 없음(보수적).
    cluster_articles Jaccard와 무관한 독립 신호라, 표현이 달라 cluster가 쪼개진 정상 속보를
    구제한다.
    """
    kw_tokens = {t.lower() for t in _tokens(keyword or "")}

    def event_toks(a):
        toks = set(_tokens(f"{a.get('title','')} {a.get('clean_description') or a.get('snippet','')}"))
        return {t for t in toks if not _keyword_derived(t, kw_tokens) and t not in _EVENT_TOKEN_STOPWORDS}

    enriched = []
    for a in high_articles:
        age = _age_hours(a.get("published_at"))
        press = a.get("press")
        if age is None or not press or press == PRESS_UNKNOWN:
            continue
        enriched.append((a, event_toks(a), age, press))

    for i in range(len(enriched)):
        for j in range(i + 1, len(enriched)):
            a1, t1, age1, p1 = enriched[i]
            a2, t2, age2, p2 = enriched[j]
            if p1 == p2:
                continue
            if not (t1 & t2):
                continue
            if abs(age1 - age2) <= BURST_HOURS:
                return True
    return False


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
    - 단일 토큰 예외(2026-07-05): effective_keyword가 비-모호 토큰 "하나"뿐인
      고유명(인물명 등)일 때는, 위 다-토큰 조건들이 구조적으로 성립할 수 없다(토큰이
      2개 이상 겹칠 수가 없음). 이 경우 그 앵커 토큰이 기사 title 주제이고
      (relevance_reason == keyword_main_topic, is_incidental=False) 그 토큰이 실제로
      겹치면 허용한다 — "장동건"/"박소영"처럼 단일 인물명 키워드의 고관련 기사가
      primary cluster 밖이라는 이유만으로 display에서 대량 탈락하는 문제를 막는다.
      "공유"/"성과" 같은 generic 단일 토큰은 _GENERIC_SINGLE_TOKENS에서 이미 걸러지므로
      이 예외를 타지 못한다(오염 방어 유지).
    - "공유"처럼 모호한 단일 토큰 하나만 겹치는 기사는 제외.

    sense-mixing 방어(2026-07): article.is_off_primary_sense=True(compute_news_signal의
    mark_off_primary_sense가 부여 — non-primary cluster이면서 keyword와 공통 토큰이
    없는 "다른 의미" 기사)이면, 아래 단일 고유토큰 예외를 먼저 평가하고 그것도 통과 못
    하면 즉시 제외한다(기존 anchor overlap 조건으로 내려가지 않음). "위홀 뜻" 사례의
    앤디워홀 기사가 우연한 토큰 overlap으로 새어나가는 것을 막는다. 단일 고유토큰 예외
    (장동건류)는 이 방어보다 먼저 평가해 기존 동작을 그대로 보존한다.
    """
    text = f"{article.get('title', '')} {article.get('clean_description') or article.get('snippet', '')}"
    text_tokens = set(_tokens(text))

    kw_tokens = set(_tokens(effective_keyword))
    matched_kw = _matched_tokens(kw_tokens, text, text_tokens)
    non_generic_matched = matched_kw - _GENERIC_SINGLE_TOKENS

    # 단일 non-generic 토큰(고유명/인물명) 키워드 예외 — title 주제 기사만 허용.
    # "키워드 자체가 토큰 1개"일 때만 적용한다(Codex review-only P1, 2026-07-05):
    # len(kw_non_generic)==1만 보면 "여행 공유"/"지원 발표"처럼 generic을 뺀 뒤 1개만
    # 남는 다토큰 키워드까지 anchor 검증 없이 예외를 타 오염이 다시 샌다. 원래 키워드가
    # 단일 토큰("장동건")이고 그 토큰이 generic이 아닐 때로 좁힌다.
    # entity-role 게이트(D, 2026-07): entity 키워드 정제로 부여된 entity_role이 있으면,
    # single_token_exception(장동건류 보존 경로)은 role이 non_subject가 "아닐" 때만 탄다.
    # non_subject(정치 수사/비교 대상)는 keyword_main_topic(0.9)이어도 예외를 못 탄다 —
    # 신천지 정치 기사가 단일토큰 예외로 새는 것을 차단(Codex 계획리뷰 P1-1/2). role이
    # 없는(event/unknown 키워드) 기사는 기존 동작 유지.
    role = article.get("entity_role")
    if role == "non_subject":
        return False
    single_token_exception = (
        len(kw_tokens) == 1
        and not (kw_tokens & _GENERIC_SINGLE_TOKENS)
        and non_generic_matched
        and not article.get("is_incidental")
        and article.get("relevance_reason") == "keyword_main_topic"
    )
    if single_token_exception:
        return True

    if article.get("is_off_primary_sense"):
        return False

    rep_title = (representative or {}).get("title") or ""
    rep_tokens = set(_tokens(rep_title))
    shared_with_rep_all = _matched_tokens(rep_tokens, text, text_tokens)
    shared_with_rep = shared_with_rep_all - _GENERIC_SINGLE_TOKENS

    if len(non_generic_matched) >= 2:
        return True
    if len(non_generic_matched) >= 1 and len(matched_kw) >= 2 and len(shared_with_rep) >= 1:
        return True

    if len(shared_with_rep) >= 2:
        return True

    return False


def canonical_evidence(news_meta: Dict, keyword: str, max_articles: Optional[int] = None) -> tuple:
    """canonical evidence set 단일 진실원(F, 2026-07). builder·B2·display-min gate가 모두
    이 helper를 호출해 "동일한 정제 후 기사 집합 + summary_type"을 얻는다.

    반환: (articles, summary, summary_type).
    - articles = dedup_articles(news_meta.articles) → filter_articles_for_display(min=ARTICLES_MIN)
      → [:max_articles]. (builder.build_ranked_entry:92-93와 동일 파이프)
    - summarize(**canonical keyword**, articles) — display_keyword가 아니라 keyword.
      builder(builder.py:94)가 keyword로 summarize하므로, drift 방지를 위해 여기서도 keyword.

    ARTICLES_MIN/ARTICLES_MAX는 builder에서 import(순환 회피 위해 함수 내부 지역 import).
    """
    from news.dedup import dedup_articles
    from news.builder import ARTICLES_MIN, ARTICLES_MAX
    if max_articles is None:
        max_articles = ARTICLES_MAX
    deduped = dedup_articles(news_meta.get("articles") or [])
    articles = filter_articles_for_display(deduped, min_count=ARTICLES_MIN)[:max_articles]
    summary, summary_type = summarize(keyword, articles)
    return articles, summary, summary_type


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
        # primary cluster 기사도 entity-role이 non_subject면 제외한다(D, 2026-07). 정제(C)가
        # non_subject를 이미 걸러내지만, 전부 non_subject라 정제가 되돌려진 방어 케이스나
        # cluster 재계산 결과 non_subject가 primary에 남는 경우까지 이중으로 막는다 —
        # "primary면 무조건 통과"가 신천지 정치기사를 노출시켰던 근본 경로(Codex P1-1).
        if a.get("entity_role") == "non_subject":
            continue
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
    phrase_reserve: int = 0,
) -> List[dict]:
    """홈/트렌드 seed + 보조후보를 병합/dedup → [{keyword, sources:{...}}] (상한 적용).

    seed_sources: {family: [{keyword, rank}]}. family 키는 그대로 candidate.sources 키가 된다
      (google_trends/daum_home/nate_home/bing_home 등). aux/phrase는 naver_news_* 로 표기.
    phrase_keywords: backfill pass 전용 phrase 후보(derive_phrase_candidates 결과).
    strict pass(pass1) 호출부는 phrase_keywords를 생략하면 기존과 동일하게 동작한다.

    phrase_reserve: phrase 후보(naver_news_phrase 전용, seed/aux와 겹치지 않는 순수 phrase)
      중 truncation 상한과 무관하게 최소 이만큼은 보존한다(2026-07, Codex diff 리뷰 P1).
      기본값 0이면 기존 동작(정렬 후 단순 [:limit])과 완전히 동일하다. rank 없는 phrase 후보는
      _best_family_rank=9999로 정렬상 맨 뒤에 밀려, seed가 limit을 가득 채우면 phrase가 통째로
      잘려 4안(phrase 원천 확장)이 무력화될 수 있다. 이를 막기 위해 상한 초과 시 rank 있는 seed
      쪽에서 limit-phrase_reserve 개를 채우고, 남은 자리에 순수 phrase 후보를 우선 채운다.
      다양성 guard/consensus는 여전히 독립 family만 세므로(phrase는 비독립) 이 보존이 guard를
      우회시키지 않는다.
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

    if len(candidates) <= limit:
        return candidates

    # phrase_reserve가 limit을 넘으면 상한 초과가 될 수 있어 clamp(함수 계약 "전체 limit
    # 유지" 방어, Codex diff 리뷰 P3). 현재 호출(10<45)에선 무해하지만 계약상 보장한다.
    phrase_reserve = min(phrase_reserve, limit)
    if phrase_reserve <= 0:
        return candidates[:limit]

    # phrase truncation 보호: 순수 phrase 후보(sources가 naver_news_phrase 뿐 — seed/aux와
    # 겹치지 않아 정렬상 뒤로 밀려 잘리는 후보)를 최대 phrase_reserve개 우선 보존한다.
    def _is_pure_phrase(c: dict) -> bool:
        srcs = set((c.get("sources") or {}).keys())
        return srcs == {"naver_news_phrase"}

    head = candidates[:limit]
    tail = candidates[limit:]
    head_phrase = sum(1 for c in head if _is_pure_phrase(c))
    need = phrase_reserve - head_phrase
    if need <= 0:
        return head  # 이미 상한 안에 충분한 phrase가 들어옴

    tail_phrases = [c for c in tail if _is_pure_phrase(c)][:need]
    if not tail_phrases:
        return head
    # 상한을 유지하기 위해, head의 non-phrase 후보를 뒤에서부터 tail_phrases 수만큼 덜어낸다
    # (rank가 낮은 seed부터 빠지도록 — head는 이미 rank 오름차순 정렬).
    keep = []
    drop_budget = len(tail_phrases)
    for c in reversed(head):
        if drop_budget > 0 and not _is_pure_phrase(c):
            drop_budget -= 1
            continue
        keep.append(c)
    keep.reverse()
    return keep + tail_phrases


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


def source_family_distribution(items: List[dict]) -> Dict[str, int]:
    """items(candidate/ranked/merged 등 sources를 가진 항목) 전체에서 source family별
    등장 항목 수 분포(다양성 관찰 로깅 전용, 2026-07).

    - count_source_families()와 달리 독립 family(_INDEPENDENT_SEARCH_FAMILIES)뿐 아니라
      naver_news_aux/naver_news_phrase까지 "등장한 모든 family"를 센다. 한 항목이 여러
      family에 걸쳐 있으면 각 family에 1씩 가산한다(항목 수 합계 != len(items) 가능).
    - ranking/merge/select 결과에 영향을 주지 않는 순수 집계 함수(로그 출력용).
    - merge된 항목은 canonical의 sources를 그대로 실어 나르므로(dedupe_and_merge §7-3),
      merge 후 단계에서도 원 후보의 family가 관측된다.
    """
    dist: Dict[str, int] = {}
    for it in items or []:
        for fam in (it.get("sources") or {}).keys():
            dist[fam] = dist.get(fam, 0) + 1
    return dist


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

    # ── entity-role 정제(C, 2026-07): 넓은 단일 엔티티 키워드에서 keyword가 사건 주체가
    #    아닌 오염 기사(정치 수사·snippet-only 등)를 제거한 뒤 clustering/representative/
    #    quality gate 신호를 "정제된 집합"으로 계산한다. 이 정제를 여기(cluster 계산 이전)에
    #    두어야 primary/representative/title_relevance/high_relevance_count/quality_cluster_size
    #    등 return dict의 모든 값이 정제본 기반이 된다(Codex 계획리뷰 P1-C: score/gate가
    #    news_meta를 즉시 소비하므로 ranker 단계 정제는 늦음). event/unknown 키워드는 정제를
    #    건너뛰어 기존 동작을 100% 보존한다(회귀 방어).
    keyword_kind = classify_keyword_kind(keyword)
    entity_role_reasons: Dict[int, str] = {}
    if keyword_kind == "entity":
        for a in scored_articles:
            role, reason = classify_entity_role(keyword, a)
            a["entity_role"] = role
            a["entity_role_reason"] = reason
            entity_role_reasons[id(a)] = reason
        # strong non_subject 기사만 canonical evidence set에서 제외한다. unknown은 보존
        # (과잉 제외 금지 — 장동건류 단일 인물 다사건 보존). 정제 후 기사가 하나도 없으면
        # (전부 non_subject) 정제를 되돌린다 — 이 경우는 아래 gate/B2가 자연히 걸러낸다.
        refined = [a for a in scored_articles if a.get("entity_role") != "non_subject"]
        if refined:
            scored_articles = refined
    else:
        # comparison 대상 정제(A, 2026-07-21): entity가 아닌 keyword('애플 카드' 같은 다토큰
        # unknown 포함)도 "비교/맞불 대상 위치 + 별도 주체 확인"이면 non_subject로 강등한다.
        # entity 블록과 달리 comparison 신호만 본다(전체 entity-role 판정은 event/unknown에
        # 적용하지 않는다는 기존 계약 유지 — 산불/폭우 등 사건어·다토큰 정상 이슈 보호).
        # comparison-dominant 모수: keyword 첫 토큰이 title 어절에 **명시적 표기변형**으로
        # 등장하는 기사. ranker._word_contains_token(grounding과 동일한 token/alias 경계 계약)을
        # 재사용해 별도 startswith 규칙을 두지 않는다(사용자 P1 사전검토 2차, 2026-07-21).
        # 이전엔 `t.startswith(kw_first_tok)`(무제한 접두)이라 '삼성'←'삼성물산', '카드'←
        # '카드뉴스'처럼 다른 개념 기사가 분모에 오포함될 수 있었다. 이제 조사결합('미국은'←
        # '미국')·명시 alias·sibling 붙여쓰기 복합('애플카드에'←'애플'+'카드')만 인정하고,
        # '파인애플'←'애플'/'삼성물산'←'삼성'/'카드뉴스'←'카드'는 배제된다. siblings는 keyword의
        # 나머지 토큰(다토큰 keyword의 붙여쓰기 복합 근거)을 넘긴다.
        from news import ranker as _ranker
        kw_toks_all = _tokens(keyword)
        kw_first_tok = kw_toks_all[0] if kw_toks_all else None
        kw_siblings = set(kw_toks_all[1:]) if len(kw_toks_all) > 1 else set()
        kw_first_alias = _ranker._institution_alias_forms(kw_first_tok) if kw_first_tok else set()
        # 문맥 alias(약칭↔정식명칭)도 grounding과 **동일한 충돌·최소 증거 계약**으로 재사용한다
        # (_contextual_alias_forms). 단순 접두 매칭을 다시 도입하지 않으며, 확장형이 충돌하거나
        # 증거가 부족하면 매핑에 안 들어가 kw_first_alias가 그대로 유지된다(사용자 P1 2차).
        if kw_first_tok:
            ctx_alias = _ranker._contextual_alias_forms(set(kw_toks_all), scored_articles)
            if kw_first_tok in ctx_alias:
                kw_first_alias = set(kw_first_alias) | ctx_alias[kw_first_tok]
        if kw_first_tok:
            # title을 _tokens(정규식 [가-힣A-Za-z0-9]{2,})로 어절 토큰화한다 — .split()은
            # '출시…애플카드에'처럼 구두점(…·)으로 붙은 어절을 못 나눠 '애플카드에'를 놓친다.
            title_present = [
                a for a in scored_articles
                if any(
                    _ranker._word_contains_token(tok, kw_first_tok, kw_siblings, kw_first_alias)
                    for tok in _tokens(a.get("title") or "")
                )
            ]
            # comparison-dominant 최소표본(>=2)은 **독립 기사 수**로 세야 한다(Codex P2,
            # 2026-07-22). scored_articles는 URL dedup 전이라 동일 URL 기사가 2번 있으면
            # title_present=2로 잘못 충족돼 comparison_dominant가 오발동한다. 상위 파이프라인의
            # dedup 계약(news.dedup.dedup_articles = 동일 url 1건, 입력순서 유지)과 동일한
            # URL identity로 evidence 모수를 dedup한다. dedup_articles는 url 없는 기사를
            # 제거하지만, comparison 모수에선 url 없는 기사도 각각 독립으로 세야 하므로
            # (fallback: 객체 identity) 그 부분만 로컬 처리한다.
            title_present = _dedup_by_url_identity(title_present)
        else:
            title_present = []
        comp_hits, subj_hits = 0, 0
        for a in title_present:
            if _comparison_target_role(keyword, a.get("title") or ""):
                comp_hits += 1
            elif _keyword_is_title_subject(keyword, a.get("title") or ""):
                subj_hits += 1
        # comparison-dominant 조건(사용자 P1 보완, 2026-07-21): 기사 1건짜리 오판으로 전체
        # 후보가 탈락하는 것을 막기 위해 최소 표본 + 비율 + 혼재 배제를 모두 요구한다.
        #   1) 최소 표본: title에 keyword가 등장하는 기사가 COMPARISON_DOMINANT_MIN_ARTICLES
        #      건 이상이어야 한다(기사 1건만의 comparison 언급으로는 판정하지 않음 — 단일
        #      기사의 서술 습관/오탐 가능성이 너무 크다).
        #   2) 혼재 배제: subj_hits>=1이면(주체로 쓰인 기사가 하나라도 있으면) 그 자체로
        #      비강등(subj_hits==0 유지 — 기존 계약과 동일, 혼재 시 "주체 증거 우선").
        #   3) 비율: comp_hits가 title_present 중 COMPARISON_DOMINANT_MIN_RATIO 이상이어야
        #      한다(desc-only 기사까지 전부 날리는 강한 조치이므로, 소수 기사의 비교 언급
        #      만으로는 부족 — 다수 기사에 걸쳐 일관되게 비교 대상으로 쓰일 때만 인정).
        comparison_dominant = (
            subj_hits == 0
            and len(title_present) >= COMPARISON_DOMINANT_MIN_ARTICLES
            and comp_hits / len(title_present) >= COMPARISON_DOMINANT_MIN_RATIO
        )
        for a in scored_articles:
            comp = _comparison_target_role(keyword, a.get("title") or "")
            if comp:
                a["entity_role"] = "non_subject"
                a["entity_role_reason"] = comp
                entity_role_reasons[id(a)] = comp
            elif comparison_dominant:
                a["entity_role"] = "non_subject"
                a["entity_role_reason"] = "NONSUBJECT_COMPARISON_DOMINANT"
                entity_role_reasons[id(a)] = "NONSUBJECT_COMPARISON_DOMINANT"
        refined = [a for a in scored_articles if a.get("entity_role") != "non_subject"]
        if refined:
            scored_articles = refined
        elif comparison_dominant:
            # 전부 comparison으로 강등됐다 = keyword가 이 이슈의 주체가 아님이 확정적.
            # 이때는 entity 블록의 "전부 non_subject면 롤백"(과잉 제외 방지)을 적용하지 않고
            # 빈 evidence를 유지한다 → high_relevance_count 0 → quality gate에서 자연 탈락하고
            # 다른 실제 주체 후보가 canonical이 된다("애플 카드"가 삼성 이슈의 대표로 승격되는
            # 것을 막는 핵심 경로). comparison 신호가 아니라 단순 롤백 케이스는 위 if로 보존.
            scored_articles = refined
    keyword_kind_effective = keyword_kind

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

    # sense-mixing 방어(2026-07) — non-primary cluster 중 keyword와 다른 의미로
    # 판별되는 기사에 is_off_primary_sense 플래그 부여(_display_anchor_allowed에서 소비).
    mark_off_primary_sense(keyword, scored_articles, primary)
    off_primary_sense_count = sum(1 for a in scored_articles if a.get("is_off_primary_sense"))

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

    # dominant event / same-event burst 신호(E, 2026-07) — entity 키워드 cohesion gate용.
    # entity 키워드에서 "고관련 2건이 실제로 같은 사건인가"를 판정한다. 단일 엔티티 토큰만
    # 공유하고 사건이 다른 기사(한화 야구 vs 한화그룹)는 dominant도 burst도 아니어서 gate
    # 미달이 된다. event/unknown 키워드는 이 신호를 gate에서 소비하지 않는다(ranker 판단).
    has_dominant_event = _has_dominant_event(keyword, high_relevance_articles)
    same_event_burst = _same_event_burst(keyword, high_relevance_articles)
    dominant_event_tokens = sorted(_dominant_event_tokens(keyword, high_relevance_articles))

    # crime-attribution safety(G, 2026-07-21) — 이름+범죄어 직결 키워드에서 실제 범죄
    # 주체가 이름 엔티티인지 기사 증거로 판정한다. crime keyword 아니면 필드가 전부
    # 무해한 기본값(triggered=False, unsafe=False)이라 비범죄 이슈에 영향이 없다.
    # 고관련 기사가 부족할 때도 판정이 가능하도록, 고관련이 2건 미만이면 정제된 전체
    # 기사(scored_articles)로 fallback 한다(fail-closed 를 위해 증거를 넓게 본다).
    crime_articles = high_relevance_articles if len(high_relevance_articles) >= 2 else scored_articles
    crime_signal = aggregate_crime_attribution(keyword, crime_articles)

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
        "off_primary_sense_count": off_primary_sense_count,
        # entity-role 정제 결과(E/G/진단용). event/unknown은 정제 미적용.
        "keyword_kind": keyword_kind_effective,
        "refined_article_count": len(scored_articles),
        # cohesion 신호(E) — ranker._quality_gate_reason이 entity 키워드에만 소비.
        "has_dominant_event": has_dominant_event,
        "same_event_burst": same_event_burst,
        "dominant_event_tokens": dominant_event_tokens,
        # crime-attribution safety(G) — ranker._quality_gate_reason이 fail-closed 소비.
        "crime_check_triggered": crime_signal["crime_check_triggered"],
        "crime_subject_count": crime_signal["crime_subject_count"],
        "crime_victim_count": crime_signal["crime_victim_count"],
        "crime_role_unknown_count": crime_signal["crime_role_unknown_count"],
        "crime_attribution_verified_self": crime_signal["crime_attribution_verified_self"],
        "has_unsafe_crime_attribution": crime_signal["has_unsafe_crime_attribution"],
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
