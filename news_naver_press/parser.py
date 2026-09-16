"""네이버 언론사별 인기뉴스(popularDay) HTML 파서 — Phase 1: 언론사별 rank1만 수집.

설계 계약:
- 본문/description/snippet은 절대 파싱하지 않는다(제목/URL/썸네일 URL만).
- 개별 box(언론사)가 malformed면 해당 box만 skip한다 — 페이지 전체를 실패시키지 않는다.
- 페이지 자체가 구조적으로 깨졌다고 판단되면(최소 box 수 미달 등) ParseResult.ok=False를
  반환해 상위(collector)가 새 snapshot을 저장하지 않도록 한다.
- 이미지 다운로드는 하지 않는다 — HTML에 이미 있는 thumbnail URL 문자열만 채택한다.
- Naver 랭킹 페이지는 lazy-load라 <img src>가 비어 있고 실제 URL이 data-src에 있는
  경우가 대다수다(운영 실측: src만 보면 83개 중 12개만 채택됨). src 우선, 없으면
  data-src로 폴백한다.
"""
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# 실제 페이지 측정(2026-09-16, 1회 GET): 83개 box, 전부 유효. 미래 변동(언론사 증감)을
# 감안해 너무 빡빡하지 않게 lower bound를 잡는다 — 절반 이하로 줄면 구조 변경/차단 의심.
MIN_VALID_BOX_COUNT = 30

# article_url이 실제 네이버 뉴스 기사 형태인지 확인(피싱/외부 URL 유입 방지).
_ARTICLE_URL_RE = re.compile(
    r"^https://n\.news\.naver\.com/(?:mnews/)?article/\d+/\d+"
)
_PRESS_ID_RE = re.compile(r"/press/(\d+)/ranking")


@dataclass
class PressTopItem:
    press_id: str
    press_name: str
    press_logo: Optional[str]
    rank: int
    article_id: Optional[str]
    title: str
    article_url: str
    thumbnail: Optional[str]
    time_label: Optional[str]


@dataclass
class ParseResult:
    ok: bool
    items: List[PressTopItem] = field(default_factory=list)
    box_count: int = 0
    valid_item_count: int = 0
    malformed_box_count: int = 0
    duplicate_press_count: int = 0
    duplicate_article_count: int = 0
    reason: Optional[str] = None  # ok=False일 때만 채움


def _img_url(img_tag) -> Optional[str]:
    """<img> 에서 실제 로드 URL 추출: src 우선, 없으면 data-src(lazy-load) 폴백."""
    if img_tag is None:
        return None
    for attr in ("src", "data-src"):
        val = img_tag.get(attr)
        if val and isinstance(val, str) and val.strip().startswith("http"):
            return val.strip()
    return None


def _is_valid_article_url(url: Optional[str]) -> bool:
    if not url or not isinstance(url, str):
        return False
    if len(url) > 500:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme != "https":
        return False
    return bool(_ARTICLE_URL_RE.match(url))


def _extract_article_id(article_url: str) -> Optional[str]:
    m = re.search(r"/article/\d+/(\d+)", article_url)
    return m.group(1) if m else None


def _parse_box(box) -> Optional[PressTopItem]:
    """단일 rankingnews_box에서 press 정보 + rank1 item을 추출. 실패 시 None."""
    head = box.select_one(".rankingnews_box_head")
    name_el = box.select_one(".rankingnews_name")
    if not name_el:
        return None
    press_name = name_el.get_text(strip=True)
    if not press_name:
        return None

    press_id = None
    if head is not None:
        href = head.get("href") or ""
        m = _PRESS_ID_RE.search(href)
        if m:
            press_id = m.group(1)
    if not press_id:
        return None

    press_logo = _img_url(box.select_one(".rankingnews_thumb img"))

    li_list = box.select(".rankingnews_list > li")
    if not li_list:
        return None

    rank1_li = None
    for li in li_list:
        num_el = li.select_one(".list_ranking_num")
        if not num_el:
            continue
        num_text = num_el.get_text(strip=True)
        # "1위" 등 접미사가 붙으므로 선두 숫자만 비교(정확히 1인지, "10"/"11" 오매칭 방지).
        m = re.match(r"^(\d+)", num_text)
        if m and m.group(1) == "1":
            rank1_li = li
            break
    if rank1_li is None:
        return None

    title_el = rank1_li.select_one(".list_title")
    if not title_el:
        return None
    title = title_el.get_text(strip=True)
    if not title:
        return None

    article_url = title_el.get("href")
    if not _is_valid_article_url(article_url):
        return None

    thumbnail = _img_url(rank1_li.select_one(".list_img img"))
    time_el = rank1_li.select_one(".list_time")
    time_label = time_el.get_text(strip=True) if time_el else None

    return PressTopItem(
        press_id=press_id,
        press_name=press_name,
        press_logo=press_logo,
        rank=1,
        article_id=_extract_article_id(article_url),
        title=title,
        article_url=article_url,
        thumbnail=thumbnail,
        time_label=time_label,
    )


def parse_popular_day_html(html: str) -> ParseResult:
    """popularDay.naver HTML을 파싱해 언론사별 rank1 item 목록을 반환.

    보수적 파서: box 단위 malformed는 skip, 페이지 전체가 최소 box 수 미달이면
    ok=False(상위에서 snapshot 저장 금지 신호).
    """
    if not html or not isinstance(html, str):
        return ParseResult(ok=False, reason="EMPTY_HTML")

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as e:
        logger.warning("[naver_press] HTML 파싱 실패: %s", type(e).__name__)
        return ParseResult(ok=False, reason="HTML_PARSE_ERROR")

    boxes = soup.select(".rankingnews_box")
    box_count = len(boxes)
    if box_count < MIN_VALID_BOX_COUNT:
        logger.warning(
            "[naver_press] rankingnews_box 개수 비정상(%d < %d) → 구조 변경/차단 의심",
            box_count, MIN_VALID_BOX_COUNT,
        )
        return ParseResult(ok=False, box_count=box_count, reason="STRUCTURE_CHANGED_OR_BLOCKED")

    items: List[PressTopItem] = []
    malformed = 0
    seen_press = set()
    seen_article = set()
    dup_press = 0
    dup_article = 0

    for box in boxes:
        item = _parse_box(box)
        if item is None:
            malformed += 1
            continue
        if item.press_id in seen_press:
            dup_press += 1
            continue
        if item.article_url in seen_article:
            dup_article += 1
            continue
        seen_press.add(item.press_id)
        seen_article.add(item.article_url)
        items.append(item)

    if not items:
        return ParseResult(
            ok=False, box_count=box_count, malformed_box_count=malformed,
            reason="NO_VALID_ITEMS",
        )

    return ParseResult(
        ok=True,
        items=items,
        box_count=box_count,
        valid_item_count=len(items),
        malformed_box_count=malformed,
        duplicate_press_count=dup_press,
        duplicate_article_count=dup_article,
    )
