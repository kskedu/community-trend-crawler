import logging
from datetime import datetime
from typing import List
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper
from models import Post
from config import MAX_POSTS_PER_SITE

logger = logging.getLogger(__name__)
BASE_URL = "https://www.clien.net"


class ClienScraper(BaseScraper):
    site_id = "clien"

    def scrape(self) -> List[Post]:
        posts = []
        try:
            html = self.fetch(f"{BASE_URL}/service/group/clien_all?&od=T33")
            soup = BeautifulSoup(html, "html.parser")

            for item in soup.select(".list_item.symph_row")[:MAX_POSTS_PER_SITE]:
                title_el = item.select_one(".list_subject span.subject_fixed")
                if not title_el:
                    continue

                title = title_el.get_text(strip=True)
                link_el = item.select_one("a.list_subject")
                href = link_el.get("href", "") if link_el else ""
                url = href if href.startswith("http") else BASE_URL + href

                img_el = item.select_one("img.list_img")
                image_url = img_el.get("src") if img_el else None

                upvotes = self._int(item.select_one(".list_recommend"))
                comments = self._int(item.select_one(".list_reply .rSymph"))
                views = self._int(item.select_one(".list_hit"))

                posts.append(Post(
                    title=title,
                    source_url=url,
                    source_site=self.site_id,
                    image_url=image_url,
                    upvotes=upvotes,
                    comments=comments,
                    views=views,
                    created_at=datetime.now(),
                ))

        except Exception as e:
            logger.error(f"[clien] 스크래핑 실패: {e}")

        return posts

    def _int(self, el) -> int:
        if not el:
            return 0
        try:
            return int(el.get_text(strip=True).replace(",", ""))
        except ValueError:
            return 0
