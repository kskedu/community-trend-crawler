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

from news.builder import build_issues
from news.seed import fetch_daum_seed, seed_from_fixture

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
        seed = fetch_daum_seed()
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


def main():
    parser = argparse.ArgumentParser(description="실시간 이슈 브리핑 dry-run (DB write 없음)")
    parser.add_argument("--live-seed", action="store_true",
                        help="seed만 실 keyword_cache(daum) read 시도 (write 없음)")
    args = parser.parse_args()
    run(live_seed=args.live_seed)


if __name__ == "__main__":
    main()
