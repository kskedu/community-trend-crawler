import logging
from datetime import datetime
from typing import List
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper
from models import Post
from config import MAX_POSTS_PER_SITE

logger = logging.getLogger(__name__)
BASE_URL = "https://www.ppomppu.co.kr"


class PpomppuScraper(BaseScraper):
    site_id = "ppomppu"

    def scrape(self) -> List[Post]:
        posts = []
        try:
            html = self.fetch(f"{BASE_URL}/hot.php")
            soup = BeautifulSoup(html, "html.parser")

            for item in soup.select("tr.baseList, tr.bbs_new")[:MAX_POSTS_PER_SITE]:
                title_el = item.select_one("a.baseList-title, .baseList-title a, td.baseList-title a")
                if not title_el:
                    continue

                title = title_el.get_text(strip=True)
                href = title_el.get("href", "")
                url = href if href.startswith("http") else BASE_URL + href

                # 썸네일 (//img.ppomppu.co.kr/... 형태)
                thumb = item.select_one("a.baseList-thumb img")
                image_url = None
                if thumb:
                    src = thumb.get("src", "")
                    image_url = ("https:" + src) if src.startswith("//") else src or None

                # 댓글수: span.list_comment2
                comments = self._int(item.select_one("span.list_comment2"))

                # board_date td들: [날짜, 추천-비추천, 조회수]
                board_dates = item.select("td.board_date")
                views = self._int(board_dates[-1]) if board_dates else 0
                upvotes = 0
                if len(board_dates) >= 2:
                    raw_rec = board_dates[-2].get_text(strip=True).split("-")[0].strip()
                    try:
                        upvotes = int(raw_rec.replace(",", ""))
                    except ValueError:
                        upvotes = 0

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
            logger.error(f"[ppomppu] 스크래핑 실패: {e}")

        return posts

    def _int(self, el) -> int:
        if not el:
            return 0
        try:
            return int(el.get_text(strip=True).replace(",", ""))
        except ValueError:
            return 0
