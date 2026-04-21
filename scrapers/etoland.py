import re
import logging
from datetime import datetime
from typing import List
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper
from models import Post
from config import MAX_POSTS_PER_SITE

logger = logging.getLogger(__name__)
BASE_URL = "https://www.etoland.co.kr"
HIT_URL = f"{BASE_URL}/bbs/hit.php"
OG_IMAGE_LIMIT = 10


class EtolandScraper(BaseScraper):
    site_id = "etoland"

    def scrape(self) -> List[Post]:
        posts = []
        try:
            content = self.fetch_bytes(HIT_URL)
            soup = BeautifulSoup(content, "html.parser")

            candidates = soup.select('a[href*="wr_id="]')
            logger.info(f"[etoland] 응답 {len(content)}B, a[wr_id=] {len(candidates)}개 발견")

            og_count = 0
            seen_urls = set()

            for a in candidates[:MAX_POSTS_PER_SITE * 2]:
                href = a.get("href", "")
                url = urljoin(HIT_URL, href)

                if url in seen_urls:
                    continue
                seen_urls.add(url)

                # 제목 파싱 (댓글수 괄호 제거)
                raw = a.get_text(strip=True)
                comments_match = re.search(r'\((\d+)\)\s*$', raw)
                comments = int(comments_match.group(1)) if comments_match else 0
                title = re.sub(r'\s*\(\d+\)\s*$', '', raw).strip()

                if not title or len(title) < 3:
                    continue

                # og:image (상위 N건)
                image_url = None
                if og_count < OG_IMAGE_LIMIT:
                    og = self.fetch_og_image(url)
                    if og and "no_image" not in og and "test.png" not in og:
                        image_url = og
                    og_count += 1

                posts.append(Post(
                    title=title,
                    source_url=url,
                    source_site=self.site_id,
                    image_url=image_url,
                    upvotes=0,
                    comments=comments,
                    views=0,
                    created_at=datetime.now(),
                ))

                if len(posts) >= MAX_POSTS_PER_SITE:
                    break

        except Exception as e:
            logger.error(f"[etoland] 스크래핑 실패: {e}")

        return posts
