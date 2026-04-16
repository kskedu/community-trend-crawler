from typing import List
from models import Post


def dedup_by_url(posts: List[Post]) -> List[Post]:
    """URL 기준 중복 제거 (Supabase UNIQUE 제약 보완용 로컬 필터)"""
    seen = set()
    result = []
    for post in posts:
        if post.source_url not in seen:
            seen.add(post.source_url)
            result.append(post)
    return result


def dedup_by_title(posts: List[Post], threshold: float = 0.8) -> List[Post]:
    """제목 유사도 기반 중복 제거 (간단 버전: 완전 일치만)
    TODO: cosine similarity 고도화
    """
    seen_titles = set()
    result = []
    for post in posts:
        normalized = post.title.strip().lower()
        if normalized not in seen_titles:
            seen_titles.add(normalized)
            result.append(post)
    return result


def dedup(posts: List[Post]) -> List[Post]:
    posts = dedup_by_url(posts)
    posts = dedup_by_title(posts)
    return posts
