"""네이버 언론사별 인기뉴스(popularDay) 수집 오케스트레이션 — Phase 1.

계약:
- fetch 실패/parser 실패 시 기존 cache(latest snapshot)를 절대 덮지 않는다.
- 성공 snapshot만 overwrite한다(history 없음, latest 1개만 유지).
- 본문/description은 다루지 않는다 — parser가 이미 그렇게 보장한다.
- 독립 entrypoint(news_naver_press.__main__)에서만 호출된다. 기존 news 파이프라인
  (main.py run_news_briefing 등)과 failure domain을 완전히 분리한다.
"""
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

from db.supabase import upsert_generic_snapshot
from news_naver_press.fetcher import fetch_popular_day
from news_naver_press.parser import parse_popular_day_html

logger = logging.getLogger(__name__)

CACHE_SOURCE = "naver_press_popular"


@dataclass
class RunOutcome:
    status: str  # "success" | "skipped_fetch_failed" | "skipped_parse_failed" | "skipped_cache_write_failed"
    reason: Optional[str] = None
    http_status: Optional[int] = None
    press_count: int = 0
    valid_item_count: int = 0
    malformed_box_count: int = 0
    duplicate_press_count: int = 0
    duplicate_article_count: int = 0
    payload_bytes: int = 0
    duration_seconds: float = 0.0
    previous_cache_preserved: bool = True


def run_naver_press_popular_collection() -> RunOutcome:
    started = time.monotonic()
    logger.info("[naver_press] fetch started url=popularDay.naver")

    fetch_result = fetch_popular_day()
    if not fetch_result.ok:
        logger.warning(
            "[naver_press] fetch 실패 reason=%s status=%s → 기존 cache 유지, 저장 생략",
            fetch_result.reason, fetch_result.status_code,
        )
        return RunOutcome(
            status="skipped_fetch_failed",
            reason=fetch_result.reason,
            http_status=fetch_result.status_code,
            duration_seconds=time.monotonic() - started,
            previous_cache_preserved=True,
        )

    logger.info("[naver_press] HTTP status=%d", fetch_result.status_code)

    parse_result = parse_popular_day_html(fetch_result.html)
    if not parse_result.ok:
        logger.warning(
            "[naver_press] parser 검증 실패 reason=%s box_count=%d → 기존 cache 유지, 저장 생략",
            parse_result.reason, parse_result.box_count,
        )
        return RunOutcome(
            status="skipped_parse_failed",
            reason=parse_result.reason,
            http_status=fetch_result.status_code,
            press_count=parse_result.box_count,
            malformed_box_count=parse_result.malformed_box_count,
            duration_seconds=time.monotonic() - started,
            previous_cache_preserved=True,
        )

    logger.info(
        "[naver_press] parsed press_count=%d valid_item_count=%d malformed_box_count=%d "
        "duplicate_press_count=%d duplicate_article_count=%d",
        parse_result.box_count, parse_result.valid_item_count,
        parse_result.malformed_box_count, parse_result.duplicate_press_count,
        parse_result.duplicate_article_count,
    )

    fetched_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "source": CACHE_SOURCE,
        "fetched_at": fetched_at,
        "item_count": len(parse_result.items),
        "items": [asdict(item) for item in parse_result.items],
    }
    import json
    payload_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    write_ok = upsert_generic_snapshot(CACHE_SOURCE, payload)
    duration = time.monotonic() - started

    if not write_ok:
        logger.error("[naver_press] cache write 실패 → 기존 cache 유지")
        return RunOutcome(
            status="skipped_cache_write_failed",
            reason="CACHE_WRITE_FAILED",
            http_status=fetch_result.status_code,
            press_count=parse_result.box_count,
            valid_item_count=parse_result.valid_item_count,
            malformed_box_count=parse_result.malformed_box_count,
            duplicate_press_count=parse_result.duplicate_press_count,
            duplicate_article_count=parse_result.duplicate_article_count,
            payload_bytes=payload_bytes,
            duration_seconds=duration,
            previous_cache_preserved=True,
        )

    logger.info(
        "[naver_press] cache write 성공 source=%s item_count=%d bytes=%d duration=%.2fs",
        CACHE_SOURCE, len(parse_result.items), payload_bytes, duration,
    )
    return RunOutcome(
        status="success",
        http_status=fetch_result.status_code,
        press_count=parse_result.box_count,
        valid_item_count=parse_result.valid_item_count,
        malformed_box_count=parse_result.malformed_box_count,
        duplicate_press_count=parse_result.duplicate_press_count,
        duplicate_article_count=parse_result.duplicate_article_count,
        payload_bytes=payload_bytes,
        duration_seconds=duration,
        previous_cache_preserved=False,
    )
