import time
import json
import logging
from typing import List, Dict
from urllib.parse import quote
from keywords.base import BaseKeywordScraper
from config import REQUEST_TIMEOUT, RETRY_COUNT

logger = logging.getLogger(__name__)
URL = "https://www.nate.com/js/data/jsonLiveKeywordDataV1.js"


class NateKeywordScraper(BaseKeywordScraper):
    source = "nate"

    def scrape(self) -> List[Dict[str, str]]:
        data = self._fetch_json()

        if len(data) < 10:
            raise RuntimeError(f"실시간 이슈 키워드 {len(data)}개만 수신 (10개 기대)")

        keywords = []
        for item in data[:10]:
            kw = item[4]
            keywords.append({
                "keyword": kw,
                "url": f"https://news.nate.com/search?q={quote(kw)}",
            })
        return keywords

    def _fetch_json(self):
        # nate.js가 EUC-KR로만 응답하고 charset을 명시하지 않아 apparent_encoding 추측이
        # 불안정함 — base.fetch()의 텍스트 반환 대신 euc-kr을 직접 강제한다.
        for attempt in range(RETRY_COUNT):
            try:
                time.sleep(1.0)
                resp = self.session.get(URL, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                resp.encoding = "euc-kr"
                return json.loads(resp.text)
            except Exception as e:
                logger.warning(f"[{self.source}] fetch 실패 ({attempt+1}/{RETRY_COUNT}): {e}")
                if attempt == RETRY_COUNT - 1:
                    raise
        return []
