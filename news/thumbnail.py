"""기사 썸네일(og:image / twitter:image) 수집 — 이미지 URL만 optional 저장.

설계/안전 계약:
- 기사 본문은 절대 저장하지 않는다. 메타 태그에서 이미지 URL만 추출한다.
- 이미지 파일을 다운로드/저장하지 않는다(R2 등). URL 문자열만 다룬다.
- DDL 무변경: article item 의 기존 optional 필드 `thumbnail` 에 URL 을 채운다.
- 수집 우선순위: og:image → twitter:image.
- 저장 거부: http(https 만 허용, mixed content 방지) / data: / base64 / 과도하게 긴 URL.
- 상대경로 이미지 URL 은 기사 URL 기준 절대경로로 변환한다.
- 캐시 우선: 이전 news_top row 의 같은 article URL thumbnail 을 재사용하고,
  캐시에 없는 URL 만 신규 GET 한다(같은 run 내 URL memoization 포함).
- 실패/timeout/차단은 조용히 None(thumbnail 생략). upsert 를 막지 않는다.
- movement 로직과 섞지 않는다(이 모듈은 thumbnail 전용).
"""
import ipaddress
import logging
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests

logger = logging.getLogger(__name__)

# 수집 파라미터 (main.py 후처리에서 주입 가능, 기본값은 보수적으로)
FETCH_TIMEOUT = 2.0          # 요청 timeout(초) — 짧게
MAX_CONCURRENCY = 5          # 동시 요청 상한(4~6)
MAX_URL_LEN = 1000           # 저장 허용 thumbnail URL 길이 상한
MAX_HTML_BYTES = 512 * 1024  # head 영역만 보면 충분 — 본문 통째 읽지 않도록 상한
USER_AGENT = (
    "Mozilla/5.0 (compatible; StartHubBot/1.0; +https://starthub.sk-aistudio.com)"
)

# og:image / twitter:image content 추출 (속성 순서 무관). DOTALL 로 멀티라인 태그 대응.
_META_OG = re.compile(
    r'<meta[^>]+(?:property|name)\s*=\s*["\']og:image(?::url)?["\'][^>]*>',
    re.IGNORECASE | re.DOTALL,
)
_META_TW = re.compile(
    r'<meta[^>]+(?:name|property)\s*=\s*["\']twitter:image(?::src)?["\'][^>]*>',
    re.IGNORECASE | re.DOTALL,
)
_CONTENT = re.compile(r'content\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)


def is_acceptable_thumbnail(url: Optional[str]) -> bool:
    """저장 허용 thumbnail URL 인지 검사: https 만, data/base64 거부, 길이 상한."""
    if not url or not isinstance(url, str):
        return False
    u = url.strip()
    if not u or len(u) > MAX_URL_LEN:
        return False
    lowered = u.lower()
    if lowered.startswith("data:") or "base64," in lowered:
        return False
    try:
        parsed = urlparse(u)
    except Exception:
        return False
    # https 만 허용 (http 썸네일은 mixed content 로 프론트에서 차단됨)
    if parsed.scheme.lower() != "https":
        return False
    if not parsed.netloc:
        return False
    return True


def extract_thumbnail(html: str, base_url: str) -> Optional[str]:
    """HTML(메타 영역)에서 og:image → twitter:image 추출 후 절대 URL 로 정규화.

    순수 함수(네트워크 I/O 없음). 허용 불가(http/data/base64/과긴)면 None.
    상대경로는 base_url 기준 절대경로로 변환.
    """
    if not html or not isinstance(html, str):
        return None
    # 복수 og:image/twitter:image 가 있을 수 있으므로 모든 매치를 순회하며 첫 유효 URL 채택.
    # (첫 og 가 http/data/과긴으로 거부돼도 뒤의 유효 https 를 놓치지 않게.)
    for pattern in (_META_OG, _META_TW):
        for m in pattern.finditer(html):
            c = _CONTENT.search(m.group(0))
            if not c:
                continue
            raw = c.group(1).strip()
            if not raw:
                continue
            # 상대경로 → 절대경로 (base_url 기준). 이미 절대면 그대로.
            try:
                absolute = urljoin(base_url, raw)
            except Exception:
                continue
            if is_acceptable_thumbnail(absolute):
                return absolute
    return None


def _is_public_host(host: str) -> bool:
    """host 가 공개 인터넷 주소인지(SSRF 방어). loopback/사설/link-local/예약 차단.

    - IP 리터럴이면 그 IP 를 직접 검사.
    - 도메인이면 resolve 후 모든 결과가 공개여야 통과(부분 사설 차단).
    - resolve 실패/예외는 False(요청 안 함).
    """
    if not host:
        return False
    host = host.strip().rstrip(".").lower()
    if host in ("localhost",):
        return False

    def _ip_ok(ip_str: str) -> bool:
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        return not (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        )

    # IP 리터럴 직접 검사
    try:
        ipaddress.ip_address(host)
        return _ip_ok(host)
    except ValueError:
        pass
    # 도메인 → resolve 후 모든 주소 공개 검증
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    addrs = {info[4][0] for info in infos}
    if not addrs:
        return False
    return all(_ip_ok(a) for a in addrs)


def fetch_thumbnail(url: str, timeout: float = FETCH_TIMEOUT) -> Optional[str]:
    """기사 URL 을 GET 해 메타에서 thumbnail 추출. 실패/timeout/차단 시 None.

    본문을 통째로 읽지 않도록 응답을 일부(MAX_HTML_BYTES)만 읽는다.
    """
    if not url or not isinstance(url, str):
        return None
    # SSRF 방어: 요청 대상 host 가 공개 주소가 아니면(localhost/사설/link-local/metadata) 거부.
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return None
    if not _is_public_host(host):
        logger.debug("[thumbnail] '%s' 비공개/해석불가 host → 요청 거부", url)
        return None

    resp = None
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
            stream=True,
            # SSRF: 리다이렉트 자동 추적 금지 — _is_public_host 는 최초 host 만 검증하므로
            # 공개 URL 이 302 로 내부주소(127.0.0.1/169.254.169.254 등)를 가리켜도 따라가지 않게.
            allow_redirects=False,
        )
        # 3xx 는 추적하지 않고 종료(메타 없음으로 처리). 본문 GET 만 og 파싱 대상.
        if resp.is_redirect or resp.is_permanent_redirect or 300 <= resp.status_code < 400:
            return None
        resp.raise_for_status()
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "html" not in ctype and ctype:
            return None  # HTML 아니면 메타 없음 — 본문 다운로드 방지
        # 전체 deadline 안에서 chunk 단위로 head 영역(MAX_HTML_BYTES)까지만 읽는다.
        # (느린 서버가 timeout 미만 간격으로 흘려도 무한정 붙잡히지 않게.)
        deadline = time.monotonic() + max(0.5, float(timeout))
        buf = bytearray()
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                buf.extend(chunk)
            if len(buf) >= MAX_HTML_BYTES or time.monotonic() > deadline:
                break
        html = bytes(buf).decode(resp.encoding or "utf-8", errors="ignore")
        return extract_thumbnail(html, url)
    except Exception as e:
        logger.debug("[thumbnail] '%s' 수집 실패 → 생략: %s", url, e)
        return None
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass


def build_thumbnail_cache(previous_issues: Optional[dict]) -> Dict[str, str]:
    """이전 news_top issues → {article_url: thumbnail} 캐시.

    movement 로직과 독립. 이전 row 없음/파싱 불가/캐시 없음은 빈 dict("캐시 없음")로 처리.
    URL 매칭은 정규화(_norm_url) 기준. 허용 가능한 thumbnail 만 캐시에 담는다.
    """
    cache: Dict[str, str] = {}
    if not isinstance(previous_issues, dict):
        return cache
    keywords = previous_issues.get("keywords")
    if not isinstance(keywords, list):
        return cache
    for kw in keywords:
        if not isinstance(kw, dict):
            continue
        articles = kw.get("articles")
        if not isinstance(articles, list):
            continue
        for art in articles:
            if not isinstance(art, dict):
                continue
            url = _norm_url(art.get("url"))
            thumb = art.get("thumbnail")
            if url and isinstance(thumb, str) and is_acceptable_thumbnail(thumb):
                cache.setdefault(url, thumb)
    return cache


def _norm_url(url: Optional[str]) -> Optional[str]:
    """URL 매칭용 단순 정규화: 공백 trim, trailing slash 제거, 빈 값 방어.

    normalizer.safe_url 의 결과(http/https 절대 URL)와 호환되는 단순 비교 키.
    과도한 정규화(쿼리 정렬 등)는 하지 않는다 — 동일성 오판 방지.
    """
    if not url or not isinstance(url, str):
        return None
    u = url.strip()
    if not u:
        return None
    # trailing slash 한 개만 정규화(경로 끝). 쿼리/프래그먼트는 건드리지 않음.
    if u.endswith("/"):
        u = u[:-1]
    return u


def enrich_issue_thumbnails(
    current_issues: dict,
    previous_issues: Optional[dict] = None,
    *,
    timeout: float = FETCH_TIMEOUT,
    concurrency: int = MAX_CONCURRENCY,
) -> dict:
    """current_issues.articles 에 thumbnail 주입(in-place 후 동일 dict 반환).

    1) 이전 news_top 캐시(같은 URL)의 thumbnail 우선 재사용.
    2) 캐시에 없고 기존 thumbnail 도 없는 유효 URL 만 신규 fetch.
    3) 같은 run 내 URL memoization 으로 중복 GET 방지.
    실패는 thumbnail 생략(None 유지). 본 함수는 upsert 를 막지 않는다.
    """
    if not isinstance(current_issues, dict):
        return current_issues
    keywords = current_issues.get("keywords")
    if not isinstance(keywords, list):
        return current_issues

    prev_cache = build_thumbnail_cache(previous_issues)
    run_memo: Dict[str, Optional[str]] = {}  # 같은 run 내 URL → thumbnail(또는 None)

    # 1차 패스: 캐시 재사용 적용 + 신규 fetch 대상 URL 수집
    to_fetch: List[str] = []
    for kw in keywords:
        if not isinstance(kw, dict):
            continue
        articles = kw.get("articles")
        if not isinstance(articles, list):
            continue
        for art in articles:
            if not isinstance(art, dict):
                continue
            # 이미 유효한 thumbnail 이 있으면 그대로 둠(재수집 안 함)
            existing = art.get("thumbnail")
            if isinstance(existing, str) and is_acceptable_thumbnail(existing):
                continue
            key = _norm_url(art.get("url"))
            if not key:
                continue
            cached = prev_cache.get(key)
            if cached:
                art["thumbnail"] = cached  # 이전 row 재사용 → GET 안 함
                run_memo[key] = cached
                continue
            if key not in run_memo:
                run_memo[key] = None     # placeholder — 신규 fetch 대상
                to_fetch.append(key)

    # 2차: 캐시 미스 URL 만 concurrency 제한으로 신규 fetch
    if to_fetch:
        workers = max(1, min(int(concurrency), MAX_CONCURRENCY))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(lambda u: (u, fetch_thumbnail(u, timeout)), to_fetch))
        for u, thumb in results:
            run_memo[u] = thumb if (thumb and is_acceptable_thumbnail(thumb)) else None

    # 3차: memo 결과를 articles 에 반영(신규 fetch 분)
    for kw in keywords:
        if not isinstance(kw, dict):
            continue
        articles = kw.get("articles")
        if not isinstance(articles, list):
            continue
        for art in articles:
            if not isinstance(art, dict):
                continue
            existing = art.get("thumbnail")
            if isinstance(existing, str) and is_acceptable_thumbnail(existing):
                continue
            key = _norm_url(art.get("url"))
            if not key:
                continue
            thumb = run_memo.get(key)
            if thumb:
                art["thumbnail"] = thumb
            # 실패면 기존 None 유지(필드 생략 아님 — normalizer 가 None 으로 둠)

    return current_issues
