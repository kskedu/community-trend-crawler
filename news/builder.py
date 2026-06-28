"""issues JSON 조립.

news_issue_cache.issues = { "keywords": [ {rank, keyword, summary, summary_type,
signals{news,trend}, trend, articles[]} ] }

P0: trend는 null 고정(데이터랩은 P1). thumbnail은 null(normalizer에서 처리).
"""
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
