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
import unicodedata
from datetime import datetime, timezone
from typing import Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

SNIPPET_MAX = 150
_TAG_RE = re.compile(r"<[^>]+>")

# 기사 언어 안전장치. Naver News API에는 기사 언어 metadata가 없으므로 title과
# description/snippet의 실제 문자 근거를 함께 본다. 한국어 기사에 영문 고유명사가 많은
# 경우를 보존하기 위해 한글이 하나라도 있으면 이 gate에서는 차단하지 않는다. 반대로
# 한글이 전혀 없는 짧은 브랜드명/약어도 근거 부족으로 허용한다. 두 필드가 모두 충분히
# 길거나, 한 필드만 있어도 외국 문자 근거가 강할 때만 고신뢰 외국어로 판정한다.
_LANG_FIELD_MIN_LETTERS = 10
_LANG_COMBINED_MIN_LETTERS = 30
_LANG_SINGLE_FIELD_MIN_LETTERS = 40
_LANG_FOREIGN_SCRIPT_MIN_LETTERS = 12
_LANG_ACCENTED_LATIN_MIN_LETTERS = 3

# 검색/랭킹의 주제 근거가 될 수 없는 기사 섹션·편집 라벨. 원문 title은 그대로
# 보존하고, relevance/phrase/grounding 판정용 view에서 선두 대괄호 prefix만 제거한다.
# 작은 의미 범주 집합으로 한정해 고유 주체(`[삼성전자]`)를 지우지 않는다.
GENERIC_NEWS_SECTION_LABELS = frozenset({
    "날씨", "뉴스", "경제", "정치", "사회", "스포츠", "연예",
})
_TITLE_FORMAT_PREFIX_WORDS = frozenset({
    "속보", "단독", "종합", "포토", "영상", "굿모닝", "오늘의",
})
_LEADING_BRACKET_PREFIX_RE = re.compile(r"^\s*\[([^\[\]]{1,30})\]\s*")
_PREFIX_TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9]+")


def title_evidence_text(title: Optional[str]) -> str:
    """Return a title view with leading section/format bracket labels removed.

    Only recognized category/format-only prefixes are stripped, repeatedly. The
    stored/displayed original title is never mutated. Unknown bracketed subjects
    remain evidence.
    """
    evidence = title or ""
    while True:
        match = _LEADING_BRACKET_PREFIX_RE.match(evidence)
        if not match:
            break
        tokens = [t.lower() for t in _PREFIX_TOKEN_RE.findall(match.group(1))]
        allowed = GENERIC_NEWS_SECTION_LABELS | _TITLE_FORMAT_PREFIX_WORDS
        if not tokens or not all(t in allowed for t in tokens):
            break
        evidence = evidence[match.end():]
    return evidence.strip()

# === description hygiene (이미지 캡션/사진 설명/출처 문구 정제, 2026-07-04) ===============
# 문제: Naver News description에 "com AI로 생성된 이미지 [사진=챗GPT] 1년 넘게..."처럼
# 이미지 캡션/사진 설명 문구가 섞여 키워드 소개글(representative_summary)에 그대로
# 노출됨. title/기사 evidence는 그대로 두고(안전), description만 "요약 재료로 쓸 수
# 있는지" 별도 판정한다 — low_quality_news(뉴스 evidence gate) 판정과는 무관.
_CAPTION_PHRASES = (
    "AI로 생성된 이미지",
    "연합뉴스 자료사진",
    "자료사진",
    "게티이미지",
    "화면 캡처",
    "본문 이미지",
    "캡처",
)
# 브래킷 안에 "사진"/"이미지"/"출처"/"캡처" 같은 캡션 마커 단어가 있을 때만 캡션/출처
# 대괄호로 본다("=" 유무와 무관 — "[사진=챗GPT]"뿐 아니라 "[사진 : 챗GPT]"/"[사진 제공
# 챗GPT]"도 포섭). "[단독]"/"[속보]"/"[Q&A]"/"[AI 기본법]"처럼 마커 단어가 없는 일반
# 기사 태그성 대괄호는 캡션으로 오판하지 않는다(Codex review-only P2 1·2차, 2026-07-04).
_CAPTION_MARKER_WORDS = ("사진", "이미지", "출처", "캡처")
_CAPTION_BRACKET_RE = re.compile(
    r"\[[^\[\]]{0,60}(?:" + "|".join(_CAPTION_MARKER_WORDS) + r")[^\[\]]{0,60}\]"
)
_CAPTION_INLINE_RE = re.compile(r"(?:사진|이미지|출처)\s*=\s*[^\s,.\]]{1,30}")
# "com AI로 생성된 이미지 [사진=챗GPT] ..."처럼 캡션이 문장 맨 앞에 붙어 시작하는
# 경우에만, 그 앞의 도메인 파편(예: "com")을 함께 제거한다. lookahead를 "파편 뒤에
# 알려진 캡션 phrase 문자열이 정확히 이어지는 경우"로만 한정한다 — 단순히 "["가
# 바로 온다는 조건까지 허용하면 "AI [사진=...] 기술 발전으로...", "5G [사진=...]
# 상용화 이후..."처럼 실제 주제 단어(AI/5G) 뒤에 캡션 브래킷이 곧장 붙는 정상
# 문장까지 도메인 파편으로 오인해 잘라먹는다(Codex review-only P2 5차, 2026-07-04).
# "파편+브래킷"만 있고 알려진 phrase가 없는 극소수 변형은 파편이 약간 남을 수
# 있지만, 정상 문장 선두 단어가 잘리는 것보다 훨씬 안전한 실패 모드다. 브래킷은
# 항상 통째로(부분 소비 없이) 먼저 제거하므로 "[캡처 챗GPT]"처럼 브래킷 안에
# phrase가 포함된 경우에도 "챗GPT]" 같은 잔여물이 남지 않는다(P2 3차).
_LEADING_DOMAIN_FRAGMENT_RE = re.compile(
    r"^[A-Za-z0-9]{1,10}(?:\.[A-Za-z0-9]{1,10})*\s+"
    r"(?=(?:" + "|".join(re.escape(p) for p in _CAPTION_PHRASES) + r"))"
)
DESC_MIN_LEN = 8

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


def _is_hangul_letter(ch: str) -> bool:
    code = ord(ch)
    return (
        0xAC00 <= code <= 0xD7A3      # Hangul syllables
        or 0x1100 <= code <= 0x11FF   # Hangul Jamo
        or 0x3130 <= code <= 0x318F   # Compatibility Jamo
        or 0xA960 <= code <= 0xA97F   # Jamo Extended-A
        or 0xD7B0 <= code <= 0xD7FF   # Jamo Extended-B
    )


def _language_counts(text: Optional[str]) -> dict:
    """언어 판정용 문자 수. 도메인/언론사명은 의도적으로 보지 않는다."""
    value = unicodedata.normalize("NFC", strip_tags(text))
    letters = hangul = foreign_script = accented_latin = 0
    for ch in value:
        if not unicodedata.category(ch).startswith("L"):
            continue
        letters += 1
        if _is_hangul_letter(ch):
            hangul += 1
            continue
        name = unicodedata.name(ch, "")
        if "LATIN" in name:
            if ord(ch) > 127:
                accented_latin += 1
        else:
            # Han/Kana/Thai/Cyrillic 등. 한국 기사 제목의 '中' 같은 한 글자는 아래
            # 최소 문자 수에 못 미쳐 차단 근거가 되지 않는다.
            foreign_script += 1
    return {
        "letters": letters,
        "hangul": hangul,
        "foreign_script": foreign_script,
        "accented_latin": accented_latin,
    }


def is_high_confidence_foreign_article(
    title: Optional[str], description: Optional[str]
) -> bool:
    """title/description 근거가 충분한, 고신뢰 비한국어 기사만 True.

    fail-open 조건:
    - 어느 필드든 한글이 있으면 한국어/혼합 기사로 보존한다.
    - 짧은 제목·약어·브랜드명 또는 설명 결측은 차단하지 않는다.

    차단 조건(모두 한글 0 전제):
    - title과 description이 각각 충분히 긴 외국어 텍스트이거나,
    - 한 필드뿐이어도 긴 영문 문장, 다수의 비라틴 문자, 또는 베트남어처럼
      accented Latin 근거가 반복되는 경우.
    """
    fields = [_language_counts(title), _language_counts(description)]
    if any(field["hangul"] for field in fields):
        return False

    substantive = [f for f in fields if f["letters"] >= _LANG_FIELD_MIN_LETTERS]
    total_letters = sum(f["letters"] for f in fields)
    total_foreign_script = sum(f["foreign_script"] for f in fields)
    total_accented_latin = sum(f["accented_latin"] for f in fields)

    if len(substantive) >= 2 and total_letters >= _LANG_COMBINED_MIN_LETTERS:
        return True
    if total_foreign_script >= _LANG_FOREIGN_SCRIPT_MIN_LETTERS:
        return True
    if (
        total_accented_latin >= _LANG_ACCENTED_LATIN_MIN_LETTERS
        and total_letters >= _LANG_COMBINED_MIN_LETTERS
    ):
        return True
    return total_letters >= _LANG_SINGLE_FIELD_MIN_LETTERS


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


def clean_description(text: Optional[str]) -> Tuple[str, float, Optional[str], bool]:
    """description → (clean_description, quality_score(0~1), drop_reason, is_usable).

    - 캡션/사진 설명 패턴이 없으면 원문 그대로 통과(quality=1.0).
    - 패턴이 있으면 캡션 구간만 제거하고 남은 문장을 clean_description으로 채택
      (quality=0.6). 제거 후 남은 문장이 DESC_MIN_LEN 미만이면 부자연스러운
      결과이므로 clean_description=""(요약 재료에서 제외, drop_reason="caption_only").
    - description 자체가 없으면 drop_reason="empty_description".
    """
    raw = strip_tags(text)
    if not raw:
        return "", 0.0, "empty_description", False

    had_caption = (
        bool(_CAPTION_BRACKET_RE.search(raw))
        or bool(_CAPTION_INLINE_RE.search(raw))
        or any(p in raw for p in _CAPTION_PHRASES)
    )
    if not had_caption:
        return raw, 1.0, None, True

    # 캡션이 문장 맨 앞에서 시작할 때만 그 앞 도메인 파편을 함께 제거(위 정규식 설명 참고).
    lead_match = _LEADING_DOMAIN_FRAGMENT_RE.match(raw)
    working = raw[lead_match.end():] if lead_match else raw

    # 브래킷을 항상 통째로 먼저 제거해야 "[캡처 챗GPT]"처럼 브래킷 안에 phrase가 낀
    # 경우에도 부분 소비 없이 안전하게 사라진다.
    cleaned = _CAPTION_BRACKET_RE.sub(" ", working)
    cleaned = _CAPTION_INLINE_RE.sub(" ", cleaned)
    for phrase in _CAPTION_PHRASES:
        cleaned = cleaned.replace(phrase, " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,-")

    if len(cleaned) < DESC_MIN_LEN:
        return "", 0.0, "caption_only", False
    return cleaned, 0.6, None, True


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

    description_raw = raw.get("description") or raw.get("snippet")
    if is_high_confidence_foreign_article(title, description_raw):
        logger.debug("[news] 고신뢰 외국어 기사 제외: url=%s", url)
        return None
    clean_desc, desc_quality, desc_drop_reason, desc_usable = clean_description(description_raw)

    return {
        "title": title,
        "url": url,
        "press": guess_press(raw.get("originallink"), raw.get("link"), url),
        "thumbnail": None,  # P0: null 고정. OG 수집은 P1.
        "published_at": parse_pubdate(raw.get("pubDate") or raw.get("published_at")),
        "snippet": clamp_snippet(description_raw),
        # === description hygiene(2026-07-04) — snippet은 내부 relevance/clustering 신호로
        # 그대로 두고(호환 유지), 노출용 재료는 아래 필드로 별도 판정한다. ===
        "clean_description": clamp_snippet(clean_desc) if clean_desc else "",
        "description_quality_score": desc_quality,
        "description_drop_reason": desc_drop_reason,
        "is_description_usable_for_summary": desc_usable,
    }
