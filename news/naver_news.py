"""네이버 검색 > 뉴스 API 호출부.

P0-1 안전 계약:
- NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 둘 중 하나라도 없으면 raise 하지 않고
  skip + WARNING 로그 후 빈 리스트 반환. (조용한 성공 금지)
- 실제 호출 코드는 작성하되, P0-1 dry-run은 키가 없어 항상 skip 경로를 탄다.
- Client ID/Secret 값은 로그에 출력하지 않는다.
- 프론트는 이 모듈을 절대 호출하지 않는다(crawler 전용).
"""
import logging
import os
from typing import List

import requests

logger = logging.getLogger(__name__)

NAVER_NEWS_API = "https://openapi.naver.com/v1/search/news.json"
REQUEST_TIMEOUT = 10


def _credentials():
    return os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET")


def is_enabled() -> bool:
    cid, secret = _credentials()
    return bool(cid and secret)


def search_news(keyword: str, display: int = 8) -> List[dict]:
    """키워드별 뉴스 raw item 리스트 반환. 실패/키없음 시 빈 리스트.

    반환 형태는 네이버 응답의 items (정규화 전 raw). normalizer가 후처리한다.
    """
    cid, secret = _credentials()
    if not (cid and secret):
        logger.warning(
            "[news] NAVER_CLIENT_ID/SECRET 미설정 → '%s' 뉴스 수집 skip (P0-1 정상 경로)",
            keyword,
        )
        return []

    display = max(1, min(int(display), 10))
    headers = {
        "X-Naver-Client-Id": cid,
        "X-Naver-Client-Secret": secret,
    }
    params = {"query": keyword, "display": display, "sort": "date"}
    try:
        resp = requests.get(
            NAVER_NEWS_API, headers=headers, params=params, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        if not isinstance(items, list):
            logger.warning("[news] '%s' 응답 items 형식 비정상 → 빈 결과", keyword)
            return []
        return items
    except Exception as e:
        # 키 값은 로그에 노출하지 않는다.
        logger.warning("[news] '%s' 뉴스 수집 실패 → skip: %s", keyword, e)
        return []
