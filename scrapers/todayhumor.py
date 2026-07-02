import logging
from datetime import datetime
from typing import List
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper
from models import Post
from config import MAX_POSTS_PER_SITE

logger = logging.getLogger(__name__)
BASE_URL = "https://www.todayhumor.co.kr"
OG_IMAGE_LIMIT = 10


class TodayhumorScraper(BaseScraper):
    site_id = "todayhumor"

    def scrape(self) -> List[Post]:
        posts = []
        self.last_status = "failed"
        try:
            content = self.fetch_bytes(
                f"{BASE_URL}/board/list.php?table=bestofbest",
                referer=f"{BASE_URL}/",
            )
            soup = BeautifulSoup(content, "html.parser")

            og_count = 0
            for a in soup.select('td.subject a[href*="view.php"]')[:MAX_POSTS_PER_SITE]:
                title = a.get_text(strip=True)
                if not title or len(title) < 3:
                    continue

                href = a.get("href", "")
                url = href if href.startswith("http") else BASE_URL + href

                # 부모 tr에서 조회수/추천수 추출
                # 컬럼 순서: 번호, 제목, 글쓴이, 날짜, 조회수, 추천수
                row = a.find_parent("tr")
                tds = row.select("td") if row else []
                views = self._int(tds[-2]) if len(tds) >= 2 else 0
                upvotes = self._int(tds[-1]) if tds else 0

                # og:image (상위 N건, 플레이스홀더 제외)
                image_url = None
                if og_count < OG_IMAGE_LIMIT:
                    og = self.fetch_og_image(url)
                    if og and "test.png" not in og and "no_image" not in og:
                        image_url = og
                    og_count += 1

                posts.append(Post(
                    title=title,
                    source_url=url,
                    source_site=self.site_id,
                    image_url=image_url,
                    upvotes=upvotes,
                    comments=0,
                    views=views,
                    created_at=datetime.now(),
                ))

        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 403:
                # 로컬(국내 IP)에서는 정상 응답 확인됨 — GH Actions 등 해외 IP 대역
                # 차단으로 추정. 소스 자체는 살아있어 제거하지 않고 skipped로만 기록.
                logger.warning(f"[todayhumor] 403 skipped(지역/IP 차단 추정, source 유지): {e}")
                self.last_status = "skipped"
            else:
                logger.error(f"[todayhumor] 스크래핑 실패: {e}")

        return posts

    def _int(self, el) -> int:
        if not el:
            return 0
        try:
            return int(el.get_text(strip=True).replace(",", ""))
        except ValueError:
            return 0
