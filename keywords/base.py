import time
import logging
from abc import ABC, abstractmethod
from typing import List, Dict
import requests
from config import HEADERS, REQUEST_TIMEOUT, RETRY_COUNT

logger = logging.getLogger(__name__)


class BaseKeywordScraper(ABC):
    source: str = ""

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
