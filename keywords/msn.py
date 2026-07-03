import json
import logging
from typing import List, Dict
from urllib.parse import quote
from keywords.base import BaseKeywordScraper

logger = logging.getLogger(__name__)
URL = (
    "https://api.msn.com/news/feed/segments/trendingsearch"
    "?apikey=pWw5OmQehOA0XNfgcgrTrwEJZJJJzE83ovtTQx6JRG"
    "&market=ko-kr&fdhead=1s-ts-percent,1s-ts-pnone"
)


class MsnKeywordScraper(BaseKeywordScraper):
    source = "msn"

    def scrape(self) -> List[Dict[str, str]]:
        raw = self.fetch(URL, referer="https://www.msn.com/")
        outer = json.loads(raw)
        card = next((c for c in outer if c.get("type") == "TrendingSearchCard"), None)
        if not card:
            raise RuntimeError("TrendingSearchCard 없음")

        # data 필드는 JSON 문자열로 한 번 더 인코딩되어 있음
        items = json.loads(card["data"])
        organic = [it for it in items if not it.get("IsAds")]
        organic.sort(key=lambda it: it.get("Score", 0), reverse=True)

        if len(organic) < 10:
            raise RuntimeError(f"실시간 인기 검색어 {len(organic)}개만 수신 (10개 기대)")

        keywords = []
        for it in organic[:10]:
            kw = it["Query"]
            keywords.append({
                "keyword": kw,
                "url": f"https://www.bing.com/search?mkt=ko-kr&q={quote(kw)}",
            })
        return keywords
