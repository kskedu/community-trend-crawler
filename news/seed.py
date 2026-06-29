"""키워드 Top10 seed.

P0 원천: 기존 keyword_cache(source='daum') 행을 read-only로 재활용.
- 기존 daum 행을 절대 수정/upsert 하지 않는다 (select만).
- dry-run에서는 실 DB 대신 fixture seed를 주입할 수 있다.
- DB read 실패 시 빈 리스트 반환(상위에서 skip 처리).
- P0-2: keyword_cache(daum).updated_at 기준 freshness 도 함께 반환한다.
  daum 수집이 실패해 stale 한 행이 남아있어도 news_top 이 갱신되어
  "실시간"처럼 보이는 것을 막기 위함 (상위에서 stale 이면 upsert skip).
"""
import logging
from datetime import datetime, timezone
from typing import List, Tuple

logger = logging.getLogger(__name__)

SEED_SOURCE = "daum"
SEED_MAX = 10
FRESHNESS_THRESHOLD_SECONDS = 2 * 60 * 60  # 2시간 (cron 매시 실행 기준)


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


def _is_fresh(updated_at_raw) -> bool:
    """updated_at 문자열이 FRESHNESS_THRESHOLD_SECONDS 이내인지 판단.

    없음/None/파싱 실패/미래값 등 애매한 경우는 전부 False(stale) 처리한다.
    """
    if not updated_at_raw or not isinstance(updated_at_raw, str):
        return False
    try:
        updated_at = datetime.fromisoformat(updated_at_raw.replace("Z", "+00:00"))
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    now = datetime.now(timezone.utc)
    age_seconds = (now - updated_at).total_seconds()
    if age_seconds < 0:
        # 미래값(시계 오차 등) → stale 처리
        return False
    return age_seconds <= FRESHNESS_THRESHOLD_SECONDS


def fetch_daum_seed(limit: int = SEED_MAX) -> Tuple[List[str], bool]:
    """keyword_cache의 daum 행에서 키워드 Top10과 freshness 를 read-only로 가져온다.

    db.supabase.get_client()를 사용하되 select만 수행한다(write 없음).
    반환: (키워드 리스트, is_fresh). 실패 시 ([], False).
    """
    try:
        from db.supabase import get_client

        client = get_client()
        res = (
            client.table("keyword_cache")
            .select("keywords,updated_at")
            .eq("source", SEED_SOURCE)
            .maybe_single()
            .execute()
        )
        data = getattr(res, "data", None)
        if not data:
            logger.warning("[news] seed: keyword_cache(daum) 행 없음 → 빈 seed")
            return [], False
        keywords = _extract_keywords(data.get("keywords"), limit)
        is_fresh = _is_fresh(data.get("updated_at"))
        if not is_fresh:
            logger.warning(
                "[news] seed: keyword_cache(daum) stale(updated_at=%s) → news_top upsert skip 대상",
                data.get("updated_at"),
            )
        return keywords, is_fresh
    except Exception as e:
        logger.warning("[news] seed: keyword_cache(daum) 조회 실패 → 빈 seed: %s", e)
        return [], False


def seed_from_fixture(fixture: dict, limit: int = SEED_MAX) -> List[str]:
    """dry-run용: fixture dict({keywords:[{keyword,url}]})에서 seed 추출."""
    if not isinstance(fixture, dict):
        return []
    return _extract_keywords(fixture.get("keywords"), limit)
