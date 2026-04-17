import logging
from datetime import datetime
from typing import List
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper
from models import Post
from config import MAX_POSTS_PER_SITE

logger = logging.getLogger(__name__)
BASE_URL = "https://www.ddanzi.com"


class DdanziScraper(BaseScraper):
    site_id = "ddanzi"

    def scrape(self) -> List[Post]:
        posts = []
        try:
            html = self.fetch(f"{BASE_URL}/free")
            soup = BeautifulSoup(html, "html.parser")

            for a in soup.select('a[href*="ddanzi.com/free/"]'):
                title = a.get_text(strip=True)
                if not title or len(title) < 5:
                    continue

                url = a.get("href", "")

                row = a.find_parent("tr") or a.parent
                upvotes = self._int(row.select_one(".recom, .vote, .good") if row else None)
                comments = self._int(row.select_one(".replyNum, .reply") if row else None)
                views = self._int(row.select_one(".hit, .readedCount") if row else None)

                posts.append(Post(
                    title=title,
                    source_url=url,
                    source_site=self.site_id,
                    image_url=None,
                    upvotes=upvotes,
                    comments=comments,
                    views=views,
                    created_at=datetime.now(),
                ))
                if len(posts) >= MAX_POSTS_PER_SITE:
                    break

        except Exception as e:
            logger.error(f"[ddanzi] 스크래핑 실패: {e}")

        return posts

    def _int(self, el) -> int:
        if not el:
            return 0
        try:
            return int(el.get_text(strip=True).replace(",", ""))
        except ValueError:
            return 0
