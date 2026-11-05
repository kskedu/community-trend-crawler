"""크롤러 DB 저장 실패 연속 알림 (crawl.yml 마지막 단계, 2026-11-04 장애 후속).

새 Cron 없이 crawl.yml 의 정기 실행(30분 주기) 끝에서 한 번 돈다. 판단 근거는 이번 실행의
결과 파일(main.py 가 CRAWL_OUTCOME_PATH 에 쓴 요약)과 같은 workflow 의 직전 실행 결론이다.
Supabase Dashboard 의 Healthy 표시는 쓰지 않는다 — 실제 저장 성공만이 복구 근거다.

- 장애 알림(DOWN): 이번 실행 실패 + 직전 실행 실패 + 그 이전이 성공(또는 기록 없음)
  → 연속 2회째에 한 번만. 3회째부터는 보내지 않는다.
- 복구 알림(RECOVERED): 이번 실행 정상 + 직전 2회 연속 실패(= 장애 알림을 보낸 상태)
  → 한 번만. 실패 1회 뒤 정상은 알리지 않는다.
- 직전 실행은 '완료 순서'로 본다. 겹친 실행(큐 지연·healthcheck dispatch)에서 늦게 끝난 쪽이
  먼저 끝난 실패를 보고 판정하므로 장애 알림이 빠지지 않는다. 한계: 실행은 알림 단계가 끝난
  뒤에야 completed 가 되므로, 알림 단계가 같은 수십 초 안에 겹친 실행들은 서로를 진행 중으로
  보고 같은 이력으로 판정한다. 그래서 장애·복구 알림이 빠지거나(둘 다 첫 실패로 판정) 2번 갈 수
  있다(둘 다 2회째로 판정). 재실행(re-run) 중인 실행도 진행 중이라 그동안 이력에서 빠진다.
- 복구는 이번 실행의 실제 필수 저장 성공(required_saves_ok > 0, 필수 저장 실패 0)이 있을 때만
  인정한다. google_trends 같은 선택 저장만 성공한 실행은 복구 근거가 아니다.
- 판단 불가(결과 파일 누락·손상, 저장 근거 없음, 직전 기록 조회 실패, 실행 취소)는 알리지
  않는다 — 특히 복구는 근거 없이 선언하지 않는다(fail-closed).
- main 브랜치 실행만 알린다(기능 브랜치 수동 실행이 운영 알림을 보내지 않게).
- 감지 한계: 크롤러 step 이 timeout(20분)으로 끝나면 결과 파일이 없어 '원인 미상 실패'로 센다.
  실행이 취소되면(cancelled) 판정하지 않고, 이력에서도 빠진다. job 자체가 시작되지 않는 경우
  (cron-job.org 정지·Actions 장애)는 이 단계가 돌지 않으므로 healthcheck.yml(데이터 경과 시간)
  이 담당한다. 알림 실패·판정 불가는 Actions 주석(::warning)으로 run 화면에 남긴다.
  checkout 이 실패하면 이 모듈이 없어 단계가 실패하고 continue-on-error 로 가려진다(그 실행은
  이력에 failure 로 남는다). step timeout 때 steps.<id>.outcome 이 failure 인지는 실측하지 않았다
  — cancelled 로 보고되면 판정하지 않는다.
- Telegram 전송은 실패하면 5초 뒤 1회만 다시 시도한다(유일한 장애 알림이 일시 오류로 사라지지
  않게). 그래도 실패하면 이후 실행은 '이미 알림'으로 보므로 run 주석만 남는다.
- 자동 재실행·DB 재시작은 하지 않는다. 이 스크립트는 항상 종료 코드 0 이다(실행 결론을
  바꾸면 다음 실행의 이력 판단이 틀어진다).
- 토큰·키는 출력하지 않는다. 예외는 타입명만 남긴다.

표준 라이브러리만 쓴다.
"""
import http.client
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("crawl_alert")

HTTP_TIMEOUT_SECONDS = 10
HISTORY_PAGE = 30
KST = timezone(timedelta(hours=9))
_VALID_STATUS = {"OK", "PARTIAL", "FAILED"}


class HistoryError(Exception):
    """직전 실행 기록을 믿을 수 없다(조회 실패·형식 이상·현재 실행 미발견)."""


# ── 순수 판정 ────────────────────────────────────────────────────────────


def read_outcome(path):
    """결과 파일을 읽는다. 없거나 손상·형식 이상이면 None."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("status") not in _VALID_STATUS:
        return None
    return data


def current_state(outcome, step_outcome):
    """이번 실행 상태: FAILED · FAILED_UNKNOWN · OK · UNKNOWN · CANCELLED.

    step_outcome 은 크롤러 step 의 steps.<id>.outcome(success·failure·cancelled·skipped).
    skipped 는 앞 단계(설치 등) 실패로 크롤러가 돌지 못한 경우라 원인 미상 실패로 본다.
    """
    if step_outcome == "cancelled":
        return "CANCELLED"
    if outcome is not None and outcome.get("status") == "FAILED":
        return "FAILED"
    if step_outcome in ("failure", "skipped"):
        return "FAILED_UNKNOWN"
    if (outcome is not None and step_outcome == "success"
            and (outcome.get("required_saves_ok") or 0) > 0 and not outcome.get("required_save_failed")):
        return "OK"
    return "UNKNOWN"


def decide(state, history, earlier_active):
    """(decision, reason). decision: DOWN · RECOVERED · NONE.

    history: 이번 실행 전에 완료된 같은 workflow·같은 브랜치 실행의 [(conclusion, created_at)]
    완료 순 최신순, conclusion 은 success·failure 만(취소 등 제외).
    earlier_active 는 기록용이다(판정에 쓰지 않는다 — 쓰면 겹친 실행에서 알림이 빠진다).
    """
    concl = [c for c, _ in history]
    if state in ("FAILED", "FAILED_UNKNOWN"):
        if concl[:1] == ["failure"] and (len(concl) < 2 or concl[1] == "success"):
            return "DOWN", "second_consecutive_failure"
        if concl[:1] == ["failure"]:
            return "NONE", "already_alerted"
        return "NONE", "first_failure"
    if state == "OK":
        if concl[:2] == ["failure", "failure"]:
            return "RECOVERED", "save_succeeded_after_alert"
        return "NONE", "normal"
    return "NONE", f"state_{state.lower()}"


def split_history(runs, current_run_id):
    """GitHub runs 목록에서 현재 실행을 뺀 같은 브랜치 완료 실행을 완료 순 최신순으로.

    반환 (history, earlier_active). 현재 실행은 지금 완료되는 중이므로, 이미 완료된 다른 실행은
    생성 시각과 무관하게 모두 '이전'이다. 완료 시각은 updated_at 으로 본다.
    """
    by_id = {r.get("id"): r for r in runs}
    if current_run_id not in by_id:
        raise HistoryError("current_run_not_found")
    current = by_id[current_run_id]
    branch = current.get("head_branch")
    started = current.get("created_at") or ""
    done, earlier_active = [], False
    for r in runs:
        if r.get("id") == current_run_id or r.get("head_branch") != branch:
            continue
        if r.get("status") != "completed":
            if (r.get("created_at") or "") <= started:
                earlier_active = True
            continue
        if r.get("conclusion") in ("success", "failure"):
            done.append(r)
    done.sort(key=lambda r: r.get("updated_at") or r.get("created_at") or "", reverse=True)
    return [(r["conclusion"], r.get("created_at")) for r in done], earlier_active


def _kst(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(KST).strftime(
            "%Y-%m-%d %H:%M KST")
    except (TypeError, ValueError):
        return "확인 불가"


def build_message(decision, state, outcome, history, auth, run_url):
    last_ok = next((t for c, t in history if c == "success"), None)
    if decision == "DOWN":
        kinds = "-"
        failed = "?"
        db_down = "확인 불가"
        if outcome:
            kinds = ", ".join(f"{k} {v}건" for k, v in sorted((outcome.get("failure_kinds") or {}).items())) or "-"
            failed = str(len(outcome.get("required_save_failed") or []))
            db_down = "예" if outcome.get("db_unavailable") else "아니오"
        lines = [
            "🚨 [StartHub] 크롤러 2회 연속 실패",
            "",
            f"이번 실행: {'필수 DB 저장 실패' if state == 'FAILED' else '실패(원인 미상·결과 파일 없음)'}",
            "직전 실행: 실패(원인은 해당 실행 로그에서 확인 — DB 외 실패일 수 있음)",
            f"실패 저장: {failed}건 · 분류: {kinds}",
            f"DB 응답 없음 판정: {db_down}",
            f"Auth health: {auth}",
            f"마지막 성공 실행: {_kst(last_ok) if last_ok else '기록 없음'}",
            "자동 재실행·DB 재시작은 하지 않습니다. 장애 중 추가 알림 없음, 복구 시 1회 알림.",
        ]
    else:
        first_fail = None
        for c, t in history:
            if c != "failure":
                break
            first_fail = t
        lines = [
            "✅ [StartHub] 크롤러 DB 저장 복구",
            "",
            "근거: 이번 실행의 필수 저장 성공(Dashboard 표시가 아니라 실제 저장)",
            f"연속 실패 시작: {_kst(first_fail) if first_fail else '확인 불가'}",
            f"Auth health: {auth}",
        ]
    if run_url:
        lines += ["", run_url]
    return "\n".join(lines)


# ── 외부 호출(테스트에서 urlopen 을 대체한다) ──────────────────────────────


def fetch_runs(api_url, repo, workflow, token, branch=""):
    url = (f"{api_url}/repos/{repo}/actions/workflows/{urllib.parse.quote(workflow)}/runs"
           f"?per_page={HISTORY_PAGE}")
    if branch:
        url += f"&branch={urllib.parse.quote(branch)}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
        raise HistoryError(type(e).__name__) from None
    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    if not isinstance(runs, list):
        raise HistoryError("bad_shape")
    return runs


def auth_health(supabase_url, key):
    """Auth /health 1회(10초). 결과 문자열만 반환한다."""
    if not supabase_url or not key:
        return "확인 안 함(설정 없음)"
    req = urllib.request.Request(f"{supabase_url.rstrip('/')}/auth/v1/health",
                                 headers={"apikey": key})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as r:
            return f"HTTP {r.status} ({time.monotonic() - t0:.1f}s)"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except http.client.HTTPException:
        return "응답 이상"
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, TimeoutError) or isinstance(e, TimeoutError):
            return f"응답 없음({HTTP_TIMEOUT_SECONDS}초 초과)"
        return "연결 실패"


TELEGRAM_RETRY_DELAY_SECONDS = 5


def send_telegram(token, chat_id, text):
    """전송 1회 + 실패 시 1회 재시도. 성공 여부를 반환한다."""
    if not token or not chat_id:
        logger.warning("[crawl-alert] Telegram 설정 없음 → 전송 생략")
        return False
    if _send_telegram_once(token, chat_id, text):
        return True
    time.sleep(TELEGRAM_RETRY_DELAY_SECONDS)
    logger.info("[crawl-alert] Telegram 1회 재시도")
    return _send_telegram_once(token, chat_id, text)


def _send_telegram_once(token, chat_id, text):
    if not token or not chat_id:
        logger.warning("[crawl-alert] Telegram 설정 없음 → 전송 생략")
        return False
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as r:
            ok = 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        logger.warning("[crawl-alert] Telegram 전송 실패 HTTP %s", e.code)
        return False
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        logger.warning("[crawl-alert] Telegram 전송 실패 %s", type(e).__name__)
        return False
    if not ok:
        logger.warning("[crawl-alert] Telegram 전송 실패")
    return ok


# ── 진입점 ────────────────────────────────────────────────────────────────


def run(env):
    outcome = read_outcome(env.get("CRAWL_OUTCOME_PATH"))
    state = current_state(outcome, env.get("CRAWL_STEP_OUTCOME", ""))
    history, decision, reason = [], "NONE", ""
    branch = env.get("GITHUB_REF_NAME", "")
    alert_branch = env.get("CRAWL_ALERT_BRANCH", "main")
    try:
        if branch != alert_branch:
            raise HistoryError(f"branch_not_alerted:{branch or '-'}")
        run_id = int(env.get("GITHUB_RUN_ID", ""))
        runs = fetch_runs(env.get("GITHUB_API_URL", "https://api.github.com"),
                          env.get("GITHUB_REPOSITORY", ""), env.get("CRAWL_WORKFLOW_FILE", "crawl.yml"),
                          env.get("GITHUB_TOKEN", ""), branch)
        history, earlier_active = split_history(runs, run_id)
        decision, reason = decide(state, history, earlier_active)
        if earlier_active:
            reason += ",earlier_run_active"
    except (HistoryError, ValueError) as e:
        if isinstance(e, HistoryError) and str(e).startswith("branch_not_alerted"):
            reason = str(e)
        else:
            reason = f"history_unavailable:{e}" if isinstance(e, HistoryError) else "history_unavailable:run_id"
    sent = None
    if decision != "NONE":
        auth = auth_health(env.get("SUPABASE_URL", ""), env.get("SUPABASE_KEY", ""))
        run_url = ""
        if env.get("GITHUB_SERVER_URL") and env.get("GITHUB_REPOSITORY") and env.get("GITHUB_RUN_ID"):
            run_url = f"{env['GITHUB_SERVER_URL']}/{env['GITHUB_REPOSITORY']}/actions/runs/{env['GITHUB_RUN_ID']}"
        msg = build_message(decision, state, outcome, history, auth, run_url)
        sent = send_telegram(env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", ""), msg)
        logger.info("[crawl-alert] auth_health=%s", auth)
    # continue-on-error 로 step 이 실패 표시되지 않으므로, 알림을 못 보낸 경우는 Actions 주석으로
    # run 화면에 드러낸다(알림 실패를 조용히 숨기지 않는다).
    if sent is False:
        print(f"::warning title=crawl alert not delivered::{decision} 알림 전송 실패 — Telegram 미전달")
    elif state in ("FAILED", "FAILED_UNKNOWN") and reason.startswith("history_unavailable"):
        print("::warning title=crawl alert undecided::직전 실행 기록 조회 실패로 연속 실패 판정 불가")
    line = (f"[crawl-alert] state={state} decision={decision} reason={reason} "
            f"history={','.join(c for c, _ in history[:3]) or '-'} "
            f"sent={'-' if sent is None else str(sent).lower()}")
    logger.info(line)
    path = env.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
    return {"state": state, "decision": decision, "reason": reason, "sent": sent}


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    try:
        run(dict(os.environ))
    except Exception as e:  # noqa: BLE001 — 알림 단계가 실행 결론을 바꾸면 안 된다
        logger.warning("[crawl-alert] 예기치 않은 오류 %s → 알림 생략", type(e).__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
