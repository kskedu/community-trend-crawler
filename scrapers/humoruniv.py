import re
import logging
from datetime import datetime
from typing import List
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper
from models import Post
from config import MAX_POSTS_PER_SITE

logger = logging.getLogger(__name__)
BASE_URL = "https://web.humoruniv.com"


class HumorunivScraper(BaseScraper):
    site_id = "humoruniv"

    def scrape(self) -> List[Post]:
        posts = []
        try:
            content = self.fetch_bytes(f"{BASE_URL}/board/humor/board_best.html")
            soup = BeautifulSoup(content, "html.parser")

            for a in soup.select('a[href*="read.html"]')[:MAX_POSTS_PER_SITE]:
                # 댓글수/추천수 브래킷 제거
                raw = a.get_text(strip=True)
                title = re.sub(r'\[\d+\]', '', raw).strip()
                title = re.sub(r'답글추천\s*\+\d+', '', title).strip()
                if not title or len(title) < 4:
                    continue

                href = a.get("href", "")
                url = href if href.startswith("http") else f"{BASE_URL}/board/humor/{href}"

                # 추천수 파싱
                upvotes_match = re.search(r'추천\s*\+(\d+)', a.get_text())
                upvotes = int(upvotes_match.group(1)) if upvotes_match else 0

                comments_match = re.search(r'\[(\d+)\]', raw)
                comments = int(comments_match.group(1)) if comments_match else 0

                posts.append(Post(
                    title=title,
                    source_url=url,
                    source_site=self.site_id,
                    image_url=None,
                    upvotes=upvotes,
                    comments=comments,
                    views=0,
                    created_at=datetime.now(),
                ))

        except Exception as e:
            logger.error(f"[humoruniv] 스크래핑 실패: {e}")

        return posts
