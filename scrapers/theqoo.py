import re
import logging
from datetime import datetime
from typing import List
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper
from models import Post
from config import MAX_POSTS_PER_SITE

logger = logging.getLogger(__name__)
BASE_URL = "https://theqoo.net"
OG_IMAGE_LIMIT = 10  # og:image 요청 최대 건수


class TheqooScraper(BaseScraper):
    site_id = "theqoo"

    def scrape(self) -> List[Post]:
        posts = []
        try:
            html = self.fetch(f"{BASE_URL}/hot")
            soup = BeautifulSoup(html, "html.parser")

            seen_urls = set()
            og_count = 0
            for tr in soup.select("table tr"):
                # 제목 링크: notice 포함 여부 무관, href=/hot/숫자 인 것만
                a = tr.select_one('a[href^="/hot/"]:not(.replyNum)')
                if not a:
                    continue

                href = a.get("href", "")
                # 실제 포스트 URL: /hot/숫자 (카테고리·댓글앵커 제외)
                if not re.fullmatch(r'/hot/\d+', href):
                    continue
                if href in seen_urls:
                    continue
                seen_urls.add(href)

                title = a.get_text(strip=True)
                if not title or len(title) < 3:
                    continue

                url = BASE_URL + href

                # 댓글수: a.replyNum
                reply_el = tr.select_one("a.replyNum")
                comments = self._int(reply_el) if reply_el else 0

                # 조회수: 마지막 td
                tds = tr.select("td")
                views = self._int(tds[-1]) if tds else 0

                # og:image (상위 N건)
                image_url = None
                if og_count < OG_IMAGE_LIMIT:
                    image_url = self.fetch_og_image(url)
                    og_count += 1

                posts.append(Post(
                    title=title,
                    source_url=url,
                    source_site=self.site_id,
                    image_url=image_url,
                    upvotes=0,
                    comments=comments,
                    views=views,
                    created_at=datetime.now(),
                ))
                if len(posts) >= MAX_POSTS_PER_SITE:
                    break

        except Exception as e:
            logger.error(f"[theqoo] 스크래핑 실패: {e}")

        return posts

    def _int(self, el) -> int:
        if not el:
            return 0
        try:
            return int(el.get_text(strip=True).replace(",", ""))
        except ValueError:
            return 0
