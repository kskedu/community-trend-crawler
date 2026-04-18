import re
from typing import List
from models import Post

# 제목에 포함 시 제외할 키워드
BLOCK_KEYWORDS = [
    # 공지/운영
    "공지", "공지사항", "안내", "규칙", "이용규칙", "이용안내",
    "비밀번호", "권장", "필독", "운영", "운영진", "관리자",
    "점검", "서버점검", "서비스점검",
    "이벤트 안내", "당첨", "정책",
    "[공지]", "[안내]", "[필독]", "[운영]", "[이벤트]",
    "투표 참여", "설문",
    # 광고/스팸
    "[광고]", "[홍보]", "[협찬]", "[PR]", "[AD]",
    "리딩방", "단톡방",
    "즉시입금", "바로입금", "당일입금", "현금입금",
    "무조건", "선착순",
]

# 정규식 패턴으로 제외
BLOCK_PATTERNS = [
    # 공지 앞머리
    r'^\[공지\]',
    r'^\[안내\]',
    r'^\[필독\]',
    r'^\[운영\]',
    r'^공지[\s:]',
    r'^안내[\s:]',
    # 광고 앞머리 (AD로 시작)
    r'^AD[\[\s#(]',
    # 해시태그 2개 이상
    r'#\S+.*#\S+',
    # 특수문자 2개 이상 연속
    r'[▶▼★◆◇■□●○]{2,}',
]

_compiled = [re.compile(p, re.IGNORECASE) for p in BLOCK_PATTERNS]

# 제목 최소 길이
MIN_TITLE_LENGTH = 6


def is_noise(title: str) -> bool:
    t = title.strip()

    # 제목 길이 필터 (5자 이하)
    if len(t) <= MIN_TITLE_LENGTH - 1:
        return True

    for kw in BLOCK_KEYWORDS:
        if kw in t:
            return True
    for pattern in _compiled:
        if pattern.search(t):
            return True
    return False


# 하위 호환 alias
def is_notice(title: str) -> bool:
    return is_noise(title)


def filter_notices(posts: List[Post]) -> List[Post]:
    """광고/공지/노이즈 게시글 제외"""
    return [p for p in posts if not is_noise(p.title)]
