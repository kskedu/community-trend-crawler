"""issues JSON 조립.

news_issue_cache.issues = { "keywords": [ {rank, keyword, summary, summary_type,
signals{news,trend}, trend, articles[]} ] }

P0: trend는 null 고정(데이터랩은 P1). thumbnail은 null(normalizer에서 처리).
"""
from datetime import datetime, timezone
from typing import Callable, List, Optional

from news.dedup import dedup_articles
from news.normalizer import normalize_article
from news.summarizer import summarize

ARTICLES_MIN = 5
ARTICLES_MAX = 8


def build_keyword_entry(
    rank: int,
    keyword: str,
    raw_items: List[dict],
    max_articles: int = ARTICLES_MAX,
) -> dict:
    """단일 키워드 entry 조립. raw_items는 네이버 item(또는 fixture item) 리스트."""
    normalized = []
    for raw in raw_items or []:
        art = normalize_article(raw)
        if art:
            normalized.append(art)

    articles = dedup_articles(normalized)[:max_articles]
    summary, summary_type = summarize(keyword, articles)
    has_news = len(articles) > 0

    return {
        "rank": rank,
        "keyword": keyword,
        "summary": summary,
        "summary_type": summary_type,  # rule | title | seed_only
        "signals": {
            "news": has_news,
            "trend": False,  # P0: 데이터랩 미사용
        },
        "trend": None,  # P0 고정 null. score/label/chart는 P1.
        "articles": articles,
    }


def build_issues(
    seed_keywords: List[str],
    fetch_news: Callable[[str], List[dict]],
    max_articles: int = ARTICLES_MAX,
) -> dict:
    """seed 키워드 + 뉴스 fetch 함수로 issues dict 조립.

    fetch_news(keyword) -> raw item 리스트 (네이버 또는 fixture).
    뉴스가 비어도 키워드는 노출(signals.news=false, summary_type='seed_only').
    """
    keywords = []
    for idx, kw in enumerate(seed_keywords, start=1):
        raw_items = fetch_news(kw) or []
        keywords.append(build_keyword_entry(idx, kw, raw_items, max_articles))
    return {"keywords": keywords}


# === 통합 랭킹용 entry/issues 조립 ===

def build_ranked_entry(
    rank: int,
    ranked_item: dict,
    candidate: Optional[dict] = None,
    max_articles: int = ARTICLES_MAX,
) -> dict:
    """ranker 결과 1건 → keyword entry(optional 필드 확장).

    ranked_item: {keyword, score, source_breakdown, rank_reason, news_meta, used_signals}
    news_meta.articles 는 candidates에서 normalize/필터된 리스트.
    """
    keyword = ranked_item["keyword"]
    news_meta = ranked_item.get("news_meta") or {}
    raw_articles = news_meta.get("articles") or []
    articles = dedup_articles(raw_articles)[:max_articles]
    summary, summary_type = summarize(keyword, articles)
    breakdown = ranked_item.get("source_breakdown") or {}
    used = set(ranked_item.get("used_signals") or [])

    return {
        "rank": rank,
        "keyword": keyword,
        "summary": summary,
        "summary_type": summary_type,
        "signals": {
            "news": len(articles) > 0,
            "trend": False,  # 기존 호환
            "daum": (candidate or {}).get("sources", {}).get("daum") is not None
                    if candidate else ("daum" in used),
            "datalab": breakdown.get("datalab", 0) > 0,
            "google": breakdown.get("google", 0) > 0,
        },
        "trend": None,  # 기존 호환 (datalab 점수화 객체는 후속)
        "articles": articles,
        # ===== 신규 optional =====
        "score": ranked_item.get("score", 0.0),
        "rank_reason": ranked_item.get("rank_reason", ""),
        "source_breakdown": breakdown,
    }


def build_ranked_issues(
    top_items: List[dict],
    candidate_map: Optional[dict] = None,
    data_sources: Optional[List[str]] = None,
    max_articles: int = ARTICLES_MAX,
) -> dict:
    """Top10 ranked 리스트 → issues dict(루트 optional 필드 포함)."""
    candidate_map = candidate_map or {}
    keywords = []
    for idx, item in enumerate(top_items, start=1):
        cand = candidate_map.get(item["keyword"])
        keywords.append(build_ranked_entry(idx, item, cand, max_articles))
    return {
        "keywords": keywords,
        "data_sources": data_sources or [],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
