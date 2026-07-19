"""뉴스 선정 파이프라인 replay — 운영 DB 접근과 완전히 분리된 순수 재생기.

목적(2026-07): 같은 문제가 재발했을 때 화면 캡처만 보고 추측하지 않도록, 운영자가
**안전하게 export한 입력 스냅샷**(원천 기사 등)을 그대로 파이프라인 select 단계에 흘려
변경 전후 동작을 비교한다.

경계(엄수):
- 이 모듈은 Supabase/service_role/네트워크에 절대 접근하지 않는다. import도 하지 않는다.
- 과거 운영 진단 이력을 자동으로 끌어오는 기능은 이 PR 범위가 아니다(진단 이력은
  service_role 전용 RPC라 여기서 읽지 않는다). 입력은 호출자가 파일/dict로 제공한다.
- fetch_news는 주입받는다(fixture dict 또는 export된 원천). 실 네이버 호출 없음.

입력 형식(ReplayInput):
{
  "keywords": ["한화", "신천지", ...],          # 후보 키워드(순서 = seed 우선순위 근사)
  "articles_by_keyword": {                       # 키워드별 원천 기사(raw item)
     "한화": [{"title": ..., "originallink"/"link"/"url": ..., "description": ...,
               "pubDate": ...}, ...],
     ...
  },
  "sources_by_keyword": {"한화": {"daum_home": 1}, ...}  # (선택) candidate.sources
}

반환(replay_selection): 각 단계 통과/제외를 재구성한 dict — CLI/테스트/사람 비교용.
"""
from typing import Callable, Dict, List, Optional

from news import candidates as cand
from news import ranker
from news.builder import build_ranked_issues


def _fetch_from_input(articles_by_keyword: Dict[str, List[dict]]) -> Callable[[str], List[dict]]:
    """articles_by_keyword dict를 fetch_news(keyword)->raw list 형태로 감싼다(순수, DB 무관)."""
    def fetch(keyword: str) -> List[dict]:
        return list(articles_by_keyword.get(keyword) or [])
    return fetch


def replay_selection(replay_input: Dict, fetch_news: Optional[Callable[[str], List[dict]]] = None) -> Dict:
    """입력 스냅샷으로 select 단계를 재생한다(순수 함수, DB 접근 없음).

    흐름은 main._rank_and_select와 동일한 게이트 순서를 재사용한다:
      compute_scores(quality gate) → exclude_pr_clusters → dedupe_and_merge →
      resolve_singleton_displays → enforce_display_article_consistency →
      exclude_generic_singletons → exclude_insufficient_display_articles →
      exclude_no_representative → select_top.

    반환:
    {
      "candidates": [keyword,...],
      "signals_keywords": [...],           # news 신호가 생성된 키워드
      "gate_passed": [...],                # compute_scores 통과
      "pr_excluded": [...], "generic_excluded": [...],
      "display_excluded": [...], "no_rep_excluded": [...],
      "selected": [{rank, keyword, display_keyword, kind}],  # 최종 Top
      "per_keyword": {keyword: {kind, has_dominant_event, same_event_burst,
                               high_relevance_count, summary_type, refined_article_count,
                               entity_roles: {title: role}}},
      "issues": {...}                      # build_ranked_issues 결과(display_articles 포함)
    }
    """
    keywords = list(replay_input.get("keywords") or [])
    articles_by_keyword = replay_input.get("articles_by_keyword") or {}
    sources_by_keyword = replay_input.get("sources_by_keyword") or {}
    if fetch_news is None:
        fetch_news = _fetch_from_input(articles_by_keyword)

    candidates = [
        {"keyword": kw, "sources": dict(sources_by_keyword.get(kw) or {"daum_home": i + 1})}
        for i, kw in enumerate(keywords)
    ]

    news_signals = cand.build_news_signals(candidates, fetch_news)
    signals = {"news": news_signals, "datalab": {}, "google": {}}

    per_keyword = {}
    for kw, meta in news_signals.items():
        roles = {}
        for a in meta.get("articles") or []:
            if "entity_role" in a:
                roles[a.get("title", "")] = a.get("entity_role")
        per_keyword[kw] = {
            "kind": meta.get("keyword_kind"),
            "has_dominant_event": meta.get("has_dominant_event"),
            "same_event_burst": meta.get("same_event_burst"),
            "high_relevance_count": meta.get("high_relevance_count"),
            "refined_article_count": meta.get("refined_article_count"),
            "dominant_event_tokens": meta.get("dominant_event_tokens"),
            "entity_roles": roles,
        }

    ranked = ranker.compute_scores(candidates, signals)
    gate_passed = [r["keyword"] for r in ranked]
    ranked, pr_excluded = ranker.exclude_pr_clusters(ranked)
    merged = ranker.dedupe_and_merge(ranked)
    merged = ranker.resolve_singleton_displays(merged)
    merged = ranker.enforce_display_article_consistency(merged)
    kept, generic_excluded = ranker.exclude_generic_singletons(merged)
    kept, display_excluded = ranker.exclude_insufficient_display_articles(kept)
    kept, no_rep_excluded = ranker.exclude_no_representative(kept)
    top = ranker.select_top(kept)

    candidate_map = {c["keyword"]: c for c in candidates}
    issues = build_ranked_issues(top, candidate_map, data_sources=["replay"])

    return {
        "candidates": keywords,
        "signals_keywords": sorted(news_signals.keys()),
        "gate_passed": gate_passed,
        "pr_excluded": list(pr_excluded),
        "generic_excluded": list(generic_excluded),
        "display_excluded": list(display_excluded),
        "no_rep_excluded": list(no_rep_excluded),
        "selected": [
            {"rank": i + 1, "keyword": t["keyword"],
             "display_keyword": t.get("display_keyword", t["keyword"])}
            for i, t in enumerate(top)
        ],
        "per_keyword": per_keyword,
        "issues": issues,
    }
