"""ops/crawl_alert.py · crawl.yml 알림 단계 회귀 테스트 (실제 Telegram·GitHub·Supabase 호출 없음).

urllib.request.urlopen 을 가짜로 바꿔 GitHub runs API·Auth health·Telegram 응답을 흉내 내고,
보낸 요청을 기록한다. 검증 항목:
- 상태 판정: 결과 파일 FAILED/OK/PARTIAL, 누락·손상(fail-closed), 취소, 앞 단계 실패
- 알림 결정: 2회 연속 실패에 1회 · 3회째 없음 · 복구 1회 · 실패 1회 뒤 정상은 없음 · 기록 없음
- 이력 신뢰성: 다른 브랜치·이후 생성 실행 제외, 앞선 실행 진행 중이면 보류, 현재 실행 미발견·API 오류
- Telegram 실패·예외에도 종료 코드 0, 토큰·키가 로그·메시지에 없음
- crawl.yml: always()·continue-on-error·권한 최소 확대·결과 파일 경로 일치·timeout
"""
import io
import json
import logging
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ops import crawl_alert as ca

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TG_TOKEN = "123456:TG_TOKEN_TEST_ONLY"
GH_TOKEN = "ghs_GH_TOKEN_TEST_ONLY"
SB_KEY = "sb_secret_SB_KEY_TEST_ONLY"


def _run(id_, conclusion="success", status="completed", created="2026-11-04T12:00:00Z", branch="main"):
    return {"id": id_, "status": status, "conclusion": conclusion if status == "completed" else None,
            "created_at": created, "head_branch": branch}


def _hist(*items):
    """최신순 runs 목록: 첫 항목이 현재 실행(id=100, 진행 중)."""
    runs = [_run(100, status="in_progress", created="2026-11-04T13:00:00Z")]
    for i, c in enumerate(items):
        runs.append(_run(99 - i, conclusion=c, created=f"2026-11-04T12:{50 - i * 10:02d}:00Z"))
    return runs


class _Resp(io.BytesIO):
    def __init__(self, body=b"", status=200):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeNet:
    def __init__(self, runs=None, gh_exc=None, auth=200, auth_exc=None, tg_status=200, tg_exc=None):
        self.runs, self.gh_exc = runs, gh_exc
        self.auth, self.auth_exc = auth, auth_exc
        self.tg_status, self.tg_exc = tg_status, tg_exc
        self.calls = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.calls.append((url, req.data, dict(req.headers), timeout))
        if "api.github.com" in url:
            if self.gh_exc:
                raise self.gh_exc
            return _Resp(json.dumps({"workflow_runs": self.runs}).encode())
        if "/auth/v1/health" in url:
            if self.auth_exc:
                raise self.auth_exc
            if self.auth != 200:
                raise urllib.error.HTTPError(url, self.auth, "x", {}, None)
            return _Resp(b"{}", 200)
        if "api.telegram.org" in url:
            if self.tg_exc:
                raise self.tg_exc
            if self.tg_status >= 400:
                raise urllib.error.HTTPError(url, self.tg_status, "x", {}, None)
            return _Resp(b"{}", self.tg_status)
        raise AssertionError(f"예상 밖 호출 {url}")

    def telegram(self):
        return [c for c in self.calls if "api.telegram.org" in c[0]]


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.outcome_path = os.path.join(self.tmp.name, "outcome.json")

    def write_outcome(self, status, **kw):
        data = {"mode": "news_top_only", "status": status, "saves_ok": 0,
                "required_save_failed": ["keyword_cache:daum"] if status == "FAILED" else [],
                "failure_kinds": {"http_522": 7} if status == "FAILED" else {},
                "db_unavailable": status == "FAILED", "fetch_failed": [], "news_top": "saved"}
        data.update(kw)
        with open(self.outcome_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def env(self, step="failure", **kw):
        e = {"CRAWL_OUTCOME_PATH": self.outcome_path, "CRAWL_STEP_OUTCOME": step,
             "GITHUB_RUN_ID": "100", "GITHUB_REPOSITORY": "kskedu/community-trend-crawler",
             "GITHUB_API_URL": "https://api.github.com", "GITHUB_SERVER_URL": "https://github.com",
             "GITHUB_TOKEN": GH_TOKEN, "TELEGRAM_BOT_TOKEN": TG_TOKEN, "TELEGRAM_CHAT_ID": "42",
             "SUPABASE_URL": "https://fake.invalid", "SUPABASE_KEY": SB_KEY}
        e.update(kw)
        return e

    def go(self, net, env):
        with patch.object(ca.urllib.request, "urlopen", side_effect=net), \
             self.assertLogs("crawl_alert", level="INFO") as logs:
            result = ca.run(env)
        text = "\n".join(logs.output)
        for secret in (TG_TOKEN, GH_TOKEN, SB_KEY):
            self.assertNotIn(secret, text)
        return result, text


class TestState(_Base):
    def test_states(self):
        self.write_outcome("FAILED")
        self.assertEqual(ca.current_state(ca.read_outcome(self.outcome_path), "failure"), "FAILED")
        self.write_outcome("OK")
        self.assertEqual(ca.current_state(ca.read_outcome(self.outcome_path), "success"), "OK")
        self.write_outcome("PARTIAL")
        self.assertEqual(ca.current_state(ca.read_outcome(self.outcome_path), "success"), "OK")
        # OK 요약인데 step 실패(요약 뒤 다른 실패) → 원인 미상 실패
        self.assertEqual(ca.current_state(ca.read_outcome(self.outcome_path), "failure"), "FAILED_UNKNOWN")

    def test_missing_or_corrupt_outcome_is_fail_closed(self):
        for content in (None, "", "{not json", "[]", json.dumps({"status": "WEIRD"})):
            with self.subTest(content=content):
                if content is None:
                    if os.path.exists(self.outcome_path):
                        os.remove(self.outcome_path)
                else:
                    with open(self.outcome_path, "w") as f:
                        f.write(content)
                o = ca.read_outcome(self.outcome_path)
                self.assertIsNone(o)
                self.assertEqual(ca.current_state(o, "failure"), "FAILED_UNKNOWN")
                # 실행은 성공했는데 근거가 없으면 정상으로 보지 않는다(복구 선언 금지).
                self.assertEqual(ca.current_state(o, "success"), "UNKNOWN")
        self.assertIsNone(ca.read_outcome(""))

    def test_cancelled_and_skipped(self):
        self.assertEqual(ca.current_state(None, "cancelled"), "CANCELLED")
        self.write_outcome("FAILED")
        self.assertEqual(ca.current_state(ca.read_outcome(self.outcome_path), "cancelled"), "CANCELLED")
        self.assertEqual(ca.current_state(None, "skipped"), "FAILED_UNKNOWN")


class TestDecide(unittest.TestCase):
    H = staticmethod(lambda *c: [(x, f"2026-11-04T12:0{i}:00Z") for i, x in enumerate(c)])

    def test_matrix(self):
        cases = [
            ("FAILED", (), "NONE"),                              # 첫 실패(기록 없음)
            ("FAILED", ("success",), "NONE"),                    # 첫 실패
            ("FAILED", ("failure",), "DOWN"),                    # 2회째, 그 이전 기록 없음
            ("FAILED", ("failure", "success"), "DOWN"),          # 2회째
            ("FAILED_UNKNOWN", ("failure", "success"), "DOWN"),
            ("FAILED", ("failure", "failure"), "NONE"),          # 3회째 이후 반복 금지
            ("FAILED", ("failure", "failure", "failure"), "NONE"),
            ("OK", ("failure", "failure"), "RECOVERED"),         # 알림 보낸 장애에서 복구
            ("OK", ("failure", "success"), "NONE"),              # 실패 1회 뒤 정상 — 알림 안 보냈음
            ("OK", ("success", "failure", "failure"), "NONE"),   # 이미 복구 알림 뒤
            ("OK", (), "NONE"),
            ("UNKNOWN", ("failure", "failure"), "NONE"),         # 근거 없는 복구 금지
            ("CANCELLED", ("failure",), "NONE"),
        ]
        for state, hist, want in cases:
            with self.subTest(state=state, hist=hist):
                self.assertEqual(ca.decide(state, self.H(*hist), False)[0], want)

    def test_earlier_active_defers(self):
        self.assertEqual(ca.decide("FAILED", self.H("failure", "success"), True), ("NONE", "earlier_run_active"))


class TestSplitHistory(unittest.TestCase):
    def test_filters_other_branch_later_and_cancelled(self):
        runs = [
            _run(101, conclusion="failure", created="2026-11-04T13:05:00Z"),   # 현재보다 나중 생성 → 제외
            _run(100, status="in_progress", created="2026-11-04T13:00:00Z"),  # 현재
            _run(99, conclusion="failure", created="2026-11-04T12:50:00Z", branch="test"),  # 다른 브랜치
            _run(98, conclusion="cancelled", created="2026-11-04T12:40:00Z"),
            _run(97, conclusion="failure", created="2026-11-04T12:30:00Z"),
            _run(96, conclusion="success", created="2026-11-04T12:00:00Z"),
        ]
        hist, active = ca.split_history(runs, 100)
        self.assertEqual([c for c, _ in hist], ["failure", "success"])
        self.assertFalse(active)

    def test_earlier_in_progress_flags_active(self):
        runs = [_run(100, status="in_progress", created="2026-11-04T13:00:00Z"),
                _run(99, status="in_progress", created="2026-11-04T12:50:00Z"),
                _run(98, conclusion="failure", created="2026-11-04T12:40:00Z")]
        self.assertTrue(ca.split_history(runs, 100)[1])

    def test_current_not_found(self):
        with self.assertRaises(ca.HistoryError):
            ca.split_history([_run(1)], 100)


class TestRunEndToEnd(_Base):
    def test_second_failure_sends_one_alert_without_secrets(self):
        self.write_outcome("FAILED")
        net = _FakeNet(runs=_hist("failure", "success"), auth_exc=urllib.error.URLError(TimeoutError()))
        r, log = self.go(net, self.env())
        self.assertEqual((r["decision"], r["sent"]), ("DOWN", True))
        tg = net.telegram()
        self.assertEqual(len(tg), 1)
        body = tg[0][1].decode()
        self.assertIn("2%ED%9A%8C", body)  # "2회" URL 인코딩
        for secret in (GH_TOKEN, SB_KEY):
            self.assertNotIn(secret, body)
        self.assertIn("decision=DOWN", log)
        # 모든 외부 호출에 timeout 이 있다.
        self.assertTrue(all(c[3] == ca.HTTP_TIMEOUT_SECONDS for c in net.calls))
        # GitHub 토큰은 Authorization 헤더로만, Supabase 키는 apikey 헤더로만 간다.
        gh = [c for c in net.calls if "api.github.com" in c[0]][0]
        self.assertNotIn(GH_TOKEN, gh[0])

    def test_message_content(self):
        self.write_outcome("FAILED")
        net = _FakeNet(runs=_hist("failure", "success"), auth_exc=urllib.error.URLError(TimeoutError()))
        with patch.object(ca, "send_telegram", return_value=True) as st, \
             patch.object(ca.urllib.request, "urlopen", side_effect=net):
            ca.run(self.env())
        msg = st.call_args[0][2]
        self.assertIn("2회 연속 실패", msg)
        self.assertIn("http_522 7건", msg)
        self.assertIn("DB 응답 없음 판정: 예", msg)
        self.assertIn("Auth health: 응답 없음(10초 초과)", msg)
        self.assertIn("마지막 성공 실행: 2026-11-04 21:40 KST", msg)
        self.assertIn("https://github.com/kskedu/community-trend-crawler/actions/runs/100", msg)

    def test_third_failure_sends_nothing(self):
        self.write_outcome("FAILED")
        net = _FakeNet(runs=_hist("failure", "failure", "success"))
        r, _ = self.go(net, self.env())
        self.assertEqual(r["decision"], "NONE")
        self.assertEqual(net.telegram(), [])
        self.assertFalse(any("/auth/v1/health" in c[0] for c in net.calls))

    def test_recovery_sends_once(self):
        self.write_outcome("OK")
        net = _FakeNet(runs=_hist("failure", "failure", "success"))
        with patch.object(ca, "send_telegram", return_value=True) as st, \
             patch.object(ca.urllib.request, "urlopen", side_effect=net):
            r = ca.run(self.env(step="success"))
        self.assertEqual(r["decision"], "RECOVERED")
        self.assertIn("복구", st.call_args[0][2])
        self.assertIn("연속 실패 시작: 2026-11-04 21:40 KST", st.call_args[0][2])

    def test_recovery_needs_outcome_evidence(self):
        # 실행은 성공(exit 0)인데 결과 파일이 없으면 복구로 보지 않는다.
        net = _FakeNet(runs=_hist("failure", "failure"))
        r, _ = self.go(net, self.env(step="success"))
        self.assertEqual((r["state"], r["decision"]), ("UNKNOWN", "NONE"))

    def test_github_api_errors_send_nothing(self):
        self.write_outcome("FAILED")
        for exc in (urllib.error.HTTPError("u", 403, "x", {}, None), urllib.error.URLError("down"),
                    TimeoutError()):
            with self.subTest(exc=type(exc).__name__):
                net = _FakeNet(gh_exc=exc)
                r, log = self.go(net, self.env())
                self.assertEqual(r["decision"], "NONE")
                self.assertIn("history_unavailable", r["reason"])
                self.assertEqual(net.telegram(), [])

    def test_bad_api_shape_and_missing_current(self):
        self.write_outcome("FAILED")
        for runs in (None, [_run(1, conclusion="failure"), _run(2, conclusion="success")]):
            with self.subTest(runs=runs):
                net = _FakeNet(runs=runs)
                r, _ = self.go(net, self.env())
                self.assertEqual(r["decision"], "NONE")

    def test_bad_run_id(self):
        self.write_outcome("FAILED")
        r, _ = self.go(_FakeNet(runs=_hist("failure")), self.env(GITHUB_RUN_ID=""))
        self.assertEqual((r["decision"], r["reason"]), ("NONE", "history_unavailable:run_id"))

    def test_telegram_failure_is_logged_without_token(self):
        self.write_outcome("FAILED")
        for kw in ({"tg_status": 401}, {"tg_exc": urllib.error.URLError("x")}, {"tg_exc": TimeoutError()}):
            with self.subTest(kw=kw):
                net = _FakeNet(runs=_hist("failure", "success"), **kw)
                r, log = self.go(net, self.env())
                self.assertEqual((r["decision"], r["sent"]), ("DOWN", False))
                self.assertIn("Telegram 전송 실패", log)

    def test_missing_telegram_config(self):
        self.write_outcome("FAILED")
        net = _FakeNet(runs=_hist("failure", "success"))
        r, _ = self.go(net, self.env(TELEGRAM_BOT_TOKEN=""))
        self.assertEqual(r["sent"], False)
        self.assertEqual(net.telegram(), [])

    def test_main_always_exits_zero(self):
        with patch.object(ca, "run", side_effect=RuntimeError("boom")):
            self.assertEqual(ca.main(), 0)

    def test_step_summary_written(self):
        self.write_outcome("FAILED")
        summary = os.path.join(self.tmp.name, "summary.md")
        self.go(_FakeNet(runs=_hist("success")), self.env(GITHUB_STEP_SUMMARY=summary))
        with open(summary, encoding="utf-8") as f:
            self.assertIn("[crawl-alert] state=FAILED decision=NONE reason=first_failure", f.read())


class TestCrawlWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, ".github", "workflows", "crawl.yml"), encoding="utf-8") as f:
            cls.wf = yaml.safe_load(f)
        cls.steps = {s.get("name", s.get("uses")): s for s in cls.wf["jobs"]["crawl"]["steps"]}

    def test_permissions_minimal(self):
        self.assertEqual(self.wf["permissions"], {"contents": "read", "actions": "read"})

    def test_triggers_unchanged(self):
        triggers = self.wf.get("on", self.wf.get(True))
        self.assertEqual(list(triggers), ["workflow_dispatch"])  # 새 schedule·push 트리거 없음

    def test_crawler_step(self):
        s = self.steps["Run crawler"]
        self.assertEqual(s["id"], "crawl")
        self.assertEqual(s["timeout-minutes"], 20)
        self.assertEqual(s["env"]["CRAWL_OUTCOME_PATH"], "${{ runner.temp }}/crawl-outcome.json")

    def test_alert_step(self):
        names = [s.get("name") for s in self.wf["jobs"]["crawl"]["steps"]]
        self.assertEqual(names[-1], "Alert on repeated DB save failure")
        s = self.steps["Alert on repeated DB save failure"]
        self.assertEqual(s["if"], "always()")
        self.assertIs(s["continue-on-error"], True)
        self.assertLessEqual(s["timeout-minutes"], 5)
        self.assertEqual(s["env"]["CRAWL_OUTCOME_PATH"], self.steps["Run crawler"]["env"]["CRAWL_OUTCOME_PATH"])
        self.assertEqual(s["env"]["CRAWL_STEP_OUTCOME"], "${{ steps.crawl.outcome }}")
        self.assertEqual(s["env"]["CRAWL_WORKFLOW_FILE"], "crawl.yml")
        self.assertEqual(s["run"].strip(), "python -m ops.crawl_alert")
        # 재실행·dispatch 없음
        self.assertNotIn("gh workflow run", s["run"])
        self.assertNotIn("dispatches", open(ca.__file__, encoding="utf-8").read())


if __name__ == "__main__":
    unittest.main()
