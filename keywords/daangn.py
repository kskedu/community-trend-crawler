import re
import logging
from typing import List, Dict
from urllib.parse import quote, unquote
from keywords.base import BaseKeywordScraper

logger = logging.getLogger(__name__)
URL = "https://www.daangn.com/kr/buy-sell/"


class DaangnKeywordScraper(BaseKeywordScraper):
    source = "daangn"

    def scrape(self) -> List[Dict[str, str]]:
        html = self.fetch(URL, referer="https://www.daangn.com/")

        # 헤더 네비의 인기검색어 ul — /kr/buy-sell/s/?search= 패턴
        keywords = []
        seen = set()
        for m in re.finditer(
            r'href="/kr/buy-sell/s/\?search=([^"&]+)"[^>]*>\s*([^<]+?)\s*<',
            html,
        ):
            raw, text = m.group(1), m.group(2).strip()
            if not text or text in seen:
                continue
            kw = unquote(raw)
            seen.add(text)
            keywords.append({
                "keyword": kw,
                "url": f"https://www.daangn.com/kr/buy-sell/s/?search={quote(kw)}",
            })
            if len(keywords) >= 10:
                break

        if not keywords:
            raise RuntimeError("당근 인기검색어 파싱 실패 — HTML 구조 변경 가능성")

        return keywords
