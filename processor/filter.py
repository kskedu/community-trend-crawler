import re
from typing import List
from models import Post

# 제목에 포함 시 제외할 키워드 (공지/운영 관련)
BLOCK_KEYWORDS = [
    "공지", "공지사항", "안내", "규칙", "이용규칙", "이용안내",
    "비밀번호", "권장", "필독", "운영", "운영진", "관리자",
    "점검", "서버점검", "서비스점검",
    "이벤트 안내", "당첨", "정책",
    "[공지]", "[안내]", "[필독]", "[운영]", "[이벤트]",
    "투표 참여", "설문",
]

# 제목이 이 패턴과 일치하면 제외 (앞부분에 공지 표시가 있는 경우)
BLOCK_PATTERNS = [
    r'^\[공지\]',
    r'^\[안내\]',
    r'^\[필독\]',
    r'^\[운영\]',
    r'^공지[\s:]',
    r'^안내[\s:]',
]

_compiled = [re.compile(p, re.IGNORECASE) for p in BLOCK_PATTERNS]


def is_notice(title: str) -> bool:
    t = title.strip()
    for kw in BLOCK_KEYWORDS:
        if kw in t:
            return True
    for pattern in _compiled:
        if pattern.search(t):
            return True
    return False


def filter_notices(posts: List[Post]) -> List[Post]:
    """공지/안내 게시글 제외"""
    return [p for p in posts if not is_notice(p.title)]
