import time
import logging
from abc import ABC, abstractmethod
from typing import List
import requests
from models import Post
from config import HEADERS, REQUEST_DELAY, REQUEST_TIMEOUT, RETRY_COUNT

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    site_id: str = ""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def fetch(self, url: str) -> str:
        for attempt in range(RETRY_COUNT):
            try:
                time.sleep(REQUEST_DELAY)
                resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                return resp.text
            except Exception as e:
                logger.warning(f"[{self.site_id}] fetch 실패 ({attempt+1}/{RETRY_COUNT}): {e}")
                if attempt == RETRY_COUNT - 1:
                    raise
        return ""

    def fetch_bytes(self, url: str) -> bytes:
        """인코딩 자동 감지가 필요한 사이트용 (EUC-KR 등)"""
        for attempt in range(RETRY_COUNT):
            try:
                time.sleep(REQUEST_DELAY)
                resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                return resp.content
            except Exception as e:
                logger.warning(f"[{self.site_id}] fetch 실패 ({attempt+1}/{RETRY_COUNT}): {e}")
                if attempt == RETRY_COUNT - 1:
                    raise
        return b""

    @abstractmethod
    def scrape(self) -> List[Post]:
        """인기글 목록 반환"""
        pass
