"""규칙 기반 요약 (P0).

여러 기사의 제목/snippet에서 공통 핵심을 추출한다.
- 생성/추론 없음. 원문 표현만 재사용.
- 기사가 없으면 summary_type='seed_only' (키워드만 노출).
- AI 요약은 P1.
"""
import re
from typing import List, Tuple

_TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9]{2,}")

# 요약 토큰 빈도 집계에서 제외할 일반어 (최소셋)
_STOPWORDS = {
    "기자", "뉴스", "오늘", "관련", "이번", "지난", "대한", "위해", "그리고",
    "그러나", "단독", "속보", "종합", "사진", "영상", "공식", "최근",
}

SUMMARY_MAX = 120


def _tokens(text: str) -> List[str]:
    return [t for t in _TOKEN_RE.findall(text or "") if t not in _STOPWORDS]


def summarize(keyword: str, articles: List[dict]) -> Tuple[str, str]:
    """(summary, summary_type) 반환.

    - articles 없음 → ("", "seed_only")
    - articles 1건 → 그 기사 제목을 요약으로 ("title")
    - articles 2건+ → 제목/snippet 공통 토큰 상위로 대표 문장 선택 ("rule")
    """
    if not articles:
        return "", "seed_only"

    if len(articles) == 1:
        title = (articles[0].get("title") or "").strip()
        return title[:SUMMARY_MAX], "title"

    # 여러 기사: 토큰 빈도 → 대표 기사(공통 토큰을 가장 많이 포함한 제목) 선택
    freq = {}
    for a in articles:
        text = f"{a.get('title', '')} {a.get('snippet', '')}"
        for tok in set(_tokens(text)):  # 기사당 1회 (문서 빈도)
            freq[tok] = freq.get(tok, 0) + 1

    # 2개 이상 기사에서 등장한 공통 토큰만 신호로 사용
    common = {t for t, c in freq.items() if c >= 2}

    best_title = ""
    best_score = -1
    for a in articles:
        title = (a.get("title") or "").strip()
        if not title:
            continue
        score = sum(1 for tok in set(_tokens(title)) if tok in common)
        if score > best_score:
            best_score = score
            best_title = title

    if not best_title:
        best_title = (articles[0].get("title") or "").strip()

    return best_title[:SUMMARY_MAX], "rule"
