"""키워드 내 기사 중복 제거 (URL 기준)."""
from typing import List


def dedup_articles(articles: List[dict]) -> List[dict]:
    """동일 url 중복 제거. 입력 순서 유지."""
    seen = set()
    result = []
    for a in articles:
        url = a.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        result.append(a)
    return result
