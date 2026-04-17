import logging
from datetime import datetime
from typing import List
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper
from models import Post
from config import MAX_POSTS_PER_SITE

logger = logging.getLogger(__name__)
BASE_URL = "https://gall.dcinside.com"


class DcinsideScraper(BaseScraper):
    site_id = "dcinside"

    def scrape(self) -> List[Post]:
        posts = []
        try:
            # 디시 실시간 베스트 (인기 마이너갤 포함)
            html = self.fetch(f"{BASE_URL}/board/lists/?id=dcbest")
            soup = BeautifulSoup(html, "html.parser")

            for item in soup.select("tr.ub-content")[:MAX_POSTS_PER_SITE]:
                title_el = item.select_one("td.gall_tit a:not(.reply_num)")
                if not title_el:
                    continue

                title = title_el.get_text(strip=True)
                href = title_el.get("href", "")
                url = href if href.startswith("http") else BASE_URL + href

                upvotes = self._int(item.select_one("td.gall_recommend"))
                comments_el = item.select_one("a.reply_num")
                comments = self._int(comments_el)
                views = self._int(item.select_one("td.gall_count"))

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
            logger.error(f"[dcinside] 스크래핑 실패: {e}")

        return posts

    def _int(self, el) -> int:
        if not el:
            return 0
        try:
            text = el.get_text(strip=True).strip("[]").replace(",", "")
            return int(text)
        except ValueError:
            return 0
