import logging
import time
from datetime import datetime, timezone
from typing import Callable, List, Dict, Optional
from supabase import create_client, Client
from supabase.lib.client_options import SyncClientOptions
import httpx
from postgrest.exceptions import APIError
from models import Post
from config import SUPABASE_URL, SUPABASE_KEY

logger = logging.getLogger(__name__)
_client: Client = None

# DB 요청 1건의 상한(초). 라이브러리 기본 120초는 2026-11-04 장애 때 키워드 저장마다
# 응답 없이 묶여 news_top_only 1회가 13분 걸렸다. 정상 upsert 는 1초 안팎이다.
DB_REQUEST_TIMEOUT_SECONDS = 30
# 멱등 upsert 의 일시 장애 재시도: 1회만, 짧게 쉰 뒤. 무제한 재시도는 하지 않는다.
TRANSIENT_RETRY_DELAY_SECONDS = 3
# 한 실행 안에서 일시 장애로 끝난 DB 호출이 '연속으로' 이 횟수에 이르면 DB 를 '사용 불가'로
# 보고 이후 저장은 호출 없이 실패 처리한다(장애 중 실행이 길게 묶이는 것 방지). 중간에 저장이
# 한 번이라도 성공하면 0 으로 돌아간다(일시 흔들림으로 정상 DB 의 저장을 막지 않는다). 실행마다 초기화.
DB_UNAVAILABLE_AFTER = 2

# 일시 장애로 보는 HTTP 상태: Cloudflare 520~524(오리진 무응답·연결 실패·timeout),
# 게이트웨이 502~504. 4xx·PostgREST JSON 오류(PGRST·SQLSTATE)는 재시도해도 같으므로 제외.
_TRANSIENT_HTTP = {502, 503, 504, 520, 521, 522, 523, 524}


def classify_db_error(exc: BaseException) -> str:
    """DB 예외를 로그·집계용 짧은 분류명으로 바꾼다. 예외 메시지·응답 본문은 보지 않는다.

    반환: http_<상태>(예: http_522) · timeout · network · api_error(PostgREST JSON 오류) · unknown.
    postgrest 는 JSON 이 아닌 오류 응답(Cloudflare HTML 등)을 code=<HTTP 상태 int>,
    details=<응답 원문> 인 APIError 로 던진다 — 원문은 로그에 남기지 않는다.
    """
    if isinstance(exc, APIError):
        code = getattr(exc, "code", None)
        if isinstance(code, int) or (isinstance(code, str) and code.isdigit() and len(code) == 3):
            return f"http_{int(code)}"
        return "api_error"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.TransportError):
        return "network"
    return "unknown"


def is_transient(kind: str) -> bool:
    if kind in ("timeout", "network"):
        return True
    if kind.startswith("http_"):
        try:
            return int(kind[5:]) in _TRANSIENT_HTTP
        except ValueError:
            return False
    return False


class SaveLedger:
    """한 실행의 DB 저장 결과 장부. 실행 요약·종료 코드 판정의 근거다.

    entries: (target, source, status, kind) — status 는 ok | failed | skipped_db_unavailable.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.entries = []
        self.transient_failures = 0

    @property
    def db_unavailable(self) -> bool:
        return self.transient_failures >= DB_UNAVAILABLE_AFTER

    def record(self, target, source, status, kind=None):
        self.entries.append((target, source, status, kind))


ledger = SaveLedger()


def _guarded_write(target: str, source: str, thunk: Callable, retry: bool = True):
    """DB 쓰기 1건을 실행하고 장부에 남긴다. (ok, result) 반환.

    - DB 가 이미 사용 불가로 판정됐으면 호출하지 않는다(skipped_db_unavailable).
    - 일시 장애(522·timeout 등)면 retry=True 일 때 1회만 재시도한다. thunk 는 같은 행을
      다시 보내는 멱등 upsert 여야 한다(행 값은 호출 전에 확정해 둔다).
    - 로그에는 대상·source·분류명만 남긴다(예외 원문·응답 HTML·payload·키 금지).
    """
    if ledger.db_unavailable:
        logger.error("[%s] %s 저장 생략 kind=db_unavailable (이번 실행에서 DB 응답 없음 판정)",
                     source, target)
        ledger.record(target, source, "skipped_db_unavailable", "db_unavailable")
        return False, None
    attempts = 2 if retry else 1
    kind = None
    for attempt in range(1, attempts + 1):
        try:
            result = thunk()
            ledger.transient_failures = 0
            ledger.record(target, source, "ok")
            return True, result
        except (APIError, httpx.HTTPError) as e:
            kind = classify_db_error(e)
            if attempt < attempts and is_transient(kind):
                logger.warning("[%s] %s 일시 오류 kind=%s → %d초 후 1회 재시도",
                               source, target, kind, TRANSIENT_RETRY_DELAY_SECONDS)
                time.sleep(TRANSIENT_RETRY_DELAY_SECONDS)
                continue
            break
    if is_transient(kind):
        ledger.transient_failures += 1
    logger.error("[%s] %s 저장 실패 kind=%s", source, target, kind)
    ledger.record(target, source, "failed", kind)
    return False, None


def get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(
            SUPABASE_URL, SUPABASE_KEY,
            options=SyncClientOptions(postgrest_client_timeout=DB_REQUEST_TIMEOUT_SECONDS),
        )
    return _client


def upsert_posts(posts: List[Post]) -> Optional[int]:
    """posts 테이블에 upsert. 성공 건수 반환, 저장 실패면 None.

    0 과 None 은 다른 상태다. 빈 입력·정상 응답의 빈 data 는 0(정상), API 호출이
    예외로 끝나면 None(저장 실패)이다. postgrest 는 2xx 가 아니면 APIError 를, 통신
    오류는 httpx 예외를 던지므로 "예외 = 실패" 로 판정한다. 호출 밖(행 변환 등)의
    개발 오류는 잡지 않고 그대로 올린다.
    """
    if not posts:
        return 0

    client = get_client()
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = []
    for p in posts:
        rows.append({
            "title": p.title,
            "source_url": p.source_url,
            "source_site": p.source_site,
            "content": p.content,
            "image_url": p.image_url,
            "upvotes": p.upvotes,
            "comments": p.comments,
            "views": p.views,
            "score": p.score,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "collected_at": now_iso,
        })

    ok, res = _guarded_write(
        "community_posts", "posts",
        lambda: client.table("community_posts").upsert(rows, on_conflict="source_url").execute(),
    )
    if not ok:
        return None
    count = len(res.data) if res.data else 0
    logger.info(f"upsert 완료: {count}건")
    return count


def upsert_keywords(source: str, keywords: List[Dict[str, str]]) -> bool:
    """keyword_cache 테이블에 upsert. 빈 입력은 저장하지 않는다(last-good 보존)."""
    if not keywords:
        return False
    client = get_client()
    row = {
        "source": source,
        "keywords": keywords,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    ok, _ = _guarded_write(
        "keyword_cache", source,
        lambda: client.table("keyword_cache").upsert(row, on_conflict="source").execute(),
    )
    return ok


def fetch_news_issues(source: str = "news_top") -> Optional[Dict]:
    """news_issue_cache 의 기존 issues 를 read-only 로 조회(movement 비교용).

    최신 1건(order updated_at desc limit 1)만 가져온다(단일 row 보장이라도 방어적).
    row 없음/이상/조회 실패 → None (상위에서 '기존 없음'으로 처리).
    """
    try:
        client = get_client()
        res = (
            client.table("news_issue_cache")
            .select("issues,updated_at")
            .eq("source", source)
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
        )
        data = getattr(res, "data", None)
        if not data:
            return None
        row = data[0] if isinstance(data, list) else data
        return row.get("issues") if isinstance(row, dict) else None
    except Exception as e:
        logger.warning("[%s] news issues read 실패(movement 비교 생략) kind=%s",
                       source, classify_db_error(e))
        return None


def upsert_news_issues(issues: Dict, source: str = "news_top") -> bool:
    """news_issue_cache 테이블에 upsert (실시간 이슈 브리핑).

    on_conflict='source'(PK)로 단일 행 운영.
    """
    if not issues or not issues.get("keywords"):
        return False
    client = get_client()
    row = {
        "source": source,
        "issues": issues,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    ok, _ = _guarded_write(
        "news_issue_cache", source,
        lambda: client.table("news_issue_cache").upsert(row, on_conflict="source").execute(),
    )
    return ok


def upsert_generic_snapshot(source: str, payload: Dict) -> bool:
    """news_issue_cache 테이블에 임의 payload를 upsert하는 범용 snapshot 저장.

    upsert_news_issues와 달리 "keywords" 필드를 강제하지 않는다 — news_top 브리핑
    계약과 무관한 다른 source(예: naver_press_popular)가 같은 테이블을 재사용할 때 쓴다.
    on_conflict='source'(PK)로 단일 행 운영. payload는 issues 컬럼(jsonb)에 그대로 저장.
    """
    if not source or not isinstance(payload, dict) or not payload:
        return False
    client = get_client()
    row = {
        "source": source,
        "issues": payload,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    ok, _ = _guarded_write(
        "news_issue_cache", source,
        lambda: client.table("news_issue_cache").upsert(row, on_conflict="source").execute(),
    )
    return ok


def record_news_diagnostics(run: Dict, decisions: list) -> bool:
    """뉴스 키워드 진단 이력을 RPC 1회로 원자 적재한다.

    - 테이블 직접 write 금지 — service_role은 이 RPC EXECUTE 권한만 갖는다.
    - 멱등: 동일 run_key면 기존 run_id를 반환하고 decisions를 추가하지 않는다(RPC 계약).
    - 실패해도 news_top 결과를 되돌리거나 실패시키지 않는다 → bool만 반환한다.
      관찰용이라 저장 장부(ledger)·종료 코드에는 넣지 않고, 재시도도 하지 않는다.
      이번 실행에서 DB 가 이미 응답 없음으로 판정됐으면 호출하지 않는다.
    - 로그에 예외 메시지를 남기지 않는다: postgrest/httpx 예외 메시지에는 요청 payload와
      Authorization 헤더가 실릴 수 있다(사용자 확정 §10-1). 분류명만 남긴다.
    """
    if not run:
        return False
    if ledger.db_unavailable:
        logger.warning("[news-diag] 진단 적재 생략 kind=db_unavailable(랭킹 영향 없음)")
        return False
    client = get_client()
    try:
        client.rpc("news_diag_record_run", {
            "p_run": run,
            "p_decisions": decisions,
        }).execute()
        return True
    except Exception as e:
        logger.warning("[news-diag] 진단 적재 실패(랭킹 영향 없음): %s kind=%s",
                       type(e).__name__, classify_db_error(e))
        return False
