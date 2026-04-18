import re
import logging
from datetime import datetime
from typing import List
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper
from models import Post
from config import MAX_POSTS_PER_SITE

logger = logging.getLogger(__name__)
BASE_URL = "https://ygosu.com"


class YgosuScraper(BaseScraper):
    site_id = "ygosu"

    def scrape(self) -> List[Post]:
        posts = []
        try:
            html = self.fetch(f"{BASE_URL}/board/best_article/?type=daily&type2=group0")
            soup = BeautifulSoup(html, "html.parser")

            seen_urls = set()

            for row in soup.select("tr"):
                td = row.select_one("td.tit")
                if not td:
                    continue

                a = td.select_one("a[href*='/board/best_article/']")
                if not a:
                    continue

                href = a.get("href", "")
                url = href if href.startswith("http") else BASE_URL + href
                # 쿼리스트링 제거한 키로 중복 확인
                url_key = re.sub(r'\?.*', '', url)
                if url_key in seen_urls:
                    continue
                seen_urls.add(url_key)

                # 제목: 카테고리 span, img, reply_cnt span 제거
                for el in a.select("span.category, img, span.reply_cnt"):
                    el.decompose()
                title = a.get_text(strip=True)

                if not title or len(title) < 3:
                    continue

                # 댓글수: 원본 a에서 reply_cnt는 이미 decompose됐으므로 row에서 재탐색
                cmt_el = row.select_one("span.reply_cnt")
                comments = 0
                if cmt_el:
                    m = re.search(r'\d+', cmt_el.get_text())
                    if m:
                        comments = int(m.group())

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
            logger.error(f"[ygosu] 스크래핑 실패: {e}")

        return posts
