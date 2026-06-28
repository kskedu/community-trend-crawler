"""기사 정규화 / 보안 유틸.

네이버 뉴스 검색 API 응답(또는 fixture)을 안전한 article dict로 변환한다.
- title/description의 <b> 등 HTML 태그 제거 + 엔티티 언이스케이프
- URL은 http/https만 허용 (javascript:/data: 등 차단)
- press는 도메인 추정, 실패 시 "출처 미상"
- snippet 길이 상한
- thumbnail은 P0에서 항상 null (OG 수집은 P1)
- 기사 본문 전문은 저장하지 않음 (title/snippet만)
"""
import html
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

SNIPPET_MAX = 150
_TAG_RE = re.compile(r"<[^>]+>")

# 도메인 → 언론사명 추정 (P0 최소셋, 미스매치 시 도메인/"출처 미상" fallback)
_PRESS_BY_HOST = {
    "yna.co.kr": "연합뉴스",
    "yonhapnews.co.kr": "연합뉴스",
    "chosun.com": "조선일보",
    "donga.com": "동아일보",
    "joongang.co.kr": "중앙일보",
    "hani.co.kr": "한겨레",
    "khan.co.kr": "경향신문",
    "hankyung.com": "한국경제",
    "mk.co.kr": "매일경제",
    "sedaily.com": "서울경제",
    "mt.co.kr": "머니투데이",
    "edaily.co.kr": "이데일리",
    "newsis.com": "뉴시스",
    "news1.kr": "뉴스1",
    "kbs.co.kr": "KBS",
    "imbc.com": "MBC",
    "sbs.co.kr": "SBS",
    "ytn.co.kr": "YTN",
    "jtbc.co.kr": "JTBC",
    "mbn.co.kr": "MBN",
    "zdnet.co.kr": "ZDNet Korea",
    "inews24.com": "아이뉴스24",
    "etnews.com": "전자신문",
    "fnnews.com": "파이낸셜뉴스",
    "asiae.co.kr": "아시아경제",
    "heraldcorp.com": "헤럴드경제",
    "kmib.co.kr": "국민일보",
    "seoul.co.kr": "서울신문",
    "munhwa.com": "문화일보",
    "hankookilbo.com": "한국일보",
    "segye.com": "세계일보",
    "ohmynews.com": "오마이뉴스",
    "pressian.com": "프레시안",
}

PRESS_UNKNOWN = "출처 미상"


def strip_tags(text: Optional[str]) -> str:
    """HTML 태그 제거 + 엔티티 언이스케이프. None 안전."""
    if not text:
        return ""
    no_tags = _TAG_RE.sub("", text)
    unescaped = html.unescape(no_tags)
    return unescaped.strip()


def safe_url(url: Optional[str]) -> Optional[str]:
    """http/https URL만 허용. 그 외(javascript:, data:, 빈값, 파싱불가)는 None."""
    if not url or not isinstance(url, str):
        return None
    u = url.strip()
    try:
        parsed = urlparse(u)
    except Exception:
        return None
    if parsed.scheme.lower() not in ("http", "https"):
        return None
    if not parsed.netloc:
        return None
    return u


def guess_press(*urls: Optional[str]) -> str:
    """주어진 URL들의 호스트로 언론사명 추정. 실패 시 도메인, 그래도 없으면 '출처 미상'."""
    for url in urls:
        safe = safe_url(url)
        if not safe:
            continue
        host = urlparse(safe).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        # 정확 매칭 우선, 그다음 suffix 매칭
        if host in _PRESS_BY_HOST:
            return _PRESS_BY_HOST[host]
        for known_host, name in _PRESS_BY_HOST.items():
            if host == known_host or host.endswith("." + known_host):
                return name
        # 매칭 실패: 도메인 자체를 출처로 노출 (빈 값보단 정보가 있음)
        return host
    return PRESS_UNKNOWN


def clamp_snippet(text: Optional[str], limit: int = SNIPPET_MAX) -> str:
    s = strip_tags(text)
    if len(s) <= limit:
        return s
    return s[:limit].rstrip() + "…"


def parse_pubdate(raw: Optional[str]) -> Optional[str]:
    """네이버 pubDate(RFC1123 등) → ISO8601(UTC). 실패 시 None."""
    if not raw:
        return None
    raw = raw.strip()
    # RFC 1123: 'Mon, 16 Jun 2026 12:00:00 +0900'
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError):
            continue
    # 이미 ISO인 경우
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        return None


def normalize_article(raw: dict) -> Optional[dict]:
    """네이버 뉴스 item(또는 fixture) → 안전한 article dict.

    URL이 유효하지 않으면 None(드롭).
    기사 본문 전문은 저장하지 않는다. title/snippet만.
    """
    if not isinstance(raw, dict):
        return None

    # 네이버는 link(네이버 재가공), originallink(원문) 둘 다 줄 수 있음.
    url = safe_url(raw.get("originallink")) or safe_url(raw.get("link")) or safe_url(raw.get("url"))
    if not url:
        return None

    title = strip_tags(raw.get("title"))
    if not title:
        return None

    return {
        "title": title,
        "url": url,
        "press": guess_press(raw.get("originallink"), raw.get("link"), url),
        "thumbnail": None,  # P0: null 고정. OG 수집은 P1.
        "published_at": parse_pubdate(raw.get("pubDate") or raw.get("published_at")),
        "snippet": clamp_snippet(raw.get("description") or raw.get("snippet")),
    }
