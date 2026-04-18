from datetime import datetime, timezone
from typing import List
from models import Post


def calculate_score(post: Post) -> float:
    base = (post.upvotes * 3) + (post.comments * 2) + (post.views * 0.1)

    # 시간 감쇠 (곱셈) — 오래될수록 score 급감
    decay = 0.02  # 기본값 (7일 이상)
    if post.created_at:
        now = datetime.now(timezone.utc)
        created = post.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        hours_ago = (now - created).total_seconds() / 3600
        if hours_ago <= 1:
            decay = 1.0
        elif hours_ago <= 6:
            decay = 0.7
        elif hours_ago <= 12:
            decay = 0.4
        elif hours_ago <= 24:
            decay = 0.2
        elif hours_ago <= 72:
            decay = 0.08
        elif hours_ago <= 168:  # 7일
            decay = 0.03

    return round(base * decay, 2)


def score_all(posts: List[Post]) -> List[Post]:
    for post in posts:
        post.score = calculate_score(post)
    return sorted(posts, key=lambda p: p.score, reverse=True)
