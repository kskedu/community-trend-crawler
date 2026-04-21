import re
import logging
from datetime import datetime
from typing import List
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper
from models import Post

logger = logging.getLogger(__name__)
BASE_URL = "https://pann.nate.com"
HOME_URL = f"{BASE_URL}/"
MAX_POSTS = 40           # 톡커들의 선택은 40개 리스트
OG_IMAGE_LIMIT = 10


class NatepannScraper(BaseScraper):
    """네이트판 '톡커들의 선택' Top 40 (대문 talkerChoiceArea0, 1)"""
    site_id = "natepann"

    def scrape(self) -> List[Post]:
        posts = []
        try:
            content = self.fetch_bytes(HOME_URL)
            # 네이트판 대문은 UTF-8 (Content-Type: text/html; charset=utf-8)
            html = content.decode("utf-8", errors="replace")
            soup = BeautifulSoup(html, "html.parser")

            # talkerChoiceArea0: 1~20위, talkerChoiceArea1: 21~40위
            # (Area2, Area3는 동일 데이터 중복이라 제외)
            anchors = []
            for area_id in ("talkerChoiceArea0", "talkerChoiceArea1"):
                ol = soup.find("ol", id=area_id)
                if ol:
                    anchors.extend(ol.find_all("a", href=re.compile(r"^/talk/\d+")))

            og_count = 0
            seen_urls = set()

            for a in anchors:
                href = a.get("href", "")
                title = a.get("title", "").strip() or a.get_text(strip=True)
                if not title or len(title) < 3:
                    continue

                url = urljoin(BASE_URL, href)
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                # 추천수 파싱: <li>...<span class="count"><i>348</i></span></li>
                upvotes = 0
                li = a.find_parent("li")
                if li:
                    i_el = li.select_one("span.count i")
                    if i_el:
                        try:
                            upvotes = int(i_el.get_text(strip=True).replace(",", ""))
                        except ValueError:
                            pass

                image_url = None
                if og_count < OG_IMAGE_LIMIT:
                    og = self.fetch_og_image(url)
                    if og and "no_image" not in og:
                        image_url = og
                    og_count += 1

                posts.append(Post(
                    title=title,
                    source_url=url,
                    source_site=self.site_id,
                    image_url=image_url,
                    upvotes=upvotes,
                    comments=0,
                    views=0,
                    created_at=datetime.now(),
                ))

                if len(posts) >= MAX_POSTS:
                    break

        except Exception as e:
            logger.error(f"[natepann] 스크래핑 실패: {e}")

        return posts
