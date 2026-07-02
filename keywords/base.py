import time
import logging
from abc import ABC, abstractmethod
from typing import List, Dict
import requests
from config import HEADERS, REQUEST_TIMEOUT, RETRY_COUNT

logger = logging.getLogger(__name__)


class BaseKeywordScraper(ABC):
    source: str = ""
    active: bool = True  # False면 upstream 없음 — run()에서 scrape() 호출 없이 skipped 처리

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def fetch(self, url: str, referer: str = None) -> str:
        headers = {"Referer": referer} if referer else {}
        for attempt in range(RETRY_COUNT):
            try:
                time.sleep(1.0)
                resp = self.session.get(url, timeout=REQUEST_TIMEOUT, headers=headers)
                resp.raise_for_status()
                # 일부 사이트가 Content-Type에 charset을 명시하지 않으면 requests가
                # RFC 기본값(ISO-8859-1)으로 잘못 추측해 한글이 깨진다. 실제 인코딩
                # 추정치(apparent_encoding)를 우선 사용.
                if resp.encoding == "ISO-8859-1":
                    resp.encoding = resp.apparent_encoding
                return resp.text
            except Exception as e:
                logger.warning(f"[{self.source}] fetch 실패 ({attempt+1}/{RETRY_COUNT}): {e}")
                if attempt == RETRY_COUNT - 1:
                    raise
        return ""

    @abstractmethod
    def scrape(self) -> List[Dict[str, str]]:
        """[{keyword, url}, ...] 반환. 실패 시 예외."""
        pass
