import re
import logging
from typing import List, Dict
from keywords.base import BaseKeywordScraper

logger = logging.getLogger(__name__)
URL = "https://namu.news/"


class NamuwikiKeywordScraper(BaseKeywordScraper):
    source = "namuwiki"

    def scrape(self) -> List[Dict[str, str]]:
        html = self.fetch(URL, referer="https://namu.wiki/")

        keywords = []
        for m in re.finditer(
            r'"WikiRank","no":\d+,"keyword":"([^"]+)","url":"([^"]+)"',
            html,
        ):
            kw = m.group(1)
            raw = m.group(2)
            url = "https:" + raw if raw.startswith("//") else raw
            keywords.append({"keyword": kw, "url": url})
            if len(keywords) >= 10:
                break
        if not keywords:
            raise RuntimeError("WikiRank 데이터 없음")
        return keywords
