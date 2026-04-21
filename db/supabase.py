import logging
from datetime import datetime, timezone
from typing import List, Dict
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
