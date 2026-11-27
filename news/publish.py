"""news_top 게시용 payload — 목록 1행 · 키워드별 상세 행 · 저장용 news_top(B4 · F7-NF-01).

StartHub PRELAUNCH_SUPABASE_EFFICIENCY_V1 B4(C안): 홈은 목록만, 키워드 상세는 클릭할 때 그 키워드만 받는다.
같은 news_issue_cache 표에 행을 더한다(운영 DB·RLS 변경 없음, anon 은 이 표의 모든 source 를 읽는다).

- news_top            : 지금과 같은 전체 payload(구버전 Web/PWA/Android 호환). 단 display_articles 가 있는
                        키워드의 중복 articles 는 저장하지 않는다(F7-NF-01 — 모든 클라이언트는 display_articles 를 먼저 쓴다).
- news_top_list       : 목록 표시에 필요한 필드 + snapshot.
- news_top_detail_<n> : rank n(1..DETAIL_MAX_RANK) 키워드의 상세 + 같은 snapshot. rank 수가 고정이라 행이 무한히 쌓이지 않는다.
                        이번 snapshot 에 없는 rank 의 행은 이전 snapshot 값이 남지만, 클라이언트가 snapshot 대조로 버린다.

세 종류는 db.supabase.upsert_news_top_bundle 이 한 문장으로 함께 저장한다(원자적 — 일부만 게시되지 않는다).
순수 함수만 둔다(입력 issues 를 바꾸지 않는다).
"""
import copy
from typing import Dict, List, Optional, Tuple

PAYLOAD_VERSION = 1
LIST_SOURCE = "news_top_list"
DETAIL_SOURCE_PREFIX = "news_top_detail_"
DETAIL_MAX_RANK = 10

LIST_FIELDS = ("rank", "keyword", "display_keyword", "summary", "movement", "rank_delta")
ARTICLE_FIELDS = ("url", "press", "title", "snippet", "clean_description", "thumbnail", "published_at")


def snapshot_of(issues: Dict) -> Dict[str, Optional[str]]:
    """목록·상세를 같은 게시 회차로 묶는 값. generated_at 이 없으면 None(클라이언트는 그때 전체 행을 쓴다)."""
    gen = issues.get("generated_at") if isinstance(issues, dict) else None
    return {"generated_at": gen if isinstance(gen, str) and gen else None}


def _articles_for_detail(kw: Dict) -> List[Dict]:
    arts = kw.get("display_articles")
    if not isinstance(arts, list):
        arts = kw.get("articles") if isinstance(kw.get("articles"), list) else []
    out = []
    for a in arts:
        if isinstance(a, dict):
            out.append({f: a[f] for f in ARTICLE_FIELDS if f in a})
    return out


def build_list_payload(issues: Dict) -> Dict:
    kws = issues.get("keywords") if isinstance(issues, dict) else None
    rows = []
    for k in kws if isinstance(kws, list) else []:
        if isinstance(k, dict):
            rows.append({f: k[f] for f in LIST_FIELDS if f in k})
    return {"v": PAYLOAD_VERSION, "snapshot": snapshot_of(issues), "keywords": rows}


def build_detail_payloads(issues: Dict) -> List[Tuple[str, Dict]]:
    """[(source, payload)] — rank 1..DETAIL_MAX_RANK 만. rank 가 정수가 아니거나 범위 밖이면 그 키워드는 상세 행이 없다
    (클라이언트는 상세 행이 없거나 snapshot 이 다르면 전체 news_top 으로 대신한다)."""
    snap = snapshot_of(issues)
    out = []
    seen = set()
    for k in issues.get("keywords") or []:
        if not isinstance(k, dict):
            continue
        rank = k.get("rank")
        if not isinstance(rank, int) or isinstance(rank, bool) or not (1 <= rank <= DETAIL_MAX_RANK) or rank in seen:
            continue
        seen.add(rank)
        out.append((f"{DETAIL_SOURCE_PREFIX}{rank}", {
            "v": PAYLOAD_VERSION,
            "snapshot": snap,
            "rank": rank,
            "keyword": k.get("keyword"),
            "display_keyword": k.get("display_keyword"),
            "summary": k.get("summary"),
            "display_articles": _articles_for_detail(k),
        }))
    return out


def storage_issues(issues: Dict) -> Dict:
    """저장용 news_top — display_articles 가 배열인 키워드에서만 중복 articles 를 뺀다(F7-NF-01).
    display_articles 가 없는 키워드(구 형식)는 articles 를 그대로 둔다(클라이언트 폴백 경로)."""
    out = copy.deepcopy(issues)
    for k in out.get("keywords") or []:
        if isinstance(k, dict) and isinstance(k.get("display_articles"), list):
            k.pop("articles", None)
    return out
