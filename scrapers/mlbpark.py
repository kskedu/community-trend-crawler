import re
import logging
from datetime import datetime
from typing import List
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper
from models import Post
from config import MAX_POSTS_PER_SITE

logger = logging.getLogger(__name__)
BASE_URL = "https://mlbpark.donga.com"


class MlbparkScraper(BaseScraper):
    site_id = "mlbpark"

    def scrape(self) -> List[Post]:
        posts = []
        seen_titles = set()
        try:
            # Content-Type: charset=UTF-8이지만 requests의 charset_normalizer가
            # 오추정해 EUC-KR로 디코딩하는 이슈가 있어 bytes + 명시적 utf-8 처리
            raw = self.fetch_bytes(f"{BASE_URL}/mp/best.php")
            # 실제 서버 응답 인코딩 확인용 디버그
            logger.info(f"[mlbpark] raw bytes: {len(raw)}, first 80 hex: {raw[:80].hex()}")
            html = raw.decode("utf-8", errors="replace")
            soup = BeautifulSoup(html, "html.parser")
            first_title = soup.select_one('a[href*="/mp/b.php?b="]')
            if first_title:
                logger.info(f"[mlbpark] sample title after utf-8 decode: {first_title.get_text(strip=True)[:40]}")

            found = soup.select('a[href*="/mp/b.php?b="][href*="id="]')
            logger.info(f"[mlbpark] candidate anchors: {len(found)}")
            for i, a in enumerate(found[:3]):
                t = a.get_text(strip=True)
                logger.info(f"[mlbpark] cand{i} title={t[:40]!r} hex={t[:10].encode('utf-8').hex()}")

            for a in found:
                title = a.get_text(strip=True)
                if not title or len(title) < 5 or title in seen_titles:
                    continue
                seen_titles.add(title)

                href = a.get("href", "")
                url = href if href.startswith("http") else BASE_URL + href

                item = a.parent
                upvotes = self._int(item.select_one(".like, .recom, .likeNum") if item else None)
                comments = self._int(item.select_one(".replyNum, .reply") if item else None)
                views = self._int(item.select_one(".hit, .viewNum") if item else None)

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
                if len(posts) >= MAX_POSTS_PER_SITE:
                    break

        except Exception as e:
            logger.error(f"[mlbpark] 스크래핑 실패: {e}")

        return posts

    def _int(self, el) -> int:
        if not el:
            return 0
        try:
            return int(el.get_text(strip=True).replace(",", ""))
        except ValueError:
            return 0
