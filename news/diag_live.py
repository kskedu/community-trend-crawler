"""news_top 7개 부족 원인 진단 전용 스크립트 (read-only, DB write 없음).

목적: daum/danawa seed 후보의 gate 통과/탈락 상세를 stdout 표로 확인한다.

안전 계약:
- Supabase read(keyword_cache)만 수행 — write/upsert 함수는 import하지 않는다.
- Naver News API read-only 호출만 수행(search_news) — 그 외 외부 호출 없음.
- main.py의 run()/run_news_briefing()은 호출하지 않는다.
- 결과는 stdout 표 출력만. 파일/DB에 아무것도 쓰지 않는다.

진단 완료 후 이 파일은 삭제된다(1회성 진단 도구).
"""
import logging
import sys

from news.seed import fetch_ranked_seed
from news.naver_news import search_news
from news import candidates as cand
from news import ranker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("news.diag_live")

_call_count = 0


def _counted_search_news(keyword: str):
    global _call_count
    _call_count += 1
    return search_news(keyword)


def _fmt(v):
    return "-" if v is None else v


def run():
    daum_ranked, daum_fresh = fetch_ranked_seed("daum")
    danawa_ranked, danawa_fresh = fetch_ranked_seed("danawa")
    logger.info("daum seed(%d, fresh=%s): %s", len(daum_ranked), daum_fresh, [c["keyword"] for c in daum_ranked])
    logger.info("danawa seed(%d, fresh=%s): %s", len(danawa_ranked), danawa_fresh, [c["keyword"] for c in danawa_ranked])

    if not daum_fresh:
        logger.warning("daum stale → 운영 파이프라인 동작대로 daum 후보 제외")
        daum_ranked = []

    aux = cand.derive_aux_keywords(daum_ranked, _counted_search_news)
    logger.info("aux 후보(%d): %s", len(aux), aux)

    candidates = cand.collect_candidates(daum_ranked, danawa_ranked, [], aux)
    logger.info("전체 후보 pool(%d, CANDIDATE_MAX=%d)", len(candidates), cand.CANDIDATE_MAX)

    news_signals = cand.build_news_signals(candidates, _counted_search_news)

    def _passes(meta):
        hrc = meta.get("high_relevance_count", 0)
        qcs = meta.get("quality_cluster_size", 0)
        relevance_ok = hrc >= 2 or qcs >= 2
        fresh_ok = meta.get("fresh_high_relevance_count", 0) >= 1
        return relevance_ok, fresh_ok

    rows = []
    for c in candidates:
        kw = c["keyword"]
        srcs = "+".join(sorted(c["sources"].keys()))
        meta = news_signals.get(kw)
        if meta is None:
            rows.append({
                "keyword": kw, "source": srcs, "articles": 0,
                "hrc": 0, "qcs": 0, "fresh_hrc": 0, "fresh_qcs": 0,
                "latest_age": None, "relevance_ok": False, "fresh_ok": False,
                "final": False, "reason": "news 신호 없음(기사 0건 또는 전부 정규화 실패)",
            })
            continue
        relevance_ok, fresh_ok = _passes(meta)
        final = relevance_ok and fresh_ok
        if not relevance_ok:
            reason = "relevance gate 탈락(high_relevance_count<2 및 quality_cluster_size<2)"
        elif not fresh_ok:
            reason = "fresh gate 탈락(고관련 기사는 있으나 전부 72h 초과)"
        else:
            reason = "gate 통과(same-issue merge/backfill 순서에 따라 최종 채택 여부 갈림)"
        rows.append({
            "keyword": kw, "source": srcs,
            "articles": len(meta.get("articles") or []),
            "hrc": meta.get("high_relevance_count", 0),
            "qcs": meta.get("quality_cluster_size", 0),
            "fresh_hrc": meta.get("fresh_high_relevance_count", 0),
            "fresh_qcs": meta.get("fresh_quality_cluster_size", 0),
            "latest_age": meta.get("latest_relevant_age_hours"),
            "relevance_ok": relevance_ok, "fresh_ok": fresh_ok,
            "final": final, "reason": reason,
        })

    print("===== 후보별 gate 진단 (DB write 없음) =====")
    header = (
        f"{'keyword':<20}{'source':<12}{'art':>4}{'hrc':>4}{'qcs':>4}"
        f"{'f_hrc':>6}{'f_qcs':>6}{'age_h':>8}{'rel_ok':>7}{'fresh_ok':>9}{'final':>7}  reason"
    )
    print(header)
    for r in rows:
        age = f"{r['latest_age']:.1f}" if isinstance(r["latest_age"], (int, float)) else "-"
        print(
            f"{r['keyword']:<20}{r['source']:<12}{r['articles']:>4}{r['hrc']:>4}{r['qcs']:>4}"
            f"{r['fresh_hrc']:>6}{r['fresh_qcs']:>6}{age:>8}{str(r['relevance_ok']):>7}"
            f"{str(r['fresh_ok']):>9}{str(r['final']):>7}  {r['reason']}"
        )
    print("===== END =====")

    gate_pass = sum(1 for r in rows if r["final"])
    relevance_drop = sum(1 for r in rows if not r["relevance_ok"])
    fresh_drop = sum(1 for r in rows if r["relevance_ok"] and not r["fresh_ok"])
    no_news = sum(1 for r in rows if r["articles"] == 0)
    logger.info(
        "요약: 후보 %d개 중 gate 통과 %d개 / relevance 탈락 %d개 / fresh 탈락 %d개 / news 신호 없음 %d개",
        len(rows), gate_pass, relevance_drop, fresh_drop, no_news,
    )

    # 실제 랭킹 파이프라인(quality/fresh gate + score + dedupe/merge)까지 그대로 실행해
    # merge로 추가로 줄어드는 개수도 함께 확인한다(단 upsert는 하지 않음).
    kw_list = [c["keyword"] for c in candidates]
    daum_signals = {c["keyword"]: c["sources"].get("daum") for c in candidates}
    signals = {"news": news_signals, "datalab": {}, "google": {}, "daum": daum_signals}
    scored = ranker.compute_scores(candidates, signals)
    logger.info("compute_scores 통과(gate+score) 후보 수: %d", len(scored))
    merged = ranker.dedupe_and_merge(scored)
    logger.info("dedupe_and_merge 후 후보 수: %d", len(merged))
    top = ranker.select_top(merged)
    logger.info("최종 select_top 개수: %d / keywords=%s", len(top), [t["keyword"] for t in top])

    # aux 파생 단계(daum 상위 5개)와 신호 산출 단계(전체 후보) 둘 다 fetch_news를 호출하므로
    # 겹치는 keyword는 API가 두 번 호출된다(운영 main.py도 동일 구조 — 캐싱 없음).
    logger.info("Naver News API 호출 횟수(운영과 동일한 캐싱 없는 구조, 중복 keyword 재호출 포함): %d", _call_count)


if __name__ == "__main__":
    run()
