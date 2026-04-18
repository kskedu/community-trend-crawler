import logging
from datetime import datetime
from typing import List
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper
from models import Post
from config import MAX_POSTS_PER_SITE

logger = logging.getLogger(__name__)
BASE_URL = "https://www.82cook.com"


class Cook82Scraper(BaseScraper):
    site_id = "82cook"

    def scrape(self) -> List[Post]:
        posts = []
        try:
            html = self.fetch(BASE_URL)
            soup = BeautifulSoup(html, "html.parser")

            # 메인 페이지 "최근 많이 읽은 글" 섹션
            best_box = soup.select_one("div.leftbox.Best ul.most")
            if not best_box:
                logger.warning("[82cook] best 섹션을 찾지 못했습니다")
                return posts

            for a in best_box.select("a[href*='read.php']")[:MAX_POSTS_PER_SITE]:
                title = a.get("title") or a.get_text(strip=True)
                if not title or len(title) < 3:
                    continue

                href = a.get("href", "")
                url = href if href.startswith("http") else BASE_URL + href

                posts.append(Post(
                    title=title,
                    source_url=url,
                    source_site=self.site_id,
                    image_url=None,
                    upvotes=0,
                    comments=0,
                    views=0,
                    created_at=datetime.now(),
                ))

        except Exception as e:
            logger.error(f"[82cook] 스크래핑 실패: {e}")

        return posts
