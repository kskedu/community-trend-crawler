"""
커뮤니티 트렌드 크롤러 진입점
GitHub Actions에서 주기적으로 실행됨
"""
import logging
import sys
from scrapers.fmkorea import FmkoreaScraper
from scrapers.clien import ClienScraper
from scrapers.ruliweb import RuliwebScraper
from scrapers.ppomppu import PpomppuScraper
from processor.dedup import dedup
from processor.scorer import score_all
from db.supabase import upsert_posts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# 활성화된 스크래퍼 목록
SCRAPERS = [
    FmkoreaScraper(),
    ClienScraper(),
    RuliwebScraper(),
    PpomppuScraper(),
    # 추후 추가: MlbparkScraper, BobaedreamScraper, InvenScraper, ...
]


def run():
    all_posts = []

    for scraper in SCRAPERS:
        logger.info(f"[{scraper.site_id}] 크롤링 시작")
        try:
            posts = scraper.scrape()
            logger.info(f"[{scraper.site_id}] {len(posts)}건 수집")
            all_posts.extend(posts)
        except Exception as e:
            logger.error(f"[{scraper.site_id}] 실패: {e}")

    logger.info(f"총 수집: {len(all_posts)}건")

    # 중복 제거
    all_posts = dedup(all_posts)
    logger.info(f"중복 제거 후: {len(all_posts)}건")

    # 점수 계산
    all_posts = score_all(all_posts)

    # DB 저장
    saved = upsert_posts(all_posts)
    logger.info(f"저장 완료: {saved}건")


if __name__ == "__main__":
    run()
