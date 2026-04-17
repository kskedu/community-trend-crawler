import re
import time
import logging
from abc import ABC, abstractmethod
from typing import List, Optional
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

    def fetch_og_image(self, url: str) -> Optional[str]:
        """포스트 페이지에서 og:image 추출 (실패해도 무시)"""
        try:
            time.sleep(0.5)
            resp = self.session.get(url, timeout=5)
            resp.raise_for_status()
            m = re.search(
                r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']'
                r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
                resp.text, re.IGNORECASE
            )
            if m:
                return m.group(1) or m.group(2)
        except Exception:
            pass
        return None

    @abstractmethod
    def scrape(self) -> List[Post]:
        """인기글 목록 반환"""
        pass
