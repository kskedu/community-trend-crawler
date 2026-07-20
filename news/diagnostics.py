"""뉴스 키워드 진단 수집기 — 랭킹 판정 이력을 관찰만 하고 기록한다.

설계 계약(사용자 확정 2026-07-16):
- **순수 관찰**: 이 모듈의 어떤 오류도 랭킹 결과를 바꾸거나 실행을 중단시키지 않는다.
  호출부는 반드시 `_safe_diag(target, thunk)` 경계를 통과한다(main.py).
- **snapshot 소유권**: PassSnapshot은 호출자가 생성해 명시적으로 전달한다.
  암묵적 "현재 활성 snapshot" 전역 상태를 두지 않는다.
- **degraded 격리**: degraded/errors는 PassSnapshot 로컬. commit된 snapshot의 것만 run으로 승격.
  폐기된 pass의 오류는 최종 이력을 오염시키지 않는다.
- **부분 이력 금지**: 채택 snapshot이 degraded면 RPC를 호출하지 않는다(진단 누락 > 잘못된 이력).
- **로그 위생**: 예외는 `type(e).__name__`만 남긴다. 메시지/payload/기사 본문/secret 금지.

카운트 invariant:
    candidate_count = len(decisions) = selected + not_selected + rule_excluded
NO_REPRESENTATIVE는 selected의 부분집합이며 별도 합산하지 않는다.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# 랭킹/게이트 판정 규칙의 버전(사용자 확정 2026-07-17). git_sha(어느 커밋이 실행됐는지)와는
# 별개 개념 — 이 값은 규칙의 "의미"가 바뀔 때만 사람이 수동으로 올린다.
# bump 기준(단일 권위 출처, 다른 곳에 복제하지 않는다):
#   MAJOR: 기존 Admin 해석과 호환 안 되는 계약 변경(result_status 의미 변경,
#          기존 reason_code 삭제·의미 변경, candidate_count 산식 변경)
#   MINOR: 기존 계약 유지 + 새 판정 규칙/reason_code 추가, 임계값 변경으로 결과
#          분포가 의미 있게 달라지는 경우
#   PATCH: 규칙 의미는 유지한 채 판정 구현 오류 수정, 진단 기록 정확성만 보정
#   변경 없음: 로그/테스트/리팩터링/성능 개선/source 매핑처럼 판정 결과와 규칙
#              의미가 변하지 않는 작업(이번 PR 전체가 여기 해당 — bump 없이 최초 도입만)
RULES_VERSION = "1.1.0"  # 1.0.0→1.1.0(MINOR): entity-role 정제·cohesion·B2 gate 추가(2026-07).

# selection_diagnostics_v1: thresholds JSONB에 버전 격리 namespace로 싣는 실행단위 진단.
# 기존 thresholds 필드(collected_candidate_count 등)와 평면 혼합하지 않는다(하위호환).
# migration 없이 기존 JSONB 컬럼만 확장한다. byte 상한 초과/직렬화 실패 시 이 namespace만
# 제거하고 진단 본체는 보존한다(build_payload). Admin에서 빈번한 필터·집계·인덱싱이 실제
# 필요해지면 그때 전용 컬럼 migration을 후속 과제로 분리한다.
SELECTION_DIAG_NS = "selection_diagnostics_v1"
SELECTION_DIAG_MAX_BYTES = 8192  # UTF-8 직렬화 byte 상한(초과 시 namespace 생략).

# 결과 상태 — 배포 SQL CHECK와 1:1
# (news_keyword_decisions.result_status IN ('selected','not_selected','selected_no_representative'))
STATUS_SELECTED = "selected"
STATUS_SELECTED_NO_REP = "selected_no_representative"
STATUS_NOT_SELECTED = "not_selected"

_SELECTED_STATUSES = (STATUS_SELECTED, STATUS_SELECTED_NO_REP)

# 규칙 제외는 별도 result_status가 아니라 not_selected + reason_code로 구분한다(배포 스키마).
# 카운트에서는 RANK_CUTOFF(순위 컷)와 규칙 제외를 reason_code로 나눈다.
RANK_CUTOFF = "RANK_CUTOFF"

# RPC가 강제하는 decisions 상한(초과 시 EXCEPTION) — 클라이언트에서 먼저 방어한다.
MAX_DECISIONS = 200

# 배포 SQL의 news_keyword_runs.run_type CHECK와 1:1. 여기 없는 값을 보내면 RPC 내부
# INSERT가 CHECK 위반으로 실패해 **모든 진단 적재가 조용히 실패**한다(RPC를 mock한
# 단위 테스트로는 잡히지 않음 — Codex diff review P1).
RUN_TYPES = ("full", "news_top_only", "baseline")

# skip_reason 단일 권위 상수 — main.py freshness guard(오래된 실행이 최신 news_top 을 덮지
# 않게 upsert skip)가 그 실행을 mark_skipped(...) 할 때 쓴다. 문자열을 여러 곳에 흩뿌리지
# 않고 여기 한 곳에서만 정의한다.
#   ⚠️ skip_reason 은 클라이언트에서 화이트리스트로 강제하지 않는다(RUN_TYPES 와 달리).
#      허용값 계약의 유일한 권위는 **배포 SQL 의 news_keyword_runs.skip_reason CHECK** 이고,
#      이 값은 그 CHECK 에 STALE_WRITE_SKIPPED 로 등록돼 있어야 한다(migration 으로 관리).
#      미등록 상태에서 이 값을 보내면 RPC INSERT 가 CHECK 위반으로 진단 적재 전체가 실패한다.
SKIP_REASON_STALE_WRITE = "STALE_WRITE_SKIPPED"

# 기사 메타 allowlist — 본문/description 저장 금지(사용자 확정).
_ARTICLE_FIELDS = (
    "title", "url", "source", "published_at",
    "relevance_score", "is_incidental", "is_primary_cluster",
)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _norm_key(keyword):
    """후보 안정 식별자 — candidates._merge()의 pool 키와 동일 규약.

    display_keyword(merge 후 변형)나 객체 identity(dict 복사로 깨짐)를 쓰지 않는다.
    """
    return (keyword or "").strip().lower()


def _safe_article(article):
    """기사 메타를 allowlist로 축소한다. 본문/description은 애초에 담지 않는다.

    source 매핑(2026-07-17): article dict는 news/normalizer.py가 만들며 "press"
    필드만 갖는다("source" 키는 오늘 코드 어디서도 만들지 않는다 — 전수 확인됨).
    저장 컬럼명은 기존 스키마상 "source"로 고정돼 있어(랭킹 계층의 signals["news"]
    sources 개념과는 무관한, diagnostics 전용 표시명) 여기서만 press->source로
    투영한다. 우선순위(방어적, 현재 데이터엔 충돌 케이스가 없다): article에 실제
    "source" 값이 있으면(향후 upstream 확장 대비) 그것을 쓰고, 없으면 "press"로
    폴백한다. ranking/representative 선정/display_articles 구성에는 영향 없음
    (이 함수는 진단 전용 투영이며 main.py의 issues payload에 되먹임되지 않는다).
    """
    source = article.get("source") or article.get("press")
    safe = {f: article.get(f) for f in _ARTICLE_FIELDS if f != "source" and article.get(f) is not None}
    if source:
        safe["source"] = source
    return safe


class PassSnapshot:
    """단일 pass의 판정 결과 + degraded/errors를 함께 보유한다.

    호출자가 생성해 `_rank_and_select(..., diag=snapshot)`으로 전달한다.
    폐기되면 commit되지 않으므로 그 오류는 run에 섞이지 않는다.
    """

    def __init__(self, pass_name):
        self.pass_name = pass_name
        self.decisions = {}          # _norm_key -> decision dict (후보당 정확히 1개)
        self.degraded = False
        self.errors = []             # type name만 보관(메시지 금지)
        self.closed = False
        # B2(no_representative)로 제외된 수 — 진단에서 cohesion 탈락과 분리(Codex 최종리뷰 P3).
        self.no_representative_excluded_count = 0

    # -- 상태 --------------------------------------------------------------

    def mark_degraded(self, exc):
        """오류 1회라도 나면 degraded. 예외 '타입명'만 남긴다(§10-1)."""
        self.degraded = True
        self.errors.append(type(exc).__name__)

    def close(self):
        """seal — 추가 기록만 막고 수집분은 보존한다(폐기가 아니다).

        _backfill_pass의 finally에서 닫혀도 호출자가 final_snapshot으로 commit할 수 있어야 한다.
        """
        self.closed = True

    # -- 기록 --------------------------------------------------------------

    def record(self, keyword, result_status, reason_code, **fields):
        """후보 1건의 최종 판정. 같은 후보를 두 번 기록하면 degraded(계약 위반 신호)."""
        if self.closed:
            raise RuntimeError("closed snapshot")
        key = _norm_key(keyword)
        if key in self.decisions:
            raise RuntimeError("duplicate decision")
        row = {
            "keyword": keyword,
            "result_status": result_status,
            "reason_code": reason_code,
        }
        articles = fields.pop("articles", None)
        if articles:
            row["articles"] = [_safe_article(a) for a in articles]
        for k, v in fields.items():
            if v is not None:
                row[k] = v
        self.decisions[key] = row

    # -- 집계 --------------------------------------------------------------

    def finalize_selected(self, published_keywords):
        """발행 payload(issues["keywords"])로 selected 행의 실제 값을 확정한다.

        _rank_and_select 시점에는 rank/summary/representative/display_articles가 **아직
        존재하지 않는다**(builder가 나중에 만든다). 그 시점 값으로 기록하면 selected가
        전부 rank=None + NO_REPRESENTATIVE로 남아 진단이 실제 발행 결과와 어긋난다
        (Codex diff review P1). 그래서 발행 직전에 최종 값으로 덮어쓴다.

        1차 범위 한계(의도적): evidence_tokens / token_df / min_tokens /
        relevance_threshold / evidence_article_count는 채우지 않는다(NULL 허용 컬럼).
        summarize()가 (summary, summary_type)만 반환하고 판정 토큰·DF는 내부에서 버려서,
        정확히 채우려면 news/summarizer.py에 analysis helper를 추가해야 한다 — 이번 PR의
        승인 파일 범위 밖이고 "대표기사 규칙 변경 금지" 제약에도 맞물린다. 진단 측에서
        로직을 복제하면 향후 판정과 조용히 어긋나므로 복제하지 않는다.
        """
        for entry in published_keywords or []:
            key = _norm_key(entry.get("keyword"))
            row = self.decisions.get(key)
            if row is None or row["result_status"] not in _SELECTED_STATUSES:
                continue
            # 대표 없음의 권위 기준은 builder와 동일하게 summary_type이다(news/builder.py:104).
            # representative_title/article 존재로 재판정하면 어긋난다 — builder는
            # no_representative일 때 title은 비우지만 representative_article은 anchor 재확인
            # 용도로 그대로 넘기기 때문이다(builder.py:115). 그 값을 보고 판정하면 대표가
            # 없는 키워드를 SELECTED로 잘못 기록한다(Codex diff review P1).
            has_rep = entry.get("summary_type") != "no_representative"
            row["result_status"] = STATUS_SELECTED if has_rep else STATUS_SELECTED_NO_REP
            row["reason_code"] = "SELECTED" if has_rep else "NO_REPRESENTATIVE"
            row["has_representative"] = has_rep
            row["rank"] = entry.get("rank")
            row["score"] = entry.get("score")
            row["summary"] = entry.get("summary")
            row["summary_type"] = entry.get("summary_type")
            row["display_keyword"] = entry.get("display_keyword")
            row["merge_reason"] = entry.get("merge_reason")
            row["representative_title"] = entry.get("representative_title")
            rep = entry.get("representative_article") if has_rep else None
            row["representative_url"] = rep.get("url") if isinstance(rep, dict) else None
            row["signals"] = entry.get("signals")
            row["rank_delta"] = entry.get("rank_delta")
            row["article_count"] = len(entry.get("articles") or [])
            display = entry.get("display_articles") or []
            row["display_article_count"] = len(display)
            row["articles"] = [_safe_article(a) for a in display]

    def counts(self):
        """candidate_count = selected + not_selected + rule_excluded (invariant).

        rule_excluded는 별도 result_status가 아니라 not_selected 중 RANK_CUTOFF가 아닌 것
        (배포 스키마의 result_status CHECK가 3값뿐이므로 reason_code로 나눈다).
        """
        rows = list(self.decisions.values())
        selected = sum(1 for r in rows if r["result_status"] in _SELECTED_STATUSES)
        not_selected = sum(
            1 for r in rows
            if r["result_status"] == STATUS_NOT_SELECTED and r["reason_code"] == RANK_CUTOFF
        )
        rule_excluded = sum(
            1 for r in rows
            if r["result_status"] == STATUS_NOT_SELECTED and r["reason_code"] != RANK_CUTOFF
        )
        no_rep = sum(1 for r in rows if r["result_status"] == STATUS_SELECTED_NO_REP)
        return {
            "candidate_count": len(rows),
            "selected_count": selected,
            "not_selected_count": not_selected,
            "rule_excluded_count": rule_excluded,
            "no_representative_count": no_rep,
        }

    def payload_decisions(self):
        return list(self.decisions.values())


class RunDiagnostics:
    """run 단위 수집기. 채택된 snapshot 1개만 commit받는다."""

    def __init__(self, run_type="full"):
        if run_type not in RUN_TYPES:
            raise ValueError(f"invalid run_type: {run_type!r}")
        self.run_type = run_type
        self.started_at = _now_iso()
        # duration_ms 계산 전용 — 시스템 시각 보정(NTP 등)에 영향받지 않는 단조 시계.
        # started_at(표시/저장용 wall-clock)과 별개로 둔다.
        self._started_monotonic = time.monotonic()
        self.status = "success"
        self.skip_reason = None
        self.error_summary = None
        self.collected_candidate_count = 0
        self.thresholds = {}
        self.selection_diagnostics = None  # selection_diagnostics_v1 payload(dict) 또는 None
        # 채택 pass에서 B2(no_representative)로 제외된 수 — LOW_QUALITY_NEWS(cohesion 탈락)와
        # 진단상 섞이므로 selection_diagnostics에서 별도 집계하기 위해 보관(Codex 최종리뷰 P3).
        self.no_representative_excluded_count = 0
        self.final_snapshot = None
        self.degraded = False         # run 전역(공통 초기화/payload 조립 오류)
        self.errors = []

    # -- 상태 --------------------------------------------------------------

    def mark_degraded(self, exc):
        """pass 바깥 오류 = run 전역 degraded(§3-1 계약 5)."""
        self.degraded = True
        self.errors.append(type(exc).__name__)

    def mark_collected(self, count):
        self.collected_candidate_count = count

    def mark_selection_diagnostics(self, *, underfill_reason=None, counts=None,
                                   source_status=None, rejection_counts=None):
        """실행단위 selection 진단을 selection_diagnostics_v1 payload로 적재한다(순수 관찰).

        counts: {raw, deduped, clusters, eligible, selected} 정수 맵.
        source_status: {family: 'ok'|'fetch_failed'|'empty'|'stale'}.
        rejection_counts: {reason_code: N} (단계별 제외 수). set 등 비직렬화 값 금지 —
        호출부에서 정수/문자열만 넣는다(build_payload가 최종 직렬화 검증). 실패해도 랭킹 무관.
        """
        self.selection_diagnostics = {
            "underfill_reason": underfill_reason,
            "counts": dict(counts or {}),
            "source_status": dict(source_status or {}),
            "rejection_counts": dict(rejection_counts or {}),
        }

    def mark_skipped(self, skip_reason):
        self.status = "skipped"
        self.skip_reason = skip_reason

    def mark_failed(self, skip_reason, exc=None):
        self.status = "failed"
        self.skip_reason = skip_reason
        if exc is not None:
            # 예외 '타입명'만 — 메시지에 payload/헤더/secret이 실릴 수 있다(§10-1).
            self.error_summary = type(exc).__name__

    def commit(self, snapshot):
        """채택 확정된 snapshot만 승격. degraded/errors도 이 시점에 함께 넘어온다."""
        self.final_snapshot = snapshot
        # 채택 pass의 B2 제외 수를 run 레벨로 승격(selection_diagnostics 별도 집계용).
        if snapshot is not None:
            self.no_representative_excluded_count = snapshot.no_representative_excluded_count

    # -- 판정 --------------------------------------------------------------

    def is_degraded(self):
        """run 전역 degraded 또는 채택 snapshot degraded면 저장 금지."""
        if self.degraded:
            return True
        return bool(self.final_snapshot and self.final_snapshot.degraded)

    def build_payload(self, run_key, git_sha=None, rules_version=None):
        """RPC payload 조립. 여기서 나는 오류는 호출부에서 run 전역 degraded로 처리된다."""
        snap = self.final_snapshot
        counts = snap.counts() if snap else {
            "candidate_count": 0, "selected_count": 0, "not_selected_count": 0,
            "rule_excluded_count": 0, "no_representative_count": 0,
        }
        decisions = snap.payload_decisions() if snap else []
        if len(decisions) > MAX_DECISIONS:
            raise ValueError("decisions over limit")

        thresholds = dict(self.thresholds)
        # 수집 원시 후보 수는 카운트 invariant 합계에 넣지 않는 별도 메타값(§8-1).
        # DB 컬럼 추가 없이 thresholds JSON에 담는다.
        thresholds["collected_candidate_count"] = self.collected_candidate_count

        # selection_diagnostics_v1 격리 적재(H, 2026-07): json.dumps 사전검증 + UTF-8 byte
        # 상한. 직렬화 실패(비직렬화 값)나 상한 초과 시 이 namespace만 생략하고 진단 본체는
        # 보존한다 — 관측 편의가 진단 저장 자체를 깨지 않게 한다(fail-open, 랭킹 무관).
        if self.selection_diagnostics is not None:
            try:
                encoded = json.dumps(self.selection_diagnostics, ensure_ascii=False)
                if len(encoded.encode("utf-8")) <= SELECTION_DIAG_MAX_BYTES:
                    thresholds[SELECTION_DIAG_NS] = self.selection_diagnostics
                else:
                    logger.warning(
                        "[news-diag] %s 생략 — byte 상한 초과(%d>%d)",
                        SELECTION_DIAG_NS, len(encoded.encode("utf-8")), SELECTION_DIAG_MAX_BYTES,
                    )
            except (TypeError, ValueError) as e:
                logger.warning("[news-diag] %s 생략 — 직렬화 실패: %s", SELECTION_DIAG_NS, type(e).__name__)

        run = {
            "run_key": run_key,
            "workflow_run_id": os.getenv("GITHUB_RUN_ID"),
            "run_attempt": _int_or_none(os.getenv("GITHUB_RUN_ATTEMPT")),
            "run_type": self.run_type,
            "status": self.status,
            "skip_reason": self.skip_reason,
            "started_at": self.started_at,
            "finished_at": _now_iso(),
            "duration_ms": self._compute_duration_ms(),
            "git_sha": git_sha,
            "rules_version": rules_version,
            "thresholds": thresholds,
            "error_summary": self.error_summary,
            "pass_name": snap.pass_name if snap else None,
        }
        run.update(counts)
        return run, decisions

    def _compute_duration_ms(self):
        """monotonic clock 기준 경과 시간(ms). success/failed/skipped 전부 동일하게 계산된다
        (build_payload는 결과와 무관하게 항상 finally에서 1회 호출됨 — main.py 참조).

        음수만 방어(시계 역행 등 이론상 이상 케이스, monotonic이라 정상 경로에선 발생하지
        않는다) — 상한 클램프는 두지 않는다. 실제로 오래 걸린 정상 실행을 NULL로 지워버리면
        이번 PR이 메우려는 관측 공백을 다시 만드는 셈이다(Codex diff review P2).
        """
        try:
            elapsed_ms = round((time.monotonic() - self._started_monotonic) * 1000)
        except Exception:      # noqa: BLE001 — duration은 부가 정보, 실패해도 진단은 계속.
            return None
        return elapsed_ms if elapsed_ms >= 0 else None


def _int_or_none(v):
    try:
        return int(v) if v else None
    except (TypeError, ValueError):
        return None


def build_run_key():
    """GitHub: '{RUN_ID}:{RUN_ATTEMPT}' / 로컬: 'ts:{ISO8601}'(배포 SQL 주석 규약)."""
    run_id = os.getenv("GITHUB_RUN_ID")
    attempt = os.getenv("GITHUB_RUN_ATTEMPT") or "1"
    if run_id:
        return f"{run_id}:{attempt}"
    return f"ts:{_now_iso()}"


def resolve_git_sha():
    sha = os.getenv("GITHUB_SHA")
    if sha:
        return sha
    try:
        import subprocess
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or None
    except Exception:      # noqa: BLE001 — sha는 부가 정보. 실패해도 진단은 계속.
        return None
