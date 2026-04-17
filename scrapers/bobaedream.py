import logging
from datetime import datetime
from typing import List
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper
from models import Post
from config import MAX_POSTS_PER_SITE

logger = logging.getLogger(__name__)
BASE_URL = "https://www.bobaedream.co.kr"


class BobaedreamScraper(BaseScraper):
    site_id = "bobaedream"

    def scrape(self) -> List[Post]:
        posts = []
        try:
            content = self.fetch_bytes(f"{BASE_URL}/list?code=best")
            soup = BeautifulSoup(content, "html.parser")

            for a in soup.select("a.bsubject")[:MAX_POSTS_PER_SITE]:
                title = a.get("title") or a.get_text(strip=True)
                if not title:
                    continue

                href = a.get("href", "")
                url = href if href.startswith("http") else BASE_URL + href

                row = a.find_parent("tr")
                upvotes = self._int(row.select_one(".recom, .like, .recomm") if row else None)
                comments = self._int(row.select_one(".replyCnt, .replyNum") if row else None)
                views = self._int(row.select_one(".hit") if row else None)

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
            logger.error(f"[bobaedream] 스크래핑 실패: {e}")

        return posts

    def _int(self, el) -> int:
        if not el:
            return 0
        try:
            return int(el.get_text(strip=True).replace(",", ""))
        except ValueError:
            return 0
