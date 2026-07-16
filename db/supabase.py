import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional
from supabase import create_client, Client
from models import Post
from config import SUPABASE_URL, SUPABASE_KEY

logger = logging.getLogger(__name__)
_client: Client = None


def get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


def upsert_posts(posts: List[Post]) -> int:
    """posts 테이블에 upsert. 성공 건수 반환."""
    if not posts:
        return 0

    client = get_client()
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = []
    for p in posts:
        rows.append({
            "title": p.title,
            "source_url": p.source_url,
            "source_site": p.source_site,
            "content": p.content,
            "image_url": p.image_url,
            "upvotes": p.upvotes,
            "comments": p.comments,
            "views": p.views,
            "score": p.score,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "collected_at": now_iso,
        })

    try:
        res = (
            client.table("community_posts")
            .upsert(rows, on_conflict="source_url")
            .execute()
        )
        count = len(res.data) if res.data else 0
        logger.info(f"upsert 완료: {count}건")
        return count
    except Exception as e:
        logger.error(f"upsert 실패: {e}")
        return 0


def upsert_keywords(source: str, keywords: List[Dict[str, str]]) -> bool:
    """keyword_cache 테이블에 upsert."""
    if not keywords:
        return False
    client = get_client()
    try:
        client.table("keyword_cache").upsert({
            "source": source,
            "keywords": keywords,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="source").execute()
        return True
    except Exception as e:
        logger.error(f"[{source}] keyword upsert 실패: {e}")
        return False


def fetch_news_issues(source: str = "news_top") -> Optional[Dict]:
    """news_issue_cache 의 기존 issues 를 read-only 로 조회(movement 비교용).

    최신 1건(order updated_at desc limit 1)만 가져온다(단일 row 보장이라도 방어적).
    row 없음/이상/조회 실패 → None (상위에서 '기존 없음'으로 처리).
    """
    try:
        client = get_client()
        res = (
            client.table("news_issue_cache")
            .select("issues,updated_at")
            .eq("source", source)
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
        )
        data = getattr(res, "data", None)
        if not data:
            return None
        row = data[0] if isinstance(data, list) else data
        return row.get("issues") if isinstance(row, dict) else None
    except Exception as e:
        logger.warning(f"[{source}] news issues read 실패(movement 비교 생략): {e}")
        return None


def upsert_news_issues(issues: Dict, source: str = "news_top") -> bool:
    """news_issue_cache 테이블에 upsert (실시간 이슈 브리핑).

    ⚠️ P0-1에서는 호출하지 않는다(정의만). 실제 write는 P0-2에서 service_role로 수행.
    on_conflict='source'(PK)로 단일 행 운영.
    """
    if not issues or not issues.get("keywords"):
        return False
    client = get_client()
    try:
        client.table("news_issue_cache").upsert({
            "source": source,
            "issues": issues,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="source").execute()
        return True
    except Exception as e:
        logger.error(f"[{source}] news issues upsert 실패: {e}")
        return False


def record_news_diagnostics(run: Dict, decisions: list) -> bool:
    """뉴스 키워드 진단 이력을 RPC 1회로 원자 적재한다.

    - 테이블 직접 write 금지 — service_role은 이 RPC EXECUTE 권한만 갖는다.
    - 멱등: 동일 run_key면 기존 run_id를 반환하고 decisions를 추가하지 않는다(RPC 계약).
    - 실패해도 news_top 결과를 되돌리거나 실패시키지 않는다 → bool만 반환한다.
    - 로그에 예외 메시지를 남기지 않는다: postgrest/httpx 예외 메시지에는 요청 payload와
      Authorization 헤더가 실릴 수 있다(사용자 확정 §10-1). 타입명만 남긴다.
    """
    if not run:
        return False
    client = get_client()
    try:
        client.rpc("news_diag_record_run", {
            "p_run": run,
            "p_decisions": decisions,
        }).execute()
        return True
    except Exception as e:
        logger.warning("[news-diag] 진단 적재 실패(랭킹 영향 없음): %s", type(e).__name__)
        return False
