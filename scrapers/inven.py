import re
import logging
from datetime import datetime
from typing import List
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper
from models import Post
from config import MAX_POSTS_PER_SITE

logger = logging.getLogger(__name__)
BASE_URL = "https://www.inven.co.kr"
HOT_URL = "https://hot.inven.co.kr/"


class InvenScraper(BaseScraper):
    site_id = "inven"

    def scrape(self) -> List[Post]:
        posts = []
        try:
            html = self.fetch(HOT_URL)
            soup = BeautifulSoup(html, "html.parser")

            # /board/게임명/숫자/숫자 패턴인 게시글 링크만
            board_pattern = re.compile(r'/board/\w+/\d+/\d+')
            for a in soup.select('a[href*="/board/"]'):
                href = a.get("href", "")
                if not board_pattern.search(href):
                    continue
                # 앞의 순위 숫자 제거
                raw = a.get_text(strip=True)
                title = re.sub(r'^\d+', '', raw).strip()
                if not title or len(title) < 4:
                    continue

                url = href if href.startswith("http") else BASE_URL + href

                item = a.find_parent("li") or a.parent
                comments_match = re.search(r'\[(\d+)\]', raw)
                comments = int(comments_match.group(1)) if comments_match else 0

                posts.append(Post(
                    title=title,
                    source_url=url,
                    source_site=self.site_id,
                    image_url=None,
                    upvotes=0,
                    comments=comments,
                    views=0,
                    created_at=datetime.now(),
                ))
                if len(posts) >= MAX_POSTS_PER_SITE:
                    break

        except Exception as e:
            logger.error(f"[inven] 스크래핑 실패: {e}")

        return posts
