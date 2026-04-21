"""
커뮤니티 트렌드 크롤러 진입점
GitHub Actions에서 주기적으로 실행됨
"""
import logging
import sys
from scrapers.clien import ClienScraper
from scrapers.ruliweb import RuliwebScraper
from scrapers.ppomppu import PpomppuScraper
from scrapers.mlbpark import MlbparkScraper
from scrapers.bobaedream import BobaedreamScraper
from scrapers.inven import InvenScraper
from scrapers.dcinside import DcinsideScraper
from scrapers.humoruniv import HumorunivScraper
from scrapers.cook82 import Cook82Scraper
from scrapers.fmkorea import FmkoreaScraper
from scrapers.theqoo import TheqooScraper
from scrapers.slrclub import SlrclubScraper
from scrapers.todayhumor import TodayhumorScraper
from scrapers.etoland import EtolandScraper
from scrapers.instiz import InstizScraper
from scrapers.ygosu import YgosuScraper
from scrapers.natepann import NatepannScraper
from keywords.danawa import DanawaKeywordScraper
from keywords.daum import DaumKeywordScraper
from keywords.namuwiki import NamuwikiKeywordScraper
from processor.dedup import dedup
from processor.filter import filter_notices
from processor.scorer import score_all
from db.supabase import upsert_posts, upsert_keywords

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# 활성화된 스크래퍼 목록
SCRAPERS = [
    ClienScraper(),
    RuliwebScraper(),
    PpomppuScraper(),
    MlbparkScraper(),
    BobaedreamScraper(),
    InvenScraper(),
    DcinsideScraper(),
    HumorunivScraper(),
    Cook82Scraper(),
    # FmkoreaScraper(),  # 봇 차단 (430) - 비활성화
    # DdanziScraper(),  # 제거
    TheqooScraper(),
    SlrclubScraper(),
    TodayhumorScraper(),
    EtolandScraper(),
    InstizScraper(),
    YgosuScraper(),
    NatepannScraper(),
]

# 키워드 스크래퍼 (검색엔진 실시간 키워드 → keyword_cache)
KEYWORD_SCRAPERS = [
    DanawaKeywordScraper(),
    DaumKeywordScraper(),
    NamuwikiKeywordScraper(),
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

    # 공지/안내 필터
    all_posts = filter_notices(all_posts)
    logger.info(f"공지 필터 후: {len(all_posts)}건")

    # 점수 계산
    all_posts = score_all(all_posts)

    # DB 저장
    saved = upsert_posts(all_posts)
    logger.info(f"저장 완료: {saved}건")

    # 검색엔진 키워드 수집
    for ks in KEYWORD_SCRAPERS:
        logger.info(f"[{ks.source}] 키워드 크롤링 시작")
        try:
            items = ks.scrape()
            if upsert_keywords(ks.source, items):
                logger.info(f"[{ks.source}] 키워드 {len(items)}개 저장")
            else:
                logger.warning(f"[{ks.source}] 키워드 저장 실패")
        except Exception as e:
            logger.error(f"[{ks.source}] 키워드 실패: {e}")


if __name__ == "__main__":
    run()
