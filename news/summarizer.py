"""규칙 기반 요약 (P0).

여러 기사의 제목/snippet에서 공통 핵심을 추출한다.
- 생성/추론 없음. 원문 표현만 재사용.
- 기사가 없으면 summary_type='seed_only' (키워드만 노출).
- 기사는 있으나 공통 하위주제가 없으면 summary_type='no_representative' (요약 미노출).
- AI 요약은 P1.
"""
import re
from typing import Dict, List, Set, Tuple

_TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9]{2,}")

# 요약 토큰 빈도 집계에서 제외할 일반어 (최소셋)
_STOPWORDS = {
    "기자", "뉴스", "오늘", "관련", "이번", "지난", "대한", "위해", "그리고",
    "그러나", "단독", "속보", "종합", "사진", "영상", "공식", "최근",
}

SUMMARY_MAX = 120

# === broad/generic 키워드 대표기사 억제(2026-07-15) ===
# 문제: "초복"처럼 검색 수요는 충분하지만 기사들이 키워드 단어만 공유하고 실제
# 사건·인물·기관·지역이 서로 다른 키워드에서, 기존 로직이 근거 없이 임의 기사를
# 대표로 뽑았다. 실측(초복 6건: 청도 화합행사/하림 팀워크/보은 삼계탕나눔/
# 성남시의회/대통령 오찬/폭염)에서 DF>=2 공통 토큰은 {초복, 초복을, 초복맞이,
# 15일, 14일, 맞아, 지역, 삼계탕}뿐 — 전부 (a)키워드 자신·파생형 (b)날짜
# (c)일반 동사 (d)세시풍속 소품이라 "공통 사건"의 증거가 아니다. 그런데도 4개
# 기사가 동점(score=2)이 되어 루프 최초 우승자가 대표로 확정됐다.
#
# 대표기사 생성 조건(명시):
#   1) 대표 자격 기사(evidence, _evidence_articles 참조)가 2건 이상이고
#   2) 키워드 자체/파생형·날짜·일반어를 제외한 잔여 공통 토큰 중
#   3) evidence의 "엄격한 과반"(n//2+1)에서 반복 등장하는 토큰(= 하위주제 토큰)이
#   4) _SUBTOPIC_MIN_TOKENS(2)개 이상일 때만
# 대표를 선정한다. 미달이면 ("", "no_representative") — summary를 특정 기사
# title로 채우지 않는다(홈은 문구 숨김, 팝업은 기사 목록만 유지).
_SUBTOPIC_MIN_TOKENS = 2      # 하위주제로 인정할 최소 토큰 수
_NUMERIC_LEAD_RE = re.compile(r"^\d")  # "15일"/"37"처럼 숫자로 시작하는 토큰(날짜·수치)

# 하위주제 근거로 인정할 최소 relevance — candidates.REPRESENTATIVE_MIN_RELEVANCE(0.5)와
# 같은 값이자 같은 의미다(대표 기사 자격 기준). candidates.py가 summarizer.py를 import하는
# 단방향 구조라 역참조하면 순환 import가 되므로 값을 여기 복제한다(candidates._cluster_common_tokens
# 가 같은 이유로 토큰 집계를 복제한 것과 동일한 선례). 한쪽을 바꾸면 다른 쪽도 함께 본다.
_EVIDENCE_MIN_RELEVANCE = 0.5

# 기사 간 공통 사건을 식별하지 못하는 일반 서술어/행사어 최소셋. 과반 임계가 대부분을
# 걸러내므로 사전을 키우지 않는다(fixture 과적합·유지보수 위험).
_SUBTOPIC_GENERIC_TOKENS = {
    "맞아", "맞이", "지역", "행사", "진행", "참석", "개최", "실시", "예정",
    "이날", "당일", "하루", "어제", "내일", "전국", "각각", "일부",
}


def _tokens(text: str) -> List[str]:
    return [t for t in _TOKEN_RE.findall(text or "") if t not in _STOPWORDS]


def _document_freq(articles: List[dict]) -> Dict[str, int]:
    """토큰별 문서빈도(기사당 1회 집계)."""
    freq: Dict[str, int] = {}
    for a in articles:
        text = f"{a.get('title', '')} {a.get('snippet', '')}"
        for tok in set(_tokens(text)):
            freq[tok] = freq.get(tok, 0) + 1
    return freq


def _evidence_articles(articles: List[dict]) -> List[dict]:
    """하위주제 근거로 쓸 기사만 남긴다 — 대표 기사 자격이 없는 기사는 제외.

    builder는 summarize() 호출 전에 filter_articles_for_display(min_count=ARTICLES_MIN)로
    "관련 기사가 부족하면 저관련/incidental 기사를 하한까지 보충"한다. 이 보충분을 과반
    분모에 넣으면, 동일 사건 기사 2건 + 보충 3건인 정상 키워드가 need=3이 되어 대표가
    억울하게 억제된다(Codex review-only P1, 2026-07-15). 보충된 기사도 is_incidental/
    relevance_score 필드를 그대로 유지하므로 여기서 식별해 분모에서 뺀다.
    표시용 목록(display_articles)은 건드리지 않는다 — 근거 집계에서만 제외한다.

    자격 기준은 candidates.select_representative/build_representative_summary와 동일하게
    "is_incidental=False 이고 relevance_score >= 0.5"다. is_incidental만 보면
    object_side_mention(0.35)처럼 대표 자격이 없는 기사가 분모에 남아 같은 과잉 억제가
    재발한다(Codex review-only P1 2차).

    판별 필드(is_incidental/relevance_score)가 "아무 기사에도 없는" 입력(구 fixture/단순
    호출)만 판별 근거가 없는 것으로 보고 원본을 그대로 쓴다. 한 기사라도 필드가 있으면 그
    판정을 신뢰한다 — 전부 자격 미달이면 근거 없음(빈 리스트)이 맞다. 이 구분이 없으면
    "명시적으로 전부 incidental"인 입력에서 원본이 되살아난다(Codex review-only P2).
    """
    if not any(("is_incidental" in a or "relevance_score" in a) for a in articles):
        return list(articles)  # legacy: 판별 필드 자체가 없는 입력
    return [
        a for a in articles
        if not a.get("is_incidental")
        and a.get("relevance_score", _EVIDENCE_MIN_RELEVANCE) >= _EVIDENCE_MIN_RELEVANCE
    ]


def subtopic_tokens(keyword: str, articles: List[dict]) -> Set[str]:
    """기사들이 공유하는 "하위주제 토큰" 집합.

    키워드 자체(및 파생형), 날짜/수치, 일반 서술어를 제외하고 과반 기사에서
    반복 등장하는 토큰만 남긴다. 비어 있으면 "키워드 말고는 공통점이 없다"는 뜻.

    과반 분모는 incidental 보충분을 뺀 "근거 기사"(_evidence_articles) 기준이다.

    keyword가 빈 문자열이면 키워드 제외를 적용하지 않는다 — substring 검사에서
    ""가 모든 토큰에 매칭돼 전부 지워지는 것을 막는다(빈 키워드 호출은
    candidates.build_representative_summary 경로. 그쪽은 이미 primary cluster/
    relevance로 걸러진 기사만 넘긴다).
    """
    if not articles:
        return set()

    articles = _evidence_articles(articles)
    kw_tokens = {t.lower() for t in _tokens(keyword or "")} if (keyword or "").strip() else set()

    def _is_keyword_derived(tok: str) -> bool:
        # 한 방향(keyword 토큰 ⊂ 후보 토큰)만 본다. 양방향으로 보면 짧은 키워드나
        # 복합 키워드에서 실제 하위주제 토큰까지 과잉 제거된다(Codex review-only P1).
        low = tok.lower()
        return any(k in low for k in kw_tokens)

    need = len(articles) // 2 + 1  # 엄격한 과반(n=6 → 4). ceil(n/2)는 과반이 아니다.
    return {
        tok
        for tok, df in _document_freq(articles).items()
        if df >= need
        and df >= 2                       # 반복 관측(최소 2개 기사) 없으면 근거 아님
        and not _is_keyword_derived(tok)
        and not _NUMERIC_LEAD_RE.match(tok)
        and tok not in _SUBTOPIC_GENERIC_TOKENS
    }


def has_representative(keyword: str, articles: List[dict]) -> bool:
    """대표기사를 생성할 근거(공통 하위주제)가 있는지 여부."""
    evidence = _evidence_articles(articles or [])
    if not evidence:
        return False  # 대표 자격 기사가 하나도 없으면 대표도 없다.
    if len(evidence) == 1:
        return True   # 1건은 "여러 기사 중 임의 선택" 문제 자체가 없다.
    return len(subtopic_tokens(keyword, articles)) >= _SUBTOPIC_MIN_TOKENS


def summarize(keyword: str, articles: List[dict]) -> Tuple[str, str]:
    """(summary, summary_type) 반환.

    - articles 없음 → ("", "seed_only")
    - 대표 자격 기사(evidence) 0건 → ("", "no_representative")
    - evidence 1건 → 그 기사 제목을 요약으로 ("title").
      단일 기사는 기사 간 합의가 성립하지 않는 명시적 예외다(임의 선택 문제도 없음).
    - evidence 2건+ 이고 공통 하위주제 없음 → ("", "no_representative")
    - evidence 2건+ → 하위주제 토큰을 가장 많이 포함한 제목 선택 ("rule")
    """
    if not articles:
        return "", "seed_only"

    # 대표 판정은 항상 evidence(대표 자격 기사) 기준이다 — 원본 articles 길이로 먼저
    # 분기하면 "명시적으로 전부 incidental인 1건"이 title로 새어나가 불변식이 깨진다
    # (Codex review-only P2).
    evidence = _evidence_articles(articles)
    if not evidence:
        return "", "no_representative"

    if len(evidence) == 1:
        title = (evidence[0].get("title") or "").strip()
        return (title[:SUMMARY_MAX], "title") if title else ("", "no_representative")

    # 키워드 말고 공통점이 없으면 대표를 뽑지 않는다(broad/generic 키워드 방어).
    subtopic = subtopic_tokens(keyword, articles)
    if len(subtopic) < _SUBTOPIC_MIN_TOKENS:
        return "", "no_representative"

    # 대표 채점도 하위주제 토큰으로 한다 — 키워드/날짜/일반어를 뺀 "사건 증거"를
    # 가장 많이 담은 제목이 대표다.
    # 대표 후보도 근거 기사로 제한한다 — 하한 보충된 incidental 기사가 대표가 되면 안 된다.
    best_title = ""
    best_score = 0
    for a in evidence:
        title = (a.get("title") or "").strip()
        if not title:
            continue
        score = sum(1 for tok in set(_tokens(title)) if tok in subtopic)
        if score > best_score:
            best_score = score
            best_title = title

    # 하위주제 증거가 snippet에만 있고 어느 title도 담지 않은 경우(best_score==0)는
    # 대표 title을 특정할 수 없다 — 임의 기사로 채우지 않는다.
    if not best_title:
        return "", "no_representative"

    return best_title[:SUMMARY_MAX], "rule"
