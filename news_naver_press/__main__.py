"""네이버 언론사별 인기뉴스 수집 독립 entrypoint (Phase 1).

실행: python -m news_naver_press

기존 main.py(커뮤니티/키워드/news_top) 파이프라인과 완전히 분리된 별도 진입점.
실패해도 기존 crawl.yml 워크플로우/스케줄에는 영향이 없다.
"""
import logging
import sys

from news_naver_press.collector import run_naver_press_popular_collection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("news_naver_press")


def main() -> int:
    outcome = run_naver_press_popular_collection()
    logger.info(
        "[naver_press] run finished status=%s reason=%s http_status=%s "
        "press_count=%d valid_item_count=%d payload_bytes=%d duration=%.2fs "
        "previous_cache_preserved=%s",
        outcome.status, outcome.reason, outcome.http_status,
        outcome.press_count, outcome.valid_item_count, outcome.payload_bytes,
        outcome.duration_seconds, outcome.previous_cache_preserved,
    )
    if outcome.status != "success":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
