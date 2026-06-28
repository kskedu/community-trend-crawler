"""키워드 Top10 seed.

P0 원천: 기존 keyword_cache(source='daum') 행을 read-only로 재활용.
- 기존 daum 행을 절대 수정/upsert 하지 않는다 (select만).
- dry-run에서는 실 DB 대신 fixture seed를 주입할 수 있다.
- DB read 실패 시 빈 리스트 반환(상위에서 skip 처리).
"""
import logging
from typing import List

logger = logging.getLogger(__name__)

SEED_SOURCE = "daum"
SEED_MAX = 10


def _extract_keywords(keywords_field, limit: int = SEED_MAX) -> List[str]:
    """keyword_cache.keywords (jsonb: [{keyword,url}, ...]) → 키워드 문자열 리스트."""
    result = []
    if not isinstance(keywords_field, list):
        return result
    for item in keywords_field:
        if isinstance(item, dict):
            kw = item.get("keyword")
        elif isinstance(item, str):
            kw = item
        else:
            kw = None
        if kw and isinstance(kw, str):
            kw = kw.strip()
            if kw and kw not in result:
                result.append(kw)
        if len(result) >= limit:
            break
    return result


def fetch_daum_seed(limit: int = SEED_MAX) -> List[str]:
    """keyword_cache의 daum 행에서 키워드 Top10을 read-only로 가져온다.

    db.supabase.get_client()를 사용하되 select만 수행한다(write 없음).
    실패 시 빈 리스트.
    """
    try:
        from db.supabase import get_client

        client = get_client()
        res = (
            client.table("keyword_cache")
            .select("keywords")
            .eq("source", SEED_SOURCE)
            .maybe_single()
            .execute()
        )
        data = getattr(res, "data", None)
        if not data:
            logger.warning("[news] seed: keyword_cache(daum) 행 없음 → 빈 seed")
            return []
        return _extract_keywords(data.get("keywords"), limit)
    except Exception as e:
        logger.warning("[news] seed: keyword_cache(daum) 조회 실패 → 빈 seed: %s", e)
        return []


def seed_from_fixture(fixture: dict, limit: int = SEED_MAX) -> List[str]:
    """dry-run용: fixture dict({keywords:[{keyword,url}]})에서 seed 추출."""
    if not isinstance(fixture, dict):
        return []
    return _extract_keywords(fixture.get("keywords"), limit)
