"""dedupe_and_merge 단계 상세 추적 진단 (read-only, DB write 없음).

목적: news_top gate 통과 후보가 왜 절반 이상 merge에 흡수되는지 쌍 단위로
추적한다. news/diag_live.py(이전 진단, 이미 삭제됨)의 후속 — 이번엔 merge
판정 근거(similar_keyword vs same-issue evidence)까지 계측한다.

안전 계약(news/diag_live.py와 동일):
- Supabase read(keyword_cache)만 수행 — write/upsert 함수는 import하지 않는다.
- Naver News API read-only 호출만 수행(search_news).
- main.py의 run()/run_news_briefing()은 호출하지 않는다.
- news.ranker의 내부 함수(_is_similar_keyword, _is_same_issue 등)를 그대로
  재사용해 계측만 추가한다 — 운영 ranker.py는 수정하지 않는다.
- 결과는 stdout 표 출력만.

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
logger = logging.getLogger("news.diag_merge")

_call_count = 0


def _counted_search_news(keyword: str):
    global _call_count
    _call_count += 1
    return search_news(keyword)


def _gate_row(c, news_signals):
    kw = c["keyword"]
    meta = news_signals.get(kw)
    srcs = "+".join(sorted(c["sources"].keys()))
    if meta is None:
        return {
            "keyword": kw, "source": srcs, "hrc": 0, "qcs": 0, "fresh_hrc": 0,
            "age": None, "relevance_ok": False, "fresh_ok": False, "gate_ok": False,
        }
    hrc = meta.get("high_relevance_count", 0)
    qcs = meta.get("quality_cluster_size", 0)
    relevance_ok = hrc >= 2 or qcs >= 2
    fresh_ok = meta.get("fresh_high_relevance_count", 0) >= 1
    return {
        "keyword": kw, "source": srcs, "hrc": hrc, "qcs": qcs,
        "fresh_hrc": meta.get("fresh_high_relevance_count", 0),
        "age": meta.get("latest_relevant_age_hours"),
        "relevance_ok": relevance_ok, "fresh_ok": fresh_ok,
        "gate_ok": relevance_ok and fresh_ok,
    }


def _trace_merge(scored):
    """ranker.dedupe_and_merge()와 동일한 selected-set 누적 루프를 재현하면서
    각 흡수 판정의 근거(similar_keyword 조건 / same-issue evidence)를 기록한다.
    """
    trace = []
    consumed = set()

    for i, item in enumerate(scored):
        kw = item["keyword"]
        if kw in consumed:
            continue
        group = [item]
        consumed.add(kw)

        # 1) dedupe: ranker.dedupe_and_merge()와 동일하게 "각 other에 대해 group 내
        #    아무 멤버와나 유사하면 흡수"를 fixed-point까지 반복(첫 매치에서 other
        #    순회를 멈추지 않음 — 원본과 동일하게 전체를 훑어야 누락이 없다).
        changed = True
        while changed:
            changed = False
            for other in scored[i + 1:]:
                okw = other["keyword"]
                if okw in consumed:
                    continue
                match = next((m for m in group if ranker._is_similar_keyword(m["keyword"], okw)), None)
                if match:
                    trace.append({
                        "absorbed": okw, "representative": kw,
                        "merge_reason": "similar_keyword",
                        "evidence": f"_is_similar_keyword('{match['keyword']}', '{okw}')=True",
                    })
                    group.append(other)
                    consumed.add(okw)
                    changed = True

        # 2) same-issue merge: 동일하게 group 전체와 개별 비교(OR), fixed-point까지 반복.
        changed = True
        while changed:
            changed = False
            for other in scored[i + 1:]:
                okw = other["keyword"]
                if okw in consumed:
                    continue
                matched_m = None
                matched_evidence = None
                for m in group:
                    art_a = (m.get("news_meta") or {}).get("articles") or []
                    art_b = (other.get("news_meta") or {}).get("articles") or []
                    art_overlap = ranker._article_overlap(art_a, art_b)
                    if art_overlap >= ranker.MERGE_ARTICLE_OVERLAP_THRESHOLD:
                        matched_m = m
                        matched_evidence = f"article_overlap({m['keyword']},{okw})={art_overlap:.2f} >= {ranker.MERGE_ARTICLE_OVERLAP_THRESHOLD}"
                        break
                    if ranker._is_same_issue(m, other):
                        shared = ranker._representative_overlap(m, other)
                        has_anchor = ranker._has_cross_keyword_anchor(m, other, shared)
                        matched_m = m
                        matched_evidence = (
                            f"shared_event_tokens({m['keyword']},{okw})={sorted(shared)} "
                            f"has_cross_keyword_anchor={has_anchor} article_overlap={art_overlap:.2f}"
                        )
                        break
                if matched_m:
                    trace.append({
                        "absorbed": okw, "representative": kw,
                        "merge_reason": "same_article_cluster",
                        "evidence": matched_evidence,
                    })
                    group.append(other)
                    consumed.add(okw)
                    changed = True
    return trace


def run():
    daum_ranked, daum_fresh = fetch_ranked_seed("daum")
    danawa_ranked, _ = fetch_ranked_seed("danawa")
    logger.info("daum seed(%d, fresh=%s): %s", len(daum_ranked), daum_fresh, [c["keyword"] for c in daum_ranked])
    logger.info("danawa seed(%d): %s", len(danawa_ranked), [c["keyword"] for c in danawa_ranked])
    if not daum_fresh:
        daum_ranked = []

    aux = cand.derive_aux_keywords(daum_ranked, _counted_search_news)
    logger.info("aux 후보(%d): %s", len(aux), aux)
    logger.info("google 후보: stub(비활성) → 0개")

    candidates = cand.collect_candidates(daum_ranked, danawa_ranked, [], aux)
    logger.info("전체 후보 pool(%d, CANDIDATE_MAX=%d)", len(candidates), cand.CANDIDATE_MAX)

    news_signals = cand.build_news_signals(candidates, _counted_search_news)

    print("===== 표1. 후보별 gate 결과 =====")
    print(f"{'keyword':<20}{'source':<10}{'hrc':>4}{'qcs':>4}{'f_hrc':>6}{'age_h':>8}{'rel_ok':>7}{'fresh_ok':>9}{'gate_ok':>8}")
    gate_rows = []
    for c in candidates:
        row = _gate_row(c, news_signals)
        gate_rows.append(row)
        age = f"{row['age']:.1f}" if isinstance(row["age"], (int, float)) else "-"
        print(f"{row['keyword']:<20}{row['source']:<10}{row['hrc']:>4}{row['qcs']:>4}{row['fresh_hrc']:>6}{age:>8}{str(row['relevance_ok']):>7}{str(row['fresh_ok']):>9}{str(row['gate_ok']):>8}")
    print("===== END 표1 =====")

    kw_list = [c["keyword"] for c in candidates]
    daum_signals = {c["keyword"]: c["sources"].get("daum") for c in candidates}
    signals = {"news": news_signals, "datalab": {}, "google": {}, "daum": daum_signals}
    scored = ranker.compute_scores(candidates, signals)
    scored_keywords_before_merge = [s["keyword"] for s in scored]
    logger.info("merge 전(gate+score 통과) 후보(%d): %s", len(scored), scored_keywords_before_merge)

    trace = _trace_merge(scored)
    merged = ranker.dedupe_and_merge(scored)
    final_keywords = [m["keyword"] for m in merged]
    logger.info("merge 후 최종 후보(%d): %s", len(merged), final_keywords)

    absorbed = [t["absorbed"] for t in trace]
    logger.info("흡수된 후보(%d): %s", len(absorbed), absorbed)

    # invariant check: trace로 재현한 흡수 집합이 실제 dedupe_and_merge() 결과의
    # related_keywords 합집합과 일치하는지 확인(재현 로직 drift 방지, Codex P3 반영).
    actual_absorbed = set()
    for m in merged:
        actual_absorbed.update(m.get("related_keywords") or [])
    trace_absorbed = set(absorbed)
    if trace_absorbed == actual_absorbed:
        logger.info("invariant check: trace 흡수 집합과 실제 dedupe_and_merge 결과 일치 확인")
    else:
        logger.warning(
            "invariant check 불일치 → trace만 있음=%s / 실제만 있음=%s (아래 표는 참고용, 실제는 select_top 로그 신뢰)",
            trace_absorbed - actual_absorbed, actual_absorbed - trace_absorbed,
        )

    print("===== 표2. merge trace =====")
    print(f"{'absorbed':<20}{'representative':<20}{'merge_reason':<22}evidence")
    for t in trace:
        print(f"{t['absorbed']:<20}{t['representative']:<20}{t['merge_reason']:<22}{t['evidence']}")
    print("===== END 표2 =====")

    watch = ["월드컵", "16강", "재검표", "뉴진스", "통영시장", "통영 시장"]
    print("===== 확인 대상 후보 상태 =====")
    for w in watch:
        if w in final_keywords:
            print(f"{w}: 최종 채택됨(대표 keyword)")
        else:
            hit = [t for t in trace if t["absorbed"] == w]
            if hit:
                print(f"{w}: 흡수됨 → {hit[0]['representative']} (reason={hit[0]['merge_reason']}, evidence={hit[0]['evidence']})")
            elif w in scored_keywords_before_merge:
                print(f"{w}: merge 전 목록엔 있으나 trace에 없음(재현 로직 확인 필요)")
            elif w in kw_list:
                print(f"{w}: 후보 pool엔 있었으나 gate 자체를 통과 못함")
            else:
                print(f"{w}: 이번 실행에서 후보 pool에 아예 없음(seed/aux 변동)")
    print("===== END 확인 대상 =====")

    top = ranker.select_top(merged)
    logger.info("select_top 최종(%d): %s", len(top), [t["keyword"] for t in top])
    logger.info("Naver News API 호출 횟수: %d", _call_count)


if __name__ == "__main__":
    run()
