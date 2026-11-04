"""실행 결과 판정·DB 장애 분류 회귀 테스트 (2026-11-04 Supabase 장애 후속).

11-04 21:47 news_top_only 실행은 keyword_cache 저장이 전부 HTTP 522 로 실패했는데
GitHub Actions 에 success 로 표시됐고(종료 코드가 커뮤니티 게시물 저장만 봤다), 요청마다
라이브러리 기본 120초를 기다려 13분 걸렸으며, 로그에 Cloudflare 오류 HTML 이 그대로 남았다.
이 테스트는 다음 계약을 고정한다.

- classify_db_error: 상태 코드·예외 종류만 보고 분류한다(원문을 보지 않는다).
- _guarded_write: 일시 장애만 1회 재시도, 일시 장애로 끝난 호출 2건이면 이후 저장은 호출 없이
  실패(db_unavailable). 4xx·PostgREST JSON 오류는 재시도하지 않는다.
- summarize_run / _main: 필수 저장 실패 = FAILED = 종료 코드 1, 외부 수집 실패 = PARTIAL = 0,
  의도된 가드 생략 = OK. DB 무응답 중의 news_top 생략은 FAILED 다.
- 어떤 로그에도 응답 원문·비밀값이 남지 않는다.

실제 Supabase·외부 사이트는 호출하지 않는다.
"""
import logging
import os
import sys
import unittest
from unittest.mock import patch

import httpx
from postgrest.exceptions import APIError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as main_module
from db import supabase as db_supabase
from news_naver_press import collector as press_collector

_SECRET = "sb_secret_TEST_ONLY_do_not_log"
_CF_HTML = ("<!DOCTYPE html><title>xyunhewepaxdrnajrttv.supabase.co | 522: Connection timed out"
            f"</title><span>Error code 522</span> apikey={_SECRET}")


def _api_error(code, details=_CF_HTML):
    return APIError({"message": "JSON could not be generated", "code": code,
                     "hint": "Refer to full message for details", "details": details})


class _Resp:
    def __init__(self, data):
        self.data = data


class _FakeClient:
    """table().upsert().execute() / table().select()...execute() / rpc().execute() 흉내.

    script: 호출 순서대로 꺼내 쓰는 결과 목록(예외면 raise, 아니면 data). 비면 마지막 항목 반복.
    """

    def __init__(self, script):
        self.script = list(script)
        self.executes = 0

    def table(self, name):
        return self

    def rpc(self, name, params):
        return self

    def upsert(self, row, on_conflict=None):
        return self

    def execute(self):
        self.executes += 1
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        return _Resp(item)


class _Base(unittest.TestCase):
    def setUp(self):
        db_supabase.ledger.reset()
        p = patch.object(db_supabase.time, "sleep")
        self.sleep = p.start()
        self.addCleanup(p.stop)

    def with_client(self, client):
        p = patch.object(db_supabase, "get_client", return_value=client)
        p.start()
        self.addCleanup(p.stop)
        return client


class TestClassify(unittest.TestCase):
    def test_kinds(self):
        cases = [
            (_api_error(522), "http_522"),
            (_api_error("524"), "http_524"),
            (_api_error(504), "http_504"),
            (_api_error(401), "http_401"),
            (APIError({"message": "dup", "code": "23505", "hint": None, "details": None}), "api_error"),
            (APIError({"message": "x", "code": "PGRST301", "hint": None, "details": None}), "api_error"),
            (httpx.ReadTimeout("timed out"), "timeout"),
            (httpx.ConnectTimeout("timed out"), "timeout"),
            (httpx.ConnectError("refused"), "network"),
            (ValueError("x"), "unknown"),
        ]
        for exc, want in cases:
            with self.subTest(want=want):
                self.assertEqual(db_supabase.classify_db_error(exc), want)

    def test_transient_set(self):
        for k in ("http_522", "http_524", "http_504", "http_520", "http_521", "http_502",
                  "http_503", "timeout", "network"):
            self.assertTrue(db_supabase.is_transient(k), k)
        for k in ("http_400", "http_401", "http_409", "http_500", "api_error", "unknown",
                  "db_unavailable", "http_x"):
            self.assertFalse(db_supabase.is_transient(k), k)

    def test_client_timeout_is_bounded(self):
        # 라이브러리 기본 120초를 쓰지 않는다.
        self.assertLessEqual(db_supabase.DB_REQUEST_TIMEOUT_SECONDS, 30)


class TestGuardedWrite(_Base):
    def test_transient_then_success_retries_once(self):
        c = self.with_client(_FakeClient([_api_error(522), [{"source": "daum"}]]))
        self.assertTrue(db_supabase.upsert_keywords("daum", [{"keyword": "a", "url": "u"}]))
        self.assertEqual(c.executes, 2)
        self.sleep.assert_called_once_with(db_supabase.TRANSIENT_RETRY_DELAY_SECONDS)
        self.assertEqual(db_supabase.ledger.entries, [("keyword_cache", "daum", "ok", None)])
        self.assertFalse(db_supabase.ledger.db_unavailable)

    def test_transient_twice_fails_after_two_attempts(self):
        c = self.with_client(_FakeClient([httpx.ReadTimeout("t")]))
        self.assertFalse(db_supabase.upsert_keywords("daum", [{"keyword": "a", "url": "u"}]))
        self.assertEqual(c.executes, 2)
        self.assertEqual(db_supabase.ledger.entries, [("keyword_cache", "daum", "failed", "timeout")])

    def test_non_transient_not_retried_and_not_counted(self):
        c = self.with_client(_FakeClient([_api_error(401)]))
        for src in ("daum", "nate", "msn"):
            self.assertFalse(db_supabase.upsert_keywords(src, [{"keyword": "a", "url": "u"}]))
        self.assertEqual(c.executes, 3)  # 재시도 없음, 차단도 없음(DB 는 응답하고 있다)
        self.sleep.assert_not_called()
        self.assertFalse(db_supabase.ledger.db_unavailable)

    def test_db_unavailable_after_two_transient_calls_skips_rest(self):
        c = self.with_client(_FakeClient([_api_error(522)]))
        for src in ("danawa", "daum", "daangn", "nate", "msn"):
            db_supabase.upsert_keywords(src, [{"keyword": "a", "url": "u"}])
        self.assertEqual(c.executes, 4)  # 처음 두 source × 2회, 나머지는 호출 없음
        self.assertTrue(db_supabase.ledger.db_unavailable)
        self.assertEqual([e[2] for e in db_supabase.ledger.entries],
                         ["failed", "failed", "skipped_db_unavailable",
                          "skipped_db_unavailable", "skipped_db_unavailable"])
        # 진단 RPC 도 호출하지 않는다.
        self.assertFalse(db_supabase.record_news_diagnostics({"run_key": "k"}, []))
        self.assertEqual(c.executes, 4)

    def test_breaker_counts_consecutive_only(self):
        # 일시 장애 1건 → 성공 → 일시 장애 1건: DB 는 응답하고 있으므로 차단하지 않는다.
        ok = [{"source": "x"}]
        t = _api_error(522)
        c = self.with_client(_FakeClient([t, t, ok, t, t, ok]))
        for src in ("danawa", "daum", "nate", "msn"):
            db_supabase.upsert_keywords(src, [{"keyword": "a", "url": "u"}])
        self.assertFalse(db_supabase.ledger.db_unavailable)
        self.assertEqual([e[2] for e in db_supabase.ledger.entries], ["failed", "ok", "failed", "ok"])
        self.assertEqual(c.executes, 6)

    def test_request_build_errors_are_save_failures_not_crashes(self):
        # 독립 리뷰 P1: 짝 없는 서로게이트 제목 → httpx JSON 인코딩에서 UnicodeEncodeError.
        # 기존 except Exception 계약대로 저장 실패(None/False)로 끝나야 한다(run 중단 금지).
        for exc in (UnicodeEncodeError("utf-8", "a\ud83db", 1, 2, "surrogates not allowed"),
                    ValueError("Out of range float values are not JSON compliant")):
            with self.subTest(exc=type(exc).__name__):
                db_supabase.ledger.reset()
                c = self.with_client(_FakeClient([exc]))
                from tests.test_post_save_failure import _posts
                self.assertIsNone(db_supabase.upsert_posts(_posts(1)))
                self.assertFalse(db_supabase.upsert_keywords("daum", [{"keyword": "a", "url": "u"}]))
                self.assertFalse(db_supabase.upsert_news_issues({"keywords": [1]}))
                self.assertEqual(c.executes, 3)  # 재시도 없음
                self.assertEqual([e[2:] for e in db_supabase.ledger.entries], [("failed", "unknown")] * 3)
                self.assertFalse(db_supabase.ledger.db_unavailable)

    def test_real_surrogate_title_through_httpx_is_save_failure(self):
        # 실제 supabase/httpx 경로(가짜 client 없이)에서 요청 조립 예외가 저장 실패로 끝나는지.
        # 인코딩 단계에서 실패하므로 네트워크 전송은 일어나지 않는다(127.0.0.1:9 는 쓰이지 않음).
        from datetime import datetime
        from models import Post
        real = db_supabase.create_client(
            "http://127.0.0.1:9", "aaa.bbb.ccc",
            options=db_supabase.SyncClientOptions(postgrest_client_timeout=1))
        self.with_client(real)
        post = Post(title="a\ud83db", source_url="https://x.test/1", source_site="clien",
                    created_at=datetime(2026, 11, 5))
        self.assertIsNone(db_supabase.upsert_posts([post]))
        self.assertEqual(db_supabase.ledger.entries, [("community_posts", "posts", "failed", "unknown")])

    def test_posts_contract_kept(self):
        self.with_client(_FakeClient([_api_error(522)]))
        from tests.test_post_save_failure import _posts
        self.assertIsNone(db_supabase.upsert_posts(_posts(2)))
        db_supabase.ledger.reset()
        self.with_client(_FakeClient([[{"id": 1}, {"id": 2}]]))
        self.assertEqual(db_supabase.upsert_posts(_posts(2)), 2)

    def test_logs_never_carry_raw_response_or_secret(self):
        self.with_client(_FakeClient([_api_error(522), _api_error(400, details=_CF_HTML),
                                      APIError({"message": f"bad {_SECRET}", "code": "PGRST301",
                                                "hint": _CF_HTML, "details": None})]))
        with self.assertLogs(level="DEBUG") as logs:
            db_supabase.upsert_keywords("daum", [{"keyword": "a", "url": "u"}])
            db_supabase.upsert_keywords("nate", [{"keyword": "a", "url": "u"}])
            db_supabase.fetch_news_issues()
            db_supabase.upsert_generic_snapshot("naver_press_popular", {"a": 1})
            db_supabase.record_news_diagnostics({"run_key": "k"}, [])
        text = "\n".join(logs.output)
        for leaked in (_SECRET, "DOCTYPE", "Error code", "supabase.co", "Connection timed out"):
            self.assertNotIn(leaked, text)
        self.assertIn("kind=http_522", text)


class TestSummarize(unittest.TestCase):
    S = staticmethod(main_module.summarize_run)

    def test_ok(self):
        r = self.S("full", {"daum": "ok", "namuwiki": "skipped"}, "saved",
                   [("community_posts", "posts", "ok", None), ("keyword_cache", "daum", "ok", None)], False)
        self.assertEqual(r["status"], "OK")

    def test_intended_news_guard_skip_is_ok(self):
        r = self.S("news_top_only", {"daum": "ok"}, "skipped",
                   [("keyword_cache", "daum", "ok", None)], False)
        self.assertEqual((r["status"], r["news_top"]), ("OK", "skipped"))
        r = self.S("news_top_only", {}, "skipped_stale", [], False)
        self.assertEqual(r["status"], "OK")

    def test_upstream_fetch_failure_is_partial(self):
        r = self.S("full", {"todayhumor": "failed", "daum": "ok"}, "saved",
                   [("keyword_cache", "daum", "ok", None)], False)
        self.assertEqual((r["status"], r["fetch_failed"]), ("PARTIAL", ["todayhumor"]))

    def test_optional_save_failure_is_partial(self):
        r = self.S("full", {}, "saved", [("keyword_cache", "google_trends", "failed", "http_400")], False)
        self.assertEqual(r["status"], "PARTIAL")

    def test_news_exception_without_db_outage_is_partial(self):
        self.assertEqual(self.S("full", {}, "error", [], False)["status"], "PARTIAL")

    def test_required_save_failure_is_failed(self):
        r = self.S("news_top_only", {"daum": "save_failed"}, "saved",
                   [("keyword_cache", "daum", "failed", "http_522")], False)
        self.assertEqual(r["status"], "FAILED")
        self.assertEqual(r["failure_kinds"], {"http_522": 1})

    def test_news_skip_during_db_outage_is_failed(self):
        r = self.S("news_top_only", {}, "skipped",
                   [("keyword_cache", "daum", "failed", "http_522"),
                    ("keyword_cache", "nate", "failed", "http_522")], True)
        self.assertEqual((r["status"], r["news_top"]), ("FAILED", "failed_db_unavailable"))

    def test_news_save_failed_is_failed(self):
        r = self.S("news_top_only", {}, "save_failed",
                   [("news_issue_cache", "news_top", "failed", "timeout")], False)
        self.assertEqual(r["status"], "FAILED")


class _KS:
    def __init__(self, source, items=None, active=True):
        self.source, self.active = source, active
        self._items = items if items is not None else [{"keyword": "k", "url": "u"}]

    def scrape(self):
        return list(self._items)


class TestMainEndToEnd(_Base):
    """news_top_only 를 실제 _collect_keyword_caches·upsert_keywords·장부로 돌린다(뉴스 랭킹만 대역)."""

    def _run(self, client_script, scrapers, briefing=None):
        self.with_client(_FakeClient(client_script))

        def fake_briefing(run_type):
            if briefing:
                briefing()

        with patch.object(main_module, "KEYWORD_SCRAPERS", scrapers), \
             patch.object(main_module, "run_news_briefing", side_effect=fake_briefing), \
             self.assertLogs(level="INFO") as logs:
            code = main_module._main("news_top_only")
        return code, "\n".join(logs.output)

    def _scrapers(self):
        return [_KS("danawa"), _KS("daum"), _KS("daangn"), _KS("namuwiki", active=False),
                _KS("nate"), _KS("msn")]

    def test_1104_outage_replay_exits_1_quickly(self):
        code, log = self._run([_api_error(522)], self._scrapers())
        self.assertEqual(code, 1)
        self.assertIn("[run-summary] mode=news_top_only status=FAILED", log)
        self.assertIn("db_unavailable=true", log)
        self.assertEqual(self.sleep.call_count, 2)  # 재시도 대기는 처음 두 source 만
        for leaked in (_SECRET, "DOCTYPE", "Error code"):
            self.assertNotIn(leaked, log)

    def test_all_ok_exits_0(self):
        code, log = self._run([[{"source": "x"}]], self._scrapers())
        self.assertEqual(code, 0)
        self.assertIn("status=OK", log)

    def test_upstream_empty_is_partial_exit_0(self):
        code, log = self._run([[{"source": "x"}]], [_KS("daum", items=[]), _KS("nate")])
        self.assertEqual(code, 0)
        self.assertIn("status=PARTIAL", log)

    def test_single_save_failure_exits_1_but_others_saved(self):
        script = [[{"s": 1}], _api_error(400), [{"s": 1}]]
        code, log = self._run(script, [_KS("danawa"), _KS("daum"), _KS("nate")])
        self.assertEqual(code, 1)
        self.assertEqual([e[2] for e in db_supabase.ledger.entries], ["ok", "failed", "ok"])

    def test_summary_written_to_outcome_file(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "outcome.json")
            with patch.dict(os.environ, {"CRAWL_OUTCOME_PATH": path}):
                code, _ = self._run([_api_error(522)], self._scrapers())
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        self.assertEqual((code, data["status"], data["db_unavailable"]), (1, "FAILED", True))
        self.assertNotIn(_SECRET, json.dumps(data))


class TestFullModeKeywordSaveFailure(_Base):
    def test_full_keyword_save_failure_now_exits_1(self):
        # 계약 변경(2026-11-04): 이전엔 키워드 저장 실패가 종료 코드 0 이었다(#52 시나리오 H).
        # 게시물 저장이 성공해도 키워드 DB 저장 실패는 1 이다. 수집·뉴스는 끝까지 돈다.
        from tests.test_post_save_failure import _FakePostScraper, _posts
        calls = []
        with patch.object(main_module, "SCRAPERS", [_FakePostScraper(_posts(2))]), \
             patch.object(main_module, "KEYWORD_SCRAPERS", [_KS("daum")]), \
             patch.object(main_module, "dedup", side_effect=lambda p: p), \
             patch.object(main_module, "filter_notices", side_effect=lambda p: p), \
             patch.object(main_module, "score_all", side_effect=lambda p: p), \
             patch.object(main_module, "upsert_posts", return_value=2), \
             patch.object(main_module, "run_news_briefing",
                          side_effect=lambda run_type="full": calls.append("briefing")):
            self.with_client(_FakeClient([_api_error(400)]))
            self.assertEqual(main_module._main("full"), 1)
        self.assertEqual(calls, ["briefing"])


class TestNonHttpSaveErrorStillFails(_Base):
    def test_keyword_encode_error_is_failed_not_partial(self):
        exc = UnicodeEncodeError("utf-8", "x", 0, 1, "surrogates not allowed")
        self.with_client(_FakeClient([exc]))
        with patch.object(main_module, "KEYWORD_SCRAPERS", [_KS("daum")]), \
             patch.object(main_module, "run_news_briefing"), \
             self.assertLogs(level="INFO") as logs:
            self.assertEqual(main_module._main("news_top_only"), 1)
        self.assertIn("status=FAILED", "\n".join(logs.output))


class TestNaverPressWriteFailure(_Base):
    def test_522_keeps_cache_and_logs_kind_only(self):
        self.with_client(_FakeClient([_api_error(522)]))
        with self.assertLogs(level="INFO") as logs:
            self.assertFalse(db_supabase.upsert_generic_snapshot("naver_press_popular", {"p": [1]}))
        text = "\n".join(logs.output)
        self.assertIn("kind=http_522", text)
        self.assertNotIn("DOCTYPE", text)
        # 기존 collector 계약: write 실패 → skipped_cache_write_failed(→ 프로세스 1)는 그대로다.
        self.assertIn("skipped_cache_write_failed", open(press_collector.__file__, encoding="utf-8").read())


if __name__ == "__main__":
    unittest.main()


class _RaisingQuery:
    """select().eq().maybe_single().execute() 등 어떤 체인이든 execute 에서 예외."""

    def __init__(self, exc):
        self.exc = exc

    def __getattr__(self, name):
        if name == "execute":
            def _raise():
                raise self.exc
            return _raise
        return lambda *a, **k: self


class TestReadPathLogsKindOnly(unittest.TestCase):
    def test_seed_and_filter_read_failures(self):
        from news import seed
        from processor import filter as pfilter
        exc = _api_error(522)
        with patch.object(db_supabase, "get_client", return_value=_RaisingQuery(exc)), \
             self.assertLogs(level="DEBUG") as logs:
            seed.fetch_daum_seed()
            seed.fetch_ranked_seed_status("nate")
            self.assertFalse(pfilter.load_filters_from_db())
        text = "\n".join(logs.output)
        self.assertEqual(text.count("kind=http_522"), 3)
        for leaked in (_SECRET, "DOCTYPE", "Error code", "supabase.co"):
            self.assertNotIn(leaked, text)
