"""issues JSON 조립.

news_issue_cache.issues = { "keywords": [ {rank, keyword, summary, summary_type,
signals{news,trend}, trend, articles[]} ] }

P0: trend는 null 고정(데이터랩은 P1). thumbnail은 null(normalizer에서 처리).
"""
from datetime import datetime, timezone
from typing import Callable, List, Optional

from news.candidates import build_display_articles, filter_articles_for_display
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
        "summary_type": summary_type,  # rule | title | seed_only | no_representative
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

    ranked_item: {keyword, score, source_breakdown, rank_reason, news_meta, used_signals,
                  (dedupe/merge 후) related_keywords, aliases, display_keyword, merge_reason}
    news_meta.articles 는 candidates에서 relevance_score 내림차순으로 정렬된 normalize 결과.
    dedup_articles()는 URL 기준 제거만 하고 입력 순서를 보존하므로, 여기서 재정렬 없이도
    relevance/primary cluster 우선 순서가 유지된다. filter_articles_for_display()가 incidental/
    저관련 기사를 기본 제외(부족하면 ARTICLES_MIN 하한 보호를 위해 relevance 높은 순 보충)한다.

    candidate lookup 실패 방어(docs/news-ranking-quality-plan.md §7-3): dedupe/merge로
    keyword가 canonical 값으로 유지되더라도, ranked_item에 sources가 직접 실려 있으면
    그것을 candidate_map lookup보다 우선 신뢰한다.
    """
    keyword = ranked_item["keyword"]
    news_meta = ranked_item.get("news_meta") or {}
    raw_articles = news_meta.get("articles") or []
    deduped = dedup_articles(raw_articles)
    articles = filter_articles_for_display(deduped, min_count=ARTICLES_MIN)[:max_articles]
    summary, summary_type = summarize(keyword, articles)
    # representative_summary → representative_title → summarize() 결과(article title fallback) 순.
    representative_summary = news_meta.get("representative_summary")
    representative_title = news_meta.get("representative_title")

    # broad/generic 키워드 방어(2026-07-15): 기사 간 공통 하위주제가 없어 대표를 뽑지
    # 않기로 한 키워드는, summary뿐 아니라 노출되는 대표 관련 필드도 모두 비운다.
    # 그렇지 않으면 summary만 비고 news_meta의 representative_*가 그대로 남아 상세
    # 팝업이 여전히 특정 기사를 "대표"로 간주한다(요구사항: 어느 한 기사도 대표로
    # 선택되지 않음). articles/display_articles(관련 기사 목록)와 랭킹 신호는 유지한다.
    no_representative = summary_type == "no_representative"
    if no_representative:
        representative_summary = None
        representative_title = None

    breakdown = ranked_item.get("source_breakdown") or {}

    # display_articles(2026-07-04): 상세 팝업 노출 전용 — articles(랭킹/게이트 근거)는 그대로
    # 두고, display_keyword(merge 후 최종 표기) 기준으로 한 번 더 걸러낸다. 같은 keyword의
    # 일반어 토큰(예: "공유")만 겹치는 무관 기사가 상세 팝업에 섞이는 것을 방지한다.
    #
    # no_representative여도 여기엔 representative_article을 그대로 넘긴다.
    # build_display_articles는 이 값을 "대표 표기"가 아니라 primary cluster 밖 기사의
    # 연관성 재확인(_display_anchor_allowed) 기준으로만 쓴다. None을 넘기면 그 앵커
    # 검사가 무력화돼 팝업 기사 목록 자체가 바뀐다(요구사항: 목록은 그대로 유지).
    # 노출용 representative_article 필드만 아래 return에서 비운다.
    effective_keyword = ranked_item.get("display_keyword") or keyword
    display_articles = build_display_articles(
        effective_keyword, articles, news_meta.get("representative_article")
    )

    sources = ranked_item.get("sources") or (candidate or {}).get("sources") or {}

    return {
        "rank": rank,
        "keyword": keyword,
        "summary": summary,
        "summary_type": summary_type,
        "signals": {
            "news": len(articles) > 0,
            "trend": False,  # 기존 호환
            "daum": sources.get("daum_home") is not None,
            "google": sources.get("google_trends") is not None,
            "nate": sources.get("nate_home") is not None,
            "bing": sources.get("bing_home") is not None,
            "search_demand": breakdown.get("search_demand", 0) > 0,
        },
        "trend": None,  # 기존 호환 (datalab 점수화 객체는 후속)
        "articles": articles,
        "display_articles": display_articles,
        # ===== 신규 optional =====
        "score": ranked_item.get("score", 0.0),
        "rank_reason": ranked_item.get("rank_reason", ""),
        "source_breakdown": breakdown,
        "sources": sources,
        "display_keyword": ranked_item.get("display_keyword", keyword),
        "related_keywords": ranked_item.get("related_keywords", []),
        "aliases": ranked_item.get("aliases", []),
        "merge_reason": ranked_item.get("merge_reason"),
        "representative_title": representative_title,
        "representative_summary": representative_summary,
        "representative_article": None if no_representative else news_meta.get("representative_article"),
        "primary_cluster_size": news_meta.get("primary_cluster_size"),
        "topic_coherence": news_meta.get("topic_coherence"),
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
