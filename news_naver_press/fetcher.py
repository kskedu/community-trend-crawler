"""popularDay.naver HTTP fetch — 우회 없는 fail-closed 정책.

정책(사용자 확정):
- run당 기본 1회 요청.
- connect/read timeout 명시.
- 403 → 재시도 금지, 즉시 중단(BLOCKED).
- 429 → 즉시 중단(RATE_LIMITED).
- CAPTCHA/challenge로 의심되는 응답 → 중단(CHALLENGE_SUSPECTED).
- 5xx → 최대 1회 제한적 retry.
- 그 외 실패(timeout/connection error) → retry 없이 실패.
- User-Agent는 고정 1종만 사용(위장/로테이션 금지) — 기존 news/thumbnail.py와 동일한
  자기 식별형 UA를 재사용한다.
- 프록시/IP 로테이션/헤드리스 브라우저 사용 안 함(순수 HTTP GET, requests만 사용).
"""
import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger(__name__)

TARGET_URL = "https://news.naver.com/main/ranking/popularDay.naver"
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 10.0
MAX_5XX_RETRY = 1

USER_AGENT = (
    "Mozilla/5.0 (compatible; StartHubBot/1.0; +https://starthub.sk-aistudio.com)"
)

# challenge 페이지는 보통 매우 짧거나 특정 문구를 포함한다(정상 페이지는 700KB+).
_CHALLENGE_MARKERS = ("captcha", "보안문자", "잠시만 기다려", "unusual traffic")
_MIN_PLAUSIBLE_BYTES = 5000


@dataclass
class FetchResult:
    ok: bool
    status_code: Optional[int] = None
    html: Optional[str] = None
    reason: Optional[str] = None  # ok=False일 때: BLOCKED / RATE_LIMITED / CHALLENGE_SUSPECTED / HTTP_ERROR / TIMEOUT / NETWORK_ERROR / BAD_CONTENT_TYPE


def _looks_like_challenge(text: str) -> bool:
    if not text:
        return True
    if len(text) < _MIN_PLAUSIBLE_BYTES:
        return True
    lowered = text.lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


def fetch_popular_day(url: str = TARGET_URL) -> FetchResult:
    """popularDay.naver를 1회 GET. 실패 시 우회하지 않고 즉시 fail-closed 반환."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    }

    attempt = 0
    max_attempts = 1 + MAX_5XX_RETRY
    last_status = None

    while attempt < max_attempts:
        attempt += 1
        try:
            resp = requests.get(
                url,
                headers=headers,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
        except requests.exceptions.Timeout:
            logger.warning("[naver_press] fetch timeout (attempt=%d)", attempt)
            return FetchResult(ok=False, reason="TIMEOUT")
        except requests.exceptions.RequestException as e:
            logger.warning("[naver_press] fetch network error: %s", type(e).__name__)
            return FetchResult(ok=False, reason="NETWORK_ERROR")

        status = resp.status_code
        last_status = status

        if status == 403:
            logger.warning("[naver_press] HTTP 403 → 우회하지 않고 중단")
            return FetchResult(ok=False, status_code=status, reason="BLOCKED")
        if status == 429:
            logger.warning("[naver_press] HTTP 429 → 중단")
            return FetchResult(ok=False, status_code=status, reason="RATE_LIMITED")
        if 500 <= status < 600:
            if attempt < max_attempts:
                logger.warning(
                    "[naver_press] HTTP %d(5xx) → 제한적 1회 retry (attempt=%d)",
                    status, attempt,
                )
                time.sleep(1.0)
                continue
            logger.warning("[naver_press] HTTP %d(5xx) → retry 소진, 중단", status)
            return FetchResult(ok=False, status_code=status, reason="HTTP_ERROR")
        if status != 200:
            logger.warning("[naver_press] 예상치 못한 HTTP status=%d → 중단", status)
            return FetchResult(ok=False, status_code=status, reason="HTTP_ERROR")

        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "html" not in ctype:
            logger.warning("[naver_press] content-type 비정상(%s) → 중단", ctype)
            return FetchResult(ok=False, status_code=status, reason="BAD_CONTENT_TYPE")

        # 페이지 인코딩은 EUC-KR(운영 실측). apparent_encoding으로 재확인해 안전하게 디코딩.
        resp.encoding = resp.apparent_encoding or "euc-kr"
        text = resp.text

        if _looks_like_challenge(text):
            logger.warning("[naver_press] CAPTCHA/challenge 의심 응답 → 중단")
            return FetchResult(ok=False, status_code=status, reason="CHALLENGE_SUSPECTED")

        return FetchResult(ok=True, status_code=status, html=text)

    return FetchResult(ok=False, status_code=last_status, reason="HTTP_ERROR")
