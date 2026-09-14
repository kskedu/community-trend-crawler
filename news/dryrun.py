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
from typing import List

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


def ranking_inputs() -> dict:
    """fixture → 랭킹 입력(candidates / signals / daum 순서). **dry-run 고유 adapter**.

    선정 게이트는 이 함수에 없다 — 게이트 시퀀스는 ranker.run_selection_stages 단일
    진실원이 전부 담당한다. dry-run 이 맡는 것은 "무엇을 입력으로 넣을지"와 "결과를
    어떻게 보여줄지"뿐이다.

    반환: {candidates, signals, datalab_signals, daum_ranked}
    """
    seed_fx = _load_fixture("seed.json")
    danawa_fx = _load_fixture("danawa_seed.json")
    news_fx = _load_fixture("naver_news.json")
    datalab_fx = _load_fixture("datalab.json")

    daum_ranked = ranked_seed_from_fixture(seed_fx)
    # 두 번째 fixture를 nate_home으로 재활용(다양성 family 데모 — Danawa는 news_top에서 제거됨).
    nate_ranked = ranked_seed_from_fixture(danawa_fx)

    def fetch_news(keyword: str):
        # fixture pubDate를 '지금 기준 최근'으로 재주입(최근성 가드 데모용).
        entry = news_fx.get(keyword) or {}
        return _inject_recent(entry.get("items", []))

    aux = cand.derive_aux_keywords(daum_ranked, fetch_news)
    seed_sources = {"daum_home": daum_ranked, "nate_home": nate_ranked}
    candidates = cand.collect_candidates(seed_sources, aux)
    news_signals = cand.build_news_signals(candidates, fetch_news)
    kw_list = [c["keyword"] for c in candidates]
    datalab_signals = datalab_adapter.fetch_from_fixture(datalab_fx, kw_list)

    return {
        "candidates": candidates,
        "signals": {
            "news": news_signals,
            "datalab": datalab_signals,
            "google": {},  # provider 기본 비활성
        },
        "datalab_signals": datalab_signals,
        "daum_ranked": daum_ranked,
    }


def _data_sources(candidates: List[dict], datalab_signals: dict) -> List[str]:
    """issues payload 에 실을 data_sources 목록(표시 전용)."""
    out = ["naver_news"]
    if datalab_signals:
        out.append("datalab")
    participating = set()
    for c in candidates:
        participating |= set(c["sources"].keys()) & cand._INDEPENDENT_SEARCH_FAMILIES
    out.extend(sorted(participating))
    return out


def run_ranking(verbose: bool = True) -> dict:
    """통합 랭킹 dry-run (fixture 전용, 실호출/DB write 0).

    multi-source fixture로 후보 수집 → 신호 → **ranker.run_selection_stages** → issues 조립.

    ⚠️ 선정 게이트 시퀀스를 여기에 다시 적지 않는다. 2026-07~09 동안 이 자리에 시퀀스
    사본이 있었고 조용히 drift했다 — enforce_display_source_grounding 과
    exclude_no_representative 두 단계가 누락됐고, exclude_insufficient_display_articles
    가 select_top **뒤**에서 돌아(운영은 앞) 하위 정상후보 backfill 도 일어나지 않았다.
    그 결과 shipped fixture 에서 dry-run 이 production 이라면 전부 탈락시켰을 후보
    2건(summary 가 빈 no_representative)을 Top 으로 내보내고 있었다(2026-09-14 교정).
    게이트 추가/순서 변경은 run_selection_stages 에서만 일어나며 dry-run 은 자동으로 따른다.
    """
    inputs = ranking_inputs()
    candidates = inputs["candidates"]
    daum_ranked = inputs["daum_ranked"]

    stages = ranker.run_selection_stages(candidates, inputs["signals"])
    top = stages["top"]

    candidate_map = {c["keyword"]: c for c in candidates}
    issues = build_ranked_issues(
        top, candidate_map, _data_sources(candidates, inputs["datalab_signals"])
    )

    if verbose:
        print("===== DRY-RUN ranked issues (NO DB WRITE) =====")
        print(json.dumps(issues, ensure_ascii=False, indent=2))
        print("===== END =====")
        # 단계별 제외를 함께 남긴다 — Top 이 비거나 짧을 때 "고장"과 "게이트가 제 역할을
        # 한 것"을 구분할 수 있어야, 사람이 시퀀스를 임의로 줄여 다시 drift 시키지 않는다.
        logger.info(
            "[dry-run] gate통과=%d PR제외=%d merge후=%d generic제외=%d "
            "display부족제외=%d no_rep제외=%d final=%d",
            len(stages["gate_passed"]), len(stages["pr_excluded"]), len(stages["merged"]),
            len(stages["generic_excluded"]), len(stages["display_excluded"]),
            len(stages["no_rep_excluded"]), len(top),
        )
        for stage in ("pr_excluded", "generic_excluded", "display_excluded", "no_rep_excluded"):
            if stages[stage]:
                logger.info("[dry-run] %s: %s", stage, stages[stage])
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
