import logging
from typing import List, Dict
from urllib.parse import quote
from bs4 import BeautifulSoup
from keywords.base import BaseKeywordScraper

logger = logging.getLogger(__name__)
URL = "https://www.daangn.com/kr/buy-sell/"


class DaangnKeywordScraper(BaseKeywordScraper):
    source = "daangn"

    def scrape(self) -> List[Dict[str, str]]:
        html = self.fetch(URL, referer="https://www.daangn.com/")
        soup = BeautifulSoup(html, "html.parser")

        # 헤더 네비 인기검색어 앵커. href에 지역코드(in=...)가 붙어 있어 텍스트 기준으로 추출한다.
        # 예: <a data-gtm="gnb_popular_keyword" href="/kr/buy-sell/s/?in=상계동-6073&search=픽시">픽시</a>
        keywords = []
        seen = set()
        for a in soup.select('a[data-gtm="gnb_popular_keyword"]'):
            kw = a.get_text(strip=True)
            if not kw or kw in seen:
                continue
            seen.add(kw)
            keywords.append({
                "keyword": kw,
                "url": f"https://www.daangn.com/kr/buy-sell/s/?search={quote(kw)}",
            })
            if len(keywords) >= 10:
                break

        if not keywords:
            raise RuntimeError("당근 인기검색어 파싱 실패 — HTML 구조 변경 가능성")

        return keywords
