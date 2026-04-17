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
            html = self.fetch(f"{BASE_URL}/entiz/best_article.php")
            soup = BeautifulSoup(html, "html.parser")

            for item in soup.select("ul.list_area li, tr.list")[:MAX_POSTS_PER_SITE]:
                title_el = item.select_one("a.tit_link, .subject a, td.title a")
                if not title_el:
                    continue

                title = title_el.get_text(strip=True)
                href = title_el.get("href", "")
                url = href if href.startswith("http") else BASE_URL + href

                upvotes = self._int(item.select_one(".recom, .good"))
                comments = self._int(item.select_one(".reply"))
                views = self._int(item.select_one(".hit"))

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

        except Exception as e:
            logger.error(f"[82cook] 스크래핑 실패: {e}")

        return posts

    def _int(self, el) -> int:
        if not el:
            return 0
        try:
            return int(el.get_text(strip=True).replace(",", ""))
        except ValueError:
            return 0
