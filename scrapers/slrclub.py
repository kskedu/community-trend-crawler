import re
import logging
from datetime import datetime
from typing import List
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper
from models import Post
from config import MAX_POSTS_PER_SITE

logger = logging.getLogger(__name__)
BASE_URL = "https://www.slrclub.com"
OG_IMAGE_LIMIT = 10


class SlrclubScraper(BaseScraper):
    site_id = "slrclub"

    def scrape(self) -> List[Post]:
        posts = []
        seen_urls = set()
        try:
            content = self.fetch_bytes(f"{BASE_URL}/bbs/zboard.php?id=hot_article")
            soup = BeautifulSoup(content, "html.parser")

            og_count = 0
            for a in soup.select('a[href*="vx2.php"]'):
                href = a.get("href", "")
                if not href or href in seen_urls:
                    continue
                seen_urls.add(href)

                raw = a.get_text(strip=True)
                # 댓글수 [숫자] 제거
                comments_match = re.search(r'\[(\d+)\]', raw)
                comments = int(comments_match.group(1)) if comments_match else 0
                title = re.sub(r'\[\d+\]', '', raw).strip()
                if not title or len(title) < 4:
                    continue

                url = href if href.startswith("http") else BASE_URL + href

                # 조회수: 부모 tr의 마지막 td
                row = a.find_parent("tr")
                tds = row.select("td") if row else []
                views = self._int(tds[-1]) if tds else 0

                # 포스트 본문에서 media.slrclub.com 이미지 추출 (상위 N건)
                image_url = None
                if og_count < OG_IMAGE_LIMIT:
                    image_url = self._fetch_slr_image(url)
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
            logger.error(f"[slrclub] 스크래핑 실패: {e}")

        return posts

    def _fetch_slr_image(self, url: str):
        """포스트 본문에서 media.slrclub.com 이미지 추출"""
        try:
            import time
            time.sleep(0.5)
            resp = self.session.get(url, timeout=5)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.content, "html.parser")
            for img in soup.select("img"):
                src = img.get("src", "")
                if "media.slrclub.com" in src and not src.lower().endswith((".gif", ".png")):
                    return ("https:" + src) if src.startswith("//") else src
        except Exception:
            pass
        return None

    def _int(self, el) -> int:
        if not el:
            return 0
        try:
            return int(el.get_text(strip=True).replace(",", ""))
        except ValueError:
            return 0
