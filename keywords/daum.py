import re
import logging
from typing import List, Dict
from urllib.parse import quote
from keywords.base import BaseKeywordScraper

logger = logging.getLogger(__name__)
URL = "https://search.daum.net/search?w=tot&q=%E3%84%B4%E3%84%B4"


class DaumKeywordScraper(BaseKeywordScraper):
    source = "daum"

    def scrape(self) -> List[Dict[str, str]]:
        html = self.fetch(URL, referer="https://www.daum.net/")

        parts = html.split("list_trend")
        section = parts[2] if len(parts) > 2 else (parts[1] if len(parts) > 1 else "")
        if not section:
            raise RuntimeError("list_trend 섹션 없음")
        chunk = section.split("</ul>", 1)[0]

        keywords = []
        for m in re.finditer(r'data-keyword="([^"]+)"', chunk):
            kw = m.group(1)
            keywords.append({
                "keyword": kw,
                "url": f"https://search.daum.net/search?q={quote(kw)}",
            })
            if len(keywords) >= 10:
                break
        return keywords
