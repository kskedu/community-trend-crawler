"""실시간 이슈 브리핑 dry-run (P0-1).

운영 main.py run() 과 분리된 별도 진입점.
- 네이버 실호출 없음 (fixture 사용).
- 실제 DB write 없음 (stdout 출력만, upsert 미호출).
- search_news는 import/호출하지 않음 — fixture만 사용하므로 NAVER env 유무와 무관하게 실호출 0.
  (env-skip + WARNING 동작 자체의 검증은 P0-2 별도 승인 후 별도 경로로 진행)

사용:
  python -m news.dryrun            # fixture 기반 (실 네이버/실 DB 미사용)
  python -m news.dryrun --live-seed  # seed만 실 keyword_cache(daum) read 시도 (write 없음)

검증 포인트:
- XSS/악성 URL fixture가 정규화에서 드롭되는지
- 뉴스 빈 키워드가 seed_only로 노출되는지
- trend가 null 고정인지
- upsert가 호출되지 않는지 (이 스크립트는 upsert를 import하지도 않음)
"""
import argparse
import json
import logging
import sys
from pathlib import Path

from news.builder import build_issues, build_ranked_issues
from news.seed import fetch_daum_seed, seed_from_fixture, ranked_seed_from_fixture
from news import candidates as cand
from news import datalab as datalab_adapter
from news import ranker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("news.dryrun")

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def run(live_seed: bool = False) -> dict:
    # 1) seed 키워드
    if live_seed:
        logger.info("seed: 실 keyword_cache(daum) read-only 조회 시도")
        seed, is_fresh = fetch_daum_seed()
        logger.info("seed freshness(참고용, dry-run은 upsert 안 함): %s", is_fresh)
    else:
        seed = seed_from_fixture(_load_fixture("seed.json"))
    logger.info("seed 키워드 %d개: %s", len(seed), seed)

    if not seed:
        logger.warning("seed 비어있음 → 브리핑 키워드 없음 (프론트는 빈 상태로 숨김)")
        return {"keywords": []}

    # 2) 키워드별 뉴스 fetch — fixture 전용.
    # ⚠️ P0-1 dry-run은 어떤 환경에서도 네이버 실 API를 호출하지 않는다.
    #    (NAVER_CLIENT_ID/SECRET이 실제 환경에 있어도 실호출 금지)
    #    → search_news를 호출하지 않고 fixture만 사용. 실호출/env-skip 검증은
    #      P0-2 별도 승인 후 진행한다. (--allow-live 옵션도 P0-1엔 두지 않음)
    news_fixture = _load_fixture("naver_news.json")

    def fetch_news(keyword: str):
        entry = news_fixture.get(keyword) or {}
        return entry.get("items", [])

    issues = build_issues(seed, fetch_news)

    # 3) 결과 stdout 출력만. DB write 없음 (upsert 미호출).
    print("===== DRY-RUN issues (NO DB WRITE) =====")
    print(json.dumps(issues, ensure_ascii=False, indent=2))
    print("===== END =====")

    # 간단 검증 요약
    for k in issues["keywords"]:
        logger.info(
            "[%s] rank=%s news=%s type=%s articles=%d trend=%s",
            k["keyword"], k["rank"], k["signals"]["news"],
            k["summary_type"], len(k["articles"]), k["trend"],
        )
    return issues


def _inject_recent(items):
    """fixture items의 pubDate를 '지금 기준 1~N시간 전'으로 재주입.

    fixture 시각이 과거여서 최근성 가드(RECENT_HOURS)에 안 걸리는 문제 회피용.
    유효 URL이 없는 악성 item은 그대로 둔다(드롭 검증 유지).
    """
    from datetime import datetime, timezone, timedelta
    out = []
    now = datetime.now(timezone.utc)
    for idx, raw in enumerate(items or []):
        new = dict(raw)
        new["pubDate"] = (now - timedelta(hours=idx + 1)).strftime("%a, %d %b %Y %H:%M:%S +0000")
        out.append(new)
    return out


def run_ranking(verbose: bool = True) -> dict:
    """통합 랭킹 dry-run (fixture 전용, 실호출/DB write 0).

    multi-source fixture로 후보 수집 → 신호 → ranker → Top10 → issues 조립.
    Daum 순서와 다른 Top10이 나오는지 확인하는 게 핵심 합격 기준.
    """
    seed_fx = _load_fixture("seed.json")
    danawa_fx = _load_fixture("danawa_seed.json")
    news_fx = _load_fixture("naver_news.json")
    datalab_fx = _load_fixture("datalab.json")

    daum_ranked = ranked_seed_from_fixture(seed_fx)
    danawa_ranked = ranked_seed_from_fixture(danawa_fx)

    def fetch_news(keyword: str):
        # fixture pubDate를 '지금 기준 최근'으로 재주입(최근성 가드 데모용).
        entry = news_fx.get(keyword) or {}
        return _inject_recent(entry.get("items", []))

    # Google stub → 후보/신호 없음
    aux = cand.derive_aux_keywords(daum_ranked, fetch_news)
    candidates = cand.collect_candidates(daum_ranked, danawa_ranked, [], aux)
    news_signals = cand.build_news_signals(candidates, fetch_news)
    kw_list = [c["keyword"] for c in candidates]
    datalab_signals = datalab_adapter.fetch_from_fixture(datalab_fx, kw_list)
    daum_signals = {c["keyword"]: c["sources"].get("daum") for c in candidates}

    signals = {
        "news": news_signals,
        "datalab": datalab_signals,
        "google": {},  # stub
        "daum": daum_signals,
    }
    # production(_rank_and_select)과 동일 시퀀스로 랭킹을 산출한다 — dry-run 검증 경로가
    # 실제 파이프라인과 어긋나지 않도록(Codex diff 리뷰 P2). PR hard exclude → dedupe/merge →
    # display/articles invariant → generic singleton 제외 → Top10.
    ranked = ranker.compute_scores(candidates, signals)
    ranked, _pr_excluded = ranker.exclude_pr_clusters(ranked)
    merged = ranker.dedupe_and_merge(ranked)
    merged = ranker.enforce_display_article_consistency(merged)
    kept, _generic_excluded = ranker.exclude_generic_singletons(merged)
    top = ranker.select_top(kept)
    candidate_map = {c["keyword"]: c for c in candidates}
    data_sources = ["naver_news"]
    if datalab_signals:
        data_sources.append("datalab")
    if any(s is not None for s in daum_signals.values()):
        data_sources.append("daum")
    issues = build_ranked_issues(top, candidate_map, data_sources)

    if verbose:
        print("===== DRY-RUN ranked issues (NO DB WRITE) =====")
        print(json.dumps(issues, ensure_ascii=False, indent=2))
        print("===== END =====")
        daum_order = [i["keyword"] for i in daum_ranked]
        ranked_order = [k["keyword"] for k in issues["keywords"]]
        logger.info("daum 순서: %s", daum_order)
        logger.info("ranked 순서: %s", ranked_order)
        logger.info("Daum 순서와 동일? %s", daum_order[:len(ranked_order)] == ranked_order)
    return issues


def main():
    parser = argparse.ArgumentParser(description="실시간 이슈 브리핑 dry-run (DB write 없음)")
    parser.add_argument("--live-seed", action="store_true",
                        help="seed만 실 keyword_cache(daum) read 시도 (write 없음)")
    parser.add_argument("--ranking", action="store_true",
                        help="통합 랭킹 dry-run (fixture 기반, 실호출/DB write 없음)")
    args = parser.parse_args()
    if args.ranking:
        run_ranking()
    else:
        run(live_seed=args.live_seed)


if __name__ == "__main__":
    main()
