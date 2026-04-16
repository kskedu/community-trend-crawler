import logging
from datetime import datetime
from typing import List
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper
from models import Post
from config import MAX_POSTS_PER_SITE

logger = logging.getLogger(__name__)
BASE_URL = "https://www.fmkorea.com"


class FmkoreaScraper(BaseScraper):
    site_id = "fmkorea"

    def scrape(self) -> List[Post]:
        posts = []
        try:
            # 봇 차단 우회: 추가 헤더
            self.session.headers.update({
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Referer": "https://www.fmkorea.com/",
                "Upgrade-Insecure-Requests": "1",
            })
            html = self.fetch(f"{BASE_URL}/best")
            soup = BeautifulSoup(html, "html.parser")

            for item in soup.select(".li_best_mini_type_list")[:MAX_POSTS_PER_SITE]:
                title_el = item.select_one(".title a")
                if not title_el:
                    continue

                title = title_el.get_text(strip=True)
                href = title_el.get("href", "")
                url = href if href.startswith("http") else BASE_URL + href

                # 썸네일
                img_el = item.select_one("img")
                image_url = img_el.get("src") if img_el else None

                # 추천/댓글/조회
                upvotes = self._int(item.select_one(".recomend_num"))
                comments = self._int(item.select_one(".comment_count"))
                views = self._int(item.select_one(".hit"))

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
            logger.error(f"[fmkorea] 스크래핑 실패: {e}")

        return posts

    def _int(self, el) -> int:
        if not el:
            return 0
        try:
            return int(el.get_text(strip=True).replace(",", ""))
        except ValueError:
            return 0
