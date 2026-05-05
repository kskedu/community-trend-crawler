import re
import logging
from typing import List
from models import Post

logger = logging.getLogger(__name__)

# ─── 하드코딩 Fallback ───────────────────────────────────────────────────────
# DB(trend_block_keywords) 조회 실패 시 사용.
# 어드민에서 DB 값을 관리하므로 여기는 최소한만 유지.

_FALLBACK_KEYWORDS = [
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
    # 광고 (2026-05-05 유입)
    "부가!!", "NO상조",
]

_FALLBACK_PATTERNS = [
    r'^\[공지\]', r'^\[안내\]', r'^\[필독\]', r'^\[운영\]',
    r'^공지[\s:]', r'^안내[\s:]',
    r'^AD[^a-zA-Z ]',
    r'#\S+.*#\S+',
    r'[▶▼★◆◇■□●○]{2,}',
    r'(?:SK|KT|LG)\s*번이\s*[A-Z]',  # 통신사 번호이동 광고 (번이+단말명)
    r'SK기변.{0,5}번이|번이기변',      # 통신사 기변/번이 광고
    r'갤S26.{0,10}\d+만|\d+만.{0,10}갤S26',  # 갤럭시S26 가격 광고
    r'5월대란|통신사대란|핸드폰대란',   # 통신사 대란 광고
]

MIN_TITLE_LENGTH = 6

# ─── 런타임 필터 상태 (크롤러 실행당 1회 로드) ───────────────────────────────
_block_keywords: List[str] = []
_compiled_patterns: List[re.Pattern] = []
_loaded = False


def load_filters_from_db() -> bool:
    """Supabase trend_block_keywords 테이블에서 필터 목록 로드.
    성공 시 True, 실패 시 False."""
    global _block_keywords, _compiled_patterns, _loaded
    try:
        from db.supabase import get_client
        client = get_client()
        res = (
            client.table("trend_block_keywords")
            .select("type,value")
            .eq("enabled", True)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return False

        keywords = [r["value"] for r in rows if r["type"] == "keyword"]
        patterns = [r["value"] for r in rows if r["type"] == "pattern"]
        compiled = []
        for p in patterns:
            try:
                compiled.append(re.compile(p, re.IGNORECASE))
            except re.error as e:
                logger.warning("필터 패턴 컴파일 실패 (무시): %s — %s", p, e)

        _block_keywords = keywords
        _compiled_patterns = compiled
        _loaded = True
        logger.info("필터 DB 로드 완료: 키워드 %d개, 패턴 %d개", len(keywords), len(patterns))
        return True
    except Exception as e:
        logger.warning("필터 DB 로드 실패, fallback 사용: %s", e)
        return False


def _ensure_loaded():
    global _block_keywords, _compiled_patterns, _loaded
    if _loaded:
        return
    if not load_filters_from_db():
        # fallback
        _block_keywords = list(_FALLBACK_KEYWORDS)
        _compiled_patterns = [re.compile(p, re.IGNORECASE) for p in _FALLBACK_PATTERNS]
        _loaded = True
        logger.info("필터 fallback 사용: 키워드 %d개, 패턴 %d개",
                    len(_block_keywords), len(_compiled_patterns))


def is_noise(title: str) -> bool:
    _ensure_loaded()
    t = title.strip()

    if len(t) <= MIN_TITLE_LENGTH - 1:
        return True
    for kw in _block_keywords:
        if kw in t:
            return True
    for pattern in _compiled_patterns:
        if pattern.search(t):
            return True
    return False


# 하위 호환 alias
def is_notice(title: str) -> bool:
    return is_noise(title)


def filter_notices(posts: List[Post]) -> List[Post]:
    """광고/공지/노이즈 게시글 제외"""
    return [p for p in posts if not is_noise(p.title)]
