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
BASE_URL = "https://etoland.co.kr"
HIT_URL = f"{BASE_URL}/hit/list"
OG_IMAGE_LIMIT = 5


class EtolandScraper(BaseScraper):
    site_id = "etoland"

    def scrape(self) -> List[Post]:
        posts = []
        try:
            html = self.fetch(HIT_URL)
            soup = BeautifulSoup(html, "html.parser")

            # 새 etoland 구조: /hit/{board}/view/{slug}-{id}
            candidates = soup.select('a[href*="/hit/"][href*="/view/"]')
            logger.info(f"[etoland] /hit/list 응답 길이={len(html)}B, 후보 링크 {len(candidates)}개")

            og_count = 0
            seen_urls = set()

            for a in candidates[:MAX_POSTS_PER_SITE * 2]:
                href = a.get("href", "")
                url = urljoin(BASE_URL, href)
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                # 제목: 링크 내부의 truncate span (첫 번째)
                title_el = a.select_one("span.truncate")
                title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
                if not title or len(title) < 3:
                    continue

                # 댓글 수: span.comment-s 내부 "(N)"
                comments = 0
                cmt_el = a.select_one("span.comment-s")
                if cmt_el:
                    m = re.search(r"\((\d+)\)", cmt_el.get_text(" ", strip=True))
                    if m:
                        comments = int(m.group(1))

                # 조회 / 추천: 캡션 행 텍스트에서 추출
                caption_text = ""
                caption = a.select_one("div.caption-m")
                if caption:
                    caption_text = caption.get_text(" ", strip=True)
                views = 0
                upvotes = 0
                m_view = re.search(r"조회\s*([\d,]+)", caption_text)
                if m_view:
                    views = int(m_view.group(1).replace(",", ""))
                m_up = re.search(r"추천\s*([\d,]+)", caption_text)
                if m_up:
                    upvotes = int(m_up.group(1).replace(",", ""))

                # 썸네일: 리스트 내부 img (loading=lazy 우선, hit.svg/new.svg 등 아이콘 제외)
                image_url = None
                for img in a.select("img"):
                    src = img.get("src") or img.get("data-src") or ""
                    if not src:
                        continue
                    if "/icon/" in src or "no_image" in src or src.endswith(".svg"):
                        continue
                    image_url = src
                    break

                # 리스트에 썸네일이 없으면 og:image 추가 fetch (상위 N건)
                if not image_url and og_count < OG_IMAGE_LIMIT:
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
                    comments=comments,
                    views=views,
                    created_at=datetime.now(),
                ))

                if len(posts) >= MAX_POSTS_PER_SITE:
                    break

        except Exception as e:
            logger.error(f"[etoland] 스크래핑 실패: {e}")

        return posts
