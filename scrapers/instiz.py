import re
import logging
from datetime import datetime
from typing import List
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper
from models import Post
from config import MAX_POSTS_PER_SITE

logger = logging.getLogger(__name__)
BASE_URL = "https://www.instiz.net"


class InstizScraper(BaseScraper):
    site_id = "instiz"

    def scrape(self) -> List[Post]:
        posts = []
        try:
            html = self.fetch(f"{BASE_URL}/pt")
            soup = BeautifulSoup(html, "html.parser")

            seen_urls = set()

            for row in soup.select("tr#detour"):
                a = row.select_one("td.listsubject a[href*='/pt/']")
                if not a:
                    continue

                url = a.get("href", "")
                if not url.startswith("http"):
                    url = BASE_URL + url
                # 중복 제거
                url_key = re.sub(r'\?.*', '', url)
                if url_key in seen_urls:
                    continue
                seen_urls.add(url_key)

                # 제목: span.texthead_notice 텍스트에서 댓글수(span.cmt3) 제거
                span = a.select_one("span.texthead_notice")
                if span:
                    cmt_el = span.select_one("span.cmt3")
                    if cmt_el:
                        cmt_el.decompose()
                    title = span.get_text(strip=True)
                else:
                    title = a.get_text(strip=True)

                if not title or len(title) < 3:
                    continue

                # 댓글수: span.cmt3 title 속성 "유효 댓글 수 N"
                cmt_tag = row.select_one("span.cmt3")
                comments = 0
                if cmt_tag:
                    m = re.search(r'(\d+)', cmt_tag.get("title", "") or cmt_tag.get_text())
                    if m:
                        comments = int(m.group(1))

                # 조회수/추천: td.listno (width="45"=조회수, width="25"=추천)
                list_nos = row.select("td.listno")
                views = 0
                upvotes = 0
                for td in list_nos:
                    w = td.get("width", "")
                    val = self._int(td)
                    if w == "45":
                        views = val
                    elif w == "25":
                        upvotes = val

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
            logger.error(f"[instiz] 스크래핑 실패: {e}")

        return posts

    def _int(self, el) -> int:
        if not el:
            return 0
        try:
            return int(el.get_text(strip=True).replace(",", ""))
        except ValueError:
            return 0
