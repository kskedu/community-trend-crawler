import re
import logging
from typing import List, Dict
from urllib.parse import quote
from keywords.base import BaseKeywordScraper

logger = logging.getLogger(__name__)
URL = "https://search.danawa.com/dsearch.php?query=best"


class DanawaKeywordScraper(BaseKeywordScraper):
    source = "danawa"

    def scrape(self) -> List[Dict[str, str]]:
        html = self.fetch(URL, referer="https://www.danawa.com/")

        parts = html.split("hot_keyword", 1)
        if len(parts) < 2:
            raise RuntimeError("hot_keyword 섹션 없음")
        section = parts[1].split("</dl>", 1)[0]

        keywords = []
        for m in re.finditer(
            r'<a href="/dsearch\.php\?query=[^"]*" target="_self" title="([^"]+)">',
            section,
        ):
            kw = m.group(1)
            keywords.append({
                "keyword": kw,
                "url": f"https://search.danawa.com/dsearch.php?query={quote(kw)}",
            })
            if len(keywords) >= 10:
                break
        return keywords
