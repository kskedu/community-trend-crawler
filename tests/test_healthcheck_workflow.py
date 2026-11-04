"""healthcheck.yml 회귀 테스트 (unittest, 실제 네트워크·gh·Telegram 호출 없음).

워크플로 YAML 의 실제 run 스크립트를 GitHub Actions 와 같은 셸 옵션
(bash --noprofile --norc -eo pipefail)으로 실행하되, PATH 앞에 둔 가짜
curl / gh 가 fixture 응답을 돌려주고 호출 인자만 기록한다.

검증 항목:
- 최신 수집 시각 조회가 collected_at 기존 인덱스(collected_at DESC)를 타는 형태인지
- NULL 행을 not.is.null 로 제외하는지, 5분 넘게 미래인 최신값을 ok 로 두지 않고 실패시키는지
- ok / stale / down 분류와 120·240분 경계
- 게시물 0건 · NULL · 키 누락 · 형식 오류 · tz 없는 값 · 오류 응답 → step 실패(출력 없음)
- UTC(Z / +00:00) 와 KST(+09:00) 표기가 같은 경과 시간으로 판정되는지
- GITHUB_OUTPUT 계약(elapsed_min / last_at / status) 유지
- 재실행 dispatch 조건과 중복 방지(queued/in_progress, 조회 실패 fail-closed) 유지
- Telegram 알림 조건(down 또는 force_alert)과 KST 표기 유지
- schedule(매시 23분)·workflow_dispatch(force_alert) 트리거 유지

PyYAML 이 필요하다: pip install -r requirements-dev.txt (운영 requirements.txt 에는 넣지 않는다).
"""
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "healthcheck.yml")


def _load():
    with open(WORKFLOW, encoding="utf-8") as f:
        wf = yaml.safe_load(f)
    steps = {s["name"]: s for s in wf["jobs"]["healthcheck"]["steps"]}
    # PyYAML 은 'on' 키를 True 로 읽는다.
    triggers = wf.get("on", wf.get(True))
    return wf, steps, triggers


WF, STEPS, TRIGGERS = _load()
CHECK = STEPS["Check latest community_posts timestamp"]
DISPATCH = STEPS["Trigger crawler (stale or down)"]
ALERT = STEPS["Send Telegram alert (down only)"]


def _render(script, outputs=None, inputs=None):
    """run 스크립트의 ${{ ... }} 표현식을 고정값으로 치환한다(Actions 템플릿 대체)."""
    outputs = outputs or {}
    inputs = inputs or {}

    def sub(m):
        expr = m.group(1).strip()
        if expr.startswith("steps.check.outputs.") or expr.startswith("steps.dispatch.outputs."):
            return str(outputs.get(expr.rsplit(".", 1)[1], ""))
        if expr.startswith("inputs."):
            return str(inputs.get(expr.split(".", 1)[1], ""))
        if expr == "github.repository":
            return "example/fake-repo"
        if expr.startswith("secrets."):
            return "FAKE_SECRET"
        raise AssertionError(f"처리하지 않은 표현식: {expr}")

    return re.sub(r"\$\{\{(.*?)\}\}", sub, script)


class _Sandbox:
    """가짜 curl/gh 와 python3 를 둔 격리 디렉터리."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.bin = os.path.join(self.dir, "bin")
        os.mkdir(self.bin)
        self.curl_log = os.path.join(self.dir, "curl.log")
        self.gh_log = os.path.join(self.dir, "gh.log")
        self.output = os.path.join(self.dir, "github_output")
        open(self.output, "w").close()
        # 가짜 curl: 인자를 한 줄씩 기록하고 FAKE_CURL_BODY 파일 내용을 출력한다.
        self._tool("curl", 'for a in "$@"; do printf "%s\\n" "$a"; done >> "$CURL_LOG"; '
                           'printf -- "---\\n" >> "$CURL_LOG"; '
                           '[ -n "${FAKE_CURL_BODY:-}" ] && cat "$FAKE_CURL_BODY"; '
                           '[ -n "${FAKE_CURL_OUT:-}" ] && printf "%s" "$FAKE_CURL_OUT"; exit ${FAKE_CURL_EXIT:-0}')
        # 가짜 gh: 인자를 기록하고 FAKE_GH_<status> 값으로 응답, FAKE_GH_FAIL=1 이면 실패.
        self._tool("gh", 'printf "%s\\n" "$*" >> "$GH_LOG"; '
                         '[ "${FAKE_GH_FAIL:-0}" = "1" ] && exit 1; '
                         '[ -n "${FAKE_GH_FAIL_STATUS:-}" ] && case "$*" in '
                         '*"--status ${FAKE_GH_FAIL_STATUS}"*) exit 1;; esac; '
                         'case "$*" in '
                         '"workflow run"*) [ "${FAKE_GH_DISPATCH_FAIL:-0}" = "1" ] && exit 1;; '
                         '*"--status completed"*) echo "${FAKE_GH_LAST:-success}";; '
                         '*"--status queued"*) echo "${FAKE_GH_QUEUED:-0}";; '
                         '*"--status in_progress"*) echo "${FAKE_GH_RUNNING:-0}";; '
                         'esac; exit 0')
        self._tool("python3", f'exec "{sys.executable}" "$@"')

    def _tool(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write("#!/bin/bash\n" + body + "\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)

    def run(self, script, body=None, env_extra=None):
        script_path = os.path.join(self.dir, "step.sh")
        with open(script_path, "w") as f:
            f.write(script)
        env = {
            "PATH": self.bin + ":/usr/bin:/bin",
            "HOME": self.dir,
            "CURL_LOG": self.curl_log,
            "GH_LOG": self.gh_log,
            "GITHUB_OUTPUT": self.output,
            "SUPABASE_URL": "https://fake.invalid",
            "SUPABASE_KEY": "FAKE_KEY",
        }
        if body is not None:
            body_path = os.path.join(self.dir, "body.json")
            with open(body_path, "w") as f:
                f.write(body)
            env["FAKE_CURL_BODY"] = body_path
        env.update(env_extra or {})
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", script_path],
            env=env, capture_output=True, text=True, timeout=30,
        )

    def outputs(self):
        out = {}
        with open(self.output) as f:
            for line in f:
                k, _, v = line.rstrip("\n").partition("=")
                out[k] = v
        return out

    def read(self, path):
        return open(path).read() if os.path.exists(path) else ""

    def close(self):
        self.tmp.cleanup()


def _ahead(seconds, fmt="utc"):
    """현재로부터 seconds 초 뒤(미래) 시각."""
    t = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    if fmt == "kst":
        return t.astimezone(timezone(timedelta(hours=9))).isoformat()
    return t.isoformat()


def _ago(minutes, fmt="z"):
    """현재로부터 minutes 분 30초 전(분 경계에서 떨어뜨려 실행 지연에 견고)."""
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes, seconds=30)
    if fmt == "z":
        return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-1] + "Z"
    if fmt == "utc":
        return t.isoformat()
    if fmt == "kst":
        return t.astimezone(timezone(timedelta(hours=9))).isoformat()
    raise ValueError(fmt)


class _Base(unittest.TestCase):
    def setUp(self):
        self.sb = _Sandbox()

    def tearDown(self):
        self.sb.close()


class TestCheckStep(_Base):
    def _check(self, body):
        return self.sb.run(CHECK["run"], body=body)

    def _row(self, value):
        return json.dumps([{"collected_at": value}])

    def test_query_uses_collected_at_index_shape(self):
        r = self._check(self._row(_ago(5)))
        self.assertEqual(r.returncode, 0, r.stderr)
        log = self.sb.read(self.sb.curl_log)
        self.assertIn(
            "https://fake.invalid/rest/v1/community_posts"
            "?select=collected_at&collected_at=not.is.null&order=collected_at.desc&limit=1",
            log,
        )
        self.assertNotIn("created_at", log)
        # 정렬은 인덱스(collected_at DESC = NULLS FIRST)와 같은 방향이어야 한다.
        self.assertNotIn("nullslast", log)
        self.assertIn("apikey: FAKE_KEY", log)
        self.assertIn("Authorization: Bearer FAKE_KEY", log)
        self.assertEqual(log.count("---"), 1)

    def test_ok(self):
        r = self._check(self._row(_ago(10)))
        self.assertEqual(r.returncode, 0, r.stderr)
        o = self.sb.outputs()
        self.assertEqual(o["status"], "ok")
        self.assertEqual(o["elapsed_min"], "10")

    def test_stale(self):
        r = self._check(self._row(_ago(150)))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.sb.outputs()["status"], "stale")

    def test_down(self):
        r = self._check(self._row(_ago(300)))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.sb.outputs()["status"], "down")

    def test_boundaries(self):
        for minutes, want in ((119, "ok"), (120, "stale"), (239, "stale"), (240, "down")):
            with self.subTest(minutes=minutes):
                sb = _Sandbox()
                try:
                    r = sb.run(CHECK["run"], body=self._row(_ago(minutes)))
                    self.assertEqual(r.returncode, 0, r.stderr)
                    o = sb.outputs()
                    self.assertEqual(o["elapsed_min"], str(minutes))
                    self.assertEqual(o["status"], want)
                finally:
                    sb.close()

    def test_output_contract(self):
        value = _ago(3)
        r = self._check(self._row(value))
        self.assertEqual(r.returncode, 0, r.stderr)
        o = self.sb.outputs()
        self.assertEqual(set(o), {"elapsed_min", "last_at", "status"})
        self.assertEqual(o["last_at"], value)
        self.assertEqual(CHECK["id"], "check")

    def test_timezone_notations_agree(self):
        results = {}
        for fmt in ("z", "utc", "kst"):
            sb = _Sandbox()
            try:
                r = sb.run(CHECK["run"], body=self._row(_ago(130, fmt)))
                self.assertEqual(r.returncode, 0, r.stderr)
                results[fmt] = (sb.outputs()["elapsed_min"], sb.outputs()["status"])
            finally:
                sb.close()
        self.assertEqual(results["z"], ("130", "stale"))
        self.assertEqual(results["utc"], results["z"])
        self.assertEqual(results["kst"], results["z"])

    def test_small_future_skew_is_tolerated(self):
        # 4분 30초 미래 = 허용 한계(5분) 안 → 시계 오차로 보고 ok
        r = self._check(self._row(_ahead(270)))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.sb.outputs()["status"], "ok")

    def test_future_beyond_tolerance_fails_without_outputs(self):
        for name, value in (("utc_6m", _ahead(360)), ("kst_6m", _ahead(360, "kst")),
                            ("naive_kst_as_utc_9h", _ahead(9 * 3600))):
            with self.subTest(case=name):
                sb = _Sandbox()
                try:
                    r = sb.run(CHECK["run"], body=self._row(value))
                    self.assertNotEqual(r.returncode, 0, f"{name} 가 성공 처리됨: {r.stdout}")
                    self.assertEqual(sb.outputs(), {})
                    self.assertIn("미래", r.stdout)
                finally:
                    sb.close()

    def test_bad_responses_fail_without_outputs(self):
        cases = {
            "empty": "[]",
            "null": self._row(None),
            "missing_key": json.dumps([{"created_at": _ago(1)}]),
            "malformed": self._row("not-a-time"),
            "naive_no_tz": self._row("2026-11-02T07:00:00"),
            "postgrest_error": json.dumps({"code": "PGRST", "message": "boom"}),
            "not_json": "<html>502</html>",
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                sb = _Sandbox()
                try:
                    r = sb.run(CHECK["run"], body=body)
                    self.assertNotEqual(r.returncode, 0, f"{name} 가 성공 처리됨: {r.stdout}")
                    self.assertNotIn("status", sb.outputs())
                finally:
                    sb.close()


class TestCheckStepTimeout(_Base):
    """2026-11-04 장애 후속: 무응답 DB 를 무한정 기다리지 않고, 원문을 로그에 남기지 않는다."""

    def test_curl_has_bounded_timeout(self):
        r = self.sb.run(CHECK["run"], body=json.dumps([{"collected_at": _ago(5)}]))
        self.assertEqual(r.returncode, 0, r.stderr)
        args = self.sb.read(self.sb.curl_log).splitlines()
        self.assertIn("-m", args)
        self.assertEqual(args[args.index("-m") + 1], "20")

    def test_curl_timeout_fails_without_outputs(self):
        r = self.sb.run(CHECK["run"], body="", env_extra={"FAKE_CURL_EXIT": "28"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("응답 없음", r.stdout)
        self.assertNotIn("status", self.sb.outputs())

    def test_raw_error_body_not_logged(self):
        html = "<!DOCTYPE html><title>522: Connection timed out</title>Error code 522"
        r = self.sb.run(CHECK["run"], body=html)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("DOCTYPE", r.stdout + r.stderr)
        self.assertNotIn("Error code", r.stdout + r.stderr)
        self.assertNotIn("status", self.sb.outputs())


class TestDispatchStep(_Base):
    def test_condition_unchanged(self):
        self.assertEqual(DISPATCH["if"], "steps.check.outputs.status != 'ok'")

    def _dispatch(self, **env):
        script = _render(DISPATCH["run"], outputs={"status": "stale"})
        return self.sb.run(script, env_extra={k: str(v) for k, v in env.items()})

    def test_dispatches_when_idle(self):
        r = self._dispatch(FAKE_GH_QUEUED=0, FAKE_GH_RUNNING=0)
        self.assertEqual(r.returncode, 0, r.stderr)
        log = self.sb.read(self.sb.gh_log)
        self.assertIn('workflow run Community Trend Crawler --ref main --repo example/fake-repo', log)

    def test_skips_when_already_running_or_queued(self):
        for q, running in ((1, 0), (0, 1)):
            with self.subTest(queued=q, running=running):
                sb = _Sandbox()
                try:
                    script = _render(DISPATCH["run"], outputs={"status": "down"})
                    r = sb.run(script, env_extra={"FAKE_GH_QUEUED": str(q), "FAKE_GH_RUNNING": str(running)})
                    self.assertEqual(r.returncode, 0, r.stderr)
                    self.assertNotIn("workflow run", sb.read(sb.gh_log))
                finally:
                    sb.close()

    def test_fail_closed_when_lookup_fails(self):
        r = self._dispatch(FAKE_GH_FAIL=1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("workflow run", self.sb.read(self.sb.gh_log))

    def test_fail_closed_when_only_one_lookup_fails(self):
        for failing in ("queued", "in_progress"):
            with self.subTest(failing=failing):
                sb = _Sandbox()
                try:
                    script = _render(DISPATCH["run"], outputs={"status": "stale"})
                    r = sb.run(script, env_extra={"FAKE_GH_FAIL_STATUS": failing})
                    self.assertEqual(r.returncode, 0, r.stderr)
                    self.assertNotIn("workflow run", sb.read(sb.gh_log))
                finally:
                    sb.close()


class TestDispatchResult(_Base):
    """재실행 단계가 실제 결과를 남기고, 저장 실패 중인 크롤러는 재촉발하지 않는다(2026-11-04 후속)."""

    def _go(self, sb, **env):
        script = _render(DISPATCH["run"], outputs={"status": "down"})
        r = sb.run(script, env_extra={k: str(v) for k, v in env.items()})
        return r, sb.outputs().get("result"), sb.read(sb.gh_log)

    def test_results(self):
        cases = [
            ({}, 0, "dispatched", True),
            ({"FAKE_GH_QUEUED": 1}, 0, "skipped_active", False),
            ({"FAKE_GH_FAIL": 1}, 0, "skipped_lookup_failed", False),
            ({"FAKE_GH_FAIL_STATUS": "completed"}, 0, "skipped_lookup_failed", False),
            ({"FAKE_GH_LAST": "failure"}, 0, "skipped_crawler_failing", False),
            ({"FAKE_GH_LAST": "cancelled"}, 0, "dispatched", True),
            ({"FAKE_GH_DISPATCH_FAIL": 1}, 1, "dispatch_failed", True),
        ]
        for env, code, result, dispatched in cases:
            with self.subTest(env=env):
                sb = _Sandbox()
                try:
                    r, got, log = self._go(sb, **env)
                    self.assertEqual(r.returncode, code, r.stdout + r.stderr)
                    self.assertEqual(got, result)
                    self.assertEqual("workflow run" in log, dispatched)
                finally:
                    sb.close()


class TestAlertMessageMatchesAction(_Base):
    """알림 문구는 실제 조치만 쓴다. '재실행 트리거됨/완료' 같은 확정 문구를 쓰지 않는다."""

    def _alert(self, sb, result, curl_out="200", curl_exit="0"):
        script = _render(ALERT["run"], outputs={"elapsed_min": "300", "last_at": "2026-11-04T12:00:00+00:00",
                                                "result": result})
        env = {k: _render(str(v), outputs={"result": result}) for k, v in ALERT["env"].items()}
        env.update({"FAKE_CURL_OUT": curl_out, "FAKE_CURL_EXIT": curl_exit})
        r = sb.run(script, env_extra=env)
        return r, sb.read(sb.curl_log)

    def test_action_text_per_result(self):
        expect = {
            "dispatched": "조치: 크롤러 재실행 요청 전송(실행 결과는 미확인)",
            "skipped_active": "조치: 크롤러가 이미 실행/대기 중이라 재실행 요청 생략",
            "skipped_lookup_failed": "조치: 실행 목록 조회 실패로 재실행 요청 생략",
            "dispatch_failed": "조치: 크롤러 재실행 요청 실패 — 수동 확인 필요",
            "": "조치: 재실행 요청 없음",
        }
        for result, text in expect.items():
            with self.subTest(result=result):
                sb = _Sandbox()
                try:
                    r, log = self._alert(sb, result)
                    self.assertEqual(r.returncode, 0, r.stderr)
                    self.assertIn(text, log)
                    self.assertNotIn("트리거됨", log)
                    self.assertNotIn("재실행 완료", log)
                finally:
                    sb.close()

    def test_telegram_failure_is_not_reported_as_sent(self):
        for out, ex in (("401", "0"), ("", "28"), ("500", "0")):
            with self.subTest(out=out, ex=ex):
                sb = _Sandbox()
                try:
                    r, _ = self._alert(sb, "dispatched", curl_out=out, curl_exit=ex)
                    self.assertNotEqual(r.returncode, 0)
                    self.assertNotIn("전송 완료", r.stdout)
                    self.assertIn("전송 실패", r.stdout)
                    self.assertNotIn("FAKE_SECRET", r.stdout + r.stderr)
                finally:
                    sb.close()

    def test_telegram_curl_has_timeout(self):
        sb = _Sandbox()
        try:
            self._alert(sb, "dispatched")
            args = sb.read(sb.curl_log).splitlines()
            self.assertEqual(args[args.index("-m") + 1], "10")
        finally:
            sb.close()


class TestAlertStep(_Base):
    def test_condition_unchanged(self):
        self.assertEqual(
            ALERT["if"],
            "always() && (inputs.force_alert == true || (steps.check.outputs.status == 'down'"
            " && steps.dispatch.outputs.result != 'skipped_crawler_failing'))",
        )

    def test_message_uses_last_at_in_kst(self):
        script = _render(
            ALERT["run"],
            outputs={"elapsed_min": "305", "last_at": "2026-11-02T00:30:00.123+00:00"},
        )
        step_env = {k: _render(str(v)) for k, v in ALERT["env"].items()}
        step_env["FAKE_CURL_OUT"] = "200"
        r = self.sb.run(script, env_extra=step_env)
        self.assertEqual(r.returncode, 0, r.stderr)
        log = self.sb.read(self.sb.curl_log)
        self.assertIn("https://api.telegram.org/botFAKE_SECRET/sendMessage", log)
        self.assertIn("chat_id=FAKE_SECRET", log)
        self.assertIn("마지막 수집: 2026-11-02 09:30 KST", log)
        self.assertIn("경과 시간: 5시간 5분", log)


class TestTriggers(unittest.TestCase):
    def test_schedule_and_manual_dispatch_unchanged(self):
        self.assertEqual(TRIGGERS["schedule"], [{"cron": "23 * * * *"}])
        force = TRIGGERS["workflow_dispatch"]["inputs"]["force_alert"]
        self.assertEqual(force["type"], "boolean")
        self.assertIs(force["default"], False)

    def test_step_order_unchanged(self):
        names = [s["name"] for s in WF["jobs"]["healthcheck"]["steps"]]
        self.assertEqual(names, [
            "Check latest community_posts timestamp",
            "Trigger crawler (stale or down)",
            "Send Telegram alert (down only)",
        ])


if __name__ == "__main__":
    unittest.main()
