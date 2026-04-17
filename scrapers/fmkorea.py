import re
import logging
from datetime import datetime
from typing import List
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper
from models import Post
from config import MAX_POSTS_PER_SITE

logger = logging.getLogger(__name__)
BASE_URL = "https://www.fmkorea.com"
OG_IMAGE_LIMIT = 10


class FmkoreaScraper(BaseScraper):
    site_id = "fmkorea"

    def scrape(self) -> List[Post]:
        posts = []
        seen_titles = set()
        try:
            self.session.headers.update({
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Referer": "https://www.fmkorea.com/",
                "Upgrade-Insecure-Requests": "1",
            })
            html = self.fetch(f"{BASE_URL}/best")
            soup = BeautifulSoup(html, "html.parser")

            og_count = 0
            for item in soup.select("li.li"):
                title_el = item.select_one("h3.title a") or item.select_one(".title a")
                if not title_el:
                    continue

                raw = title_el.get_text(strip=True)
                # 댓글수 [숫자] 제거
                title = re.sub(r'\[\d+\]', '', raw).strip()
                if not title or len(title) < 4 or title in seen_titles:
                    continue
                seen_titles.add(title)

                href = title_el.get("href", "")
                # document_srl 추출 → 실제 URL
                srl_match = re.search(r'document_srl=(\d+)', href)
                if srl_match:
                    url = f"{BASE_URL}/{srl_match.group(1)}"
                else:
                    url = href if href.startswith("http") else BASE_URL + href

                # 리스트 썸네일 시도
                img_el = item.select_one("img")
                image_url = img_el.get("src") if img_el else None
                if not image_url and og_count < OG_IMAGE_LIMIT:
                    image_url = self.fetch_og_image(url)
                    og_count += 1

                upvotes = self._int(item.select_one(".recomend_num, .recommend_num, .recom"))
                comments = self._int(item.select_one(".comment_count, .replyNum"))
                views = self._int(item.select_one(".hit, .view_count"))

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
                if len(posts) >= MAX_POSTS_PER_SITE:
                    break

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
