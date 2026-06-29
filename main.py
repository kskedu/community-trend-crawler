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
from keywords.daangn import DaangnKeywordScraper
from processor.dedup import dedup
from processor.filter import filter_notices
from processor.scorer import score_all
from db.supabase import upsert_posts, upsert_keywords, upsert_news_issues
from news.seed import fetch_daum_seed
from news.naver_news import search_news
from news.builder import build_issues

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
    DaangnKeywordScraper(),
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

    # 실시간 이슈 브리핑 (P0-2) — 실패해도 위 커뮤니티/키워드 수집 결과에 영향 없도록 격리
    run_news_briefing()


def run_news_briefing():
    """daum seed + 네이버 뉴스로 news_issue_cache(source='news_top') 갱신.

    - daum seed가 비어있거나 stale(2시간 초과)이면 upsert 자체를 skip(기존 캐시 보존).
    - 뉴스가 전부 0건(전 키워드 seed_only)이면 upsert를 skip(기존 캐시 보존).
    - NAVER_CLIENT_ID/SECRET 없으면 search_news가 자동 skip+WARNING, 빈 리스트 반환.
    """
    try:
        seed, is_fresh = fetch_daum_seed()
        if not seed:
            logger.warning("[news] seed 비어있음 → news_top upsert skip")
            return
        if not is_fresh:
            logger.warning("[news] seed stale → news_top upsert skip (기존 캐시 보존)")
            return

        issues = build_issues(seed, search_news)
        has_any_news = any(k["signals"]["news"] for k in issues["keywords"])
        if not has_any_news:
            logger.warning("[news] 전체 키워드 뉴스 0건(seed_only) → news_top upsert skip (기존 캐시 보존)")
            return

        if upsert_news_issues(issues, source="news_top"):
            logger.info("[news] news_top 저장 완료 (%d개 키워드)", len(issues["keywords"]))
        else:
            logger.warning("[news] news_top 저장 실패")
    except Exception as e:
        logger.error(f"[news] 실시간 이슈 브리핑 실패(커뮤니티/키워드 수집에는 영향 없음): {e}")


if __name__ == "__main__":
    run()
