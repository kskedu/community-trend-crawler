from datetime import datetime, timezone
from typing import List
from models import Post


def calculate_score(post: Post) -> float:
    base = (post.upvotes * 3) + (post.comments * 2) + (post.views * 0.1)

    freshness = 0
    if post.created_at:
        now = datetime.now(timezone.utc)
        created = post.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        hours_ago = (now - created).total_seconds() / 3600
        if hours_ago <= 1:
            freshness = 50
        elif hours_ago <= 6:
            freshness = 30
        elif hours_ago <= 24:
            freshness = 10

    return round(base + freshness, 2)


def score_all(posts: List[Post]) -> List[Post]:
    for post in posts:
        post.score = calculate_score(post)
    return sorted(posts, key=lambda p: p.score, reverse=True)
