"""커뮤니티 게시물 저장 실패 감지 회귀 테스트.

2026-11-02 장애 때 full 실행 7회가 community_posts 저장 0건(upsert 예외)인데도
GitHub Actions 에 success 로 표시됐다. upsert_posts() 가 예외를 0 으로 바꾸고 run() 이
종료 코드에 반영하지 않았기 때문이다. 이 테스트는 다음 계약을 고정한다.

- upsert_posts: 정상 = 건수(int), 빈 입력·정상 응답의 빈 data = 0, API 예외 = None.
- run(): 저장 실패여도 키워드·뉴스를 끝까지 수행하고, 실패 여부만 반환한다.
- _main(): full 은 저장 실패일 때만 1, news_top_only·선택 기능 실패는 0.

실제 Supabase·외부 사이트는 호출하지 않는다(fake client·fake scraper).
"""
import logging
import os
import sys
import unittest
from datetime import datetime
from unittest.mock import patch

import httpx
from postgrest.exceptions import APIError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as main_module
from db import supabase as db_supabase
from models import Post

_SECRET = "sb_secret_TEST_ONLY_do_not_log"


def _posts(n=2):
    return [
        Post(title=f"제목 {i} 테스트", source_url=f"https://example.test/post/{i}",
             source_site="clien", created_at=datetime(2026, 11, 3, 9, 0, 0))
        for i in range(n)
    ]


class _Resp:
    def __init__(self, data):
        self.data = data


class _FakeClient:
    """client.table(name).upsert(rows, on_conflict=...).execute() 만 흉내 낸다."""

    def __init__(self, data=None, exc=None):
        self._data = data
        self._exc = exc
        self.calls = []

    def table(self, name):
        self.calls.append(("table", name))
        return self

    def upsert(self, rows, on_conflict=None):
        self.calls.append(("upsert", len(rows), on_conflict))
        return self

    def execute(self):
        if self._exc is not None:
            raise self._exc
        return _Resp(self._data)


def _api_error_522():
    return APIError({"message": "JSON could not be generated", "code": 522,
                     "hint": "Refer to full message for details", "details": None})


class TestUpsertPostsContract(unittest.TestCase):

    def _upsert(self, client, posts):
        with patch.object(db_supabase, "get_client", return_value=client):
            return db_supabase.upsert_posts(posts)

    def test_success_returns_count(self):
        client = _FakeClient(data=[{"id": 1}, {"id": 2}])
        self.assertEqual(self._upsert(client, _posts(2)), 2)
        self.assertIn(("upsert", 2, "source_url"), client.calls)

    def test_empty_input_returns_zero_without_client(self):
        with patch.object(db_supabase, "get_client") as gc:
            self.assertEqual(db_supabase.upsert_posts([]), 0)
        gc.assert_not_called()

    def test_success_with_empty_data_is_zero_not_failure(self):
        # 시나리오 F: 2xx 정상 응답인데 data 가 비어도 실패로 오판하지 않는다.
        for data in ([], None):
            with self.subTest(data=data):
                result = self._upsert(_FakeClient(data=data), _posts(2))
                self.assertEqual(result, 0)
                self.assertIsNotNone(result)

    def test_api_and_transport_errors_return_none(self):
        # 시나리오 B: 11-02 장애 로그에 실제로 나온 형태(522 APIError, read timeout).
        for exc in (_api_error_522(), httpx.ReadTimeout("The read operation timed out")):
            with self.subTest(exc=type(exc).__name__):
                self.assertIsNone(self._upsert(_FakeClient(exc=exc), _posts(2)))

    def test_programming_error_outside_api_call_is_not_swallowed(self):
        # 행 변환 같은 호출 밖 개발 오류는 실패 상태로 숨기지 않고 그대로 올린다.
        bad = Post(title="제목 테스트 글", source_url="https://example.test/bad",
                   source_site="clien", created_at="2026-11-03")  # str 에는 isoformat 없음
        with self.assertRaises(AttributeError):
            self._upsert(_FakeClient(data=[]), [bad])


class _FakePostScraper:
    site_id = "fake_site"

    def __init__(self, posts):
        self._posts = posts

    def scrape(self):
        return list(self._posts)


class _FakeKeywordScraper:
    def __init__(self, source):
        self.source = source
        self.active = True

    def scrape(self):
        return [{"keyword": "테스트", "url": "https://x"}]


class TestRunPropagatesPostSaveFailure(unittest.TestCase):
    """run() 이 저장 실패를 기억하고, 후속 작업을 막지 않고, 끝에 반환하는지."""

    def _run(self, posts, saved, keyword_ok=True, briefing=None):
        calls = []

        def fake_upsert(p):
            calls.append(("posts", len(p)))
            return saved(p) if callable(saved) else saved

        def fake_keywords(source, items):
            calls.append(("keyword", source))
            return keyword_ok

        def fake_briefing(run_type="full"):
            calls.append(("briefing", run_type))
            if briefing:
                briefing()

        patches = [
            patch.object(main_module, "SCRAPERS", [_FakePostScraper(posts)]),
            patch.object(main_module, "KEYWORD_SCRAPERS", [_FakeKeywordScraper("daum")]),
            patch.object(main_module, "dedup", side_effect=lambda p: p),
            patch.object(main_module, "filter_notices", side_effect=lambda p: p),
            patch.object(main_module, "score_all", side_effect=lambda p: p),
            patch.object(main_module, "upsert_posts", side_effect=fake_upsert),
            patch.object(main_module, "upsert_keywords", side_effect=fake_keywords),
            patch.object(main_module, "run_news_briefing", side_effect=fake_briefing),
        ]
        for p in patches:
            p.start()
        try:
            with self.assertLogs(main_module.logger, level="INFO") as logs:
                result = main_module.run()
        finally:
            for p in reversed(patches):
                p.stop()
        return result, calls, "\n".join(logs.output)

    def test_success_returns_true(self):
        # 시나리오 A
        ok, calls, log = self._run(_posts(2), saved=2)
        self.assertIs(ok, True)
        self.assertIn("저장 완료: 2건", log)

    def test_failure_returns_false_and_still_runs_keywords_and_news(self):
        # 시나리오 B·C·D·J: 실패 뒤 키워드·뉴스가 모두 성공해도 실패 상태가 유지된다.
        ok, calls, log = self._run(_posts(3), saved=None)
        self.assertIs(ok, False)
        self.assertEqual(calls, [("posts", 3), ("keyword", "daum"), ("briefing", "full")])
        self.assertIn("커뮤니티 게시물 저장 실패: 3건 미저장", log)
        # "정상 저장 0건" 으로 오해할 로그를 남기지 않는다.
        self.assertNotIn("저장 완료", log)

    def test_no_posts_collected_is_normal(self):
        # 시나리오 E: 수집 0건은 실제 upsert_posts 의 빈 입력 경로(0) 그대로 정상이다.
        with patch.object(db_supabase, "get_client") as gc:
            ok, calls, log = self._run([], saved=db_supabase.upsert_posts)
        gc.assert_not_called()
        self.assertIs(ok, True)
        self.assertIn("저장 완료: 0건", log)

    def test_optional_feature_failures_keep_success(self):
        # 시나리오 H: 키워드 저장 실패·뉴스 브리핑 내부 실패는 기존처럼 종료 코드와 무관하다.
        ok, calls, log = self._run(_posts(2), saved=2, keyword_ok=False)
        self.assertIs(ok, True)
        self.assertIn("[daum] 키워드 저장 실패", log)

    def test_failure_log_does_not_expose_secret_or_payload(self):
        # 시나리오 I: 새로 추가한 실패 로그는 건수만 남긴다(키·제목·URL 없음).
        with patch.dict(os.environ, {"SUPABASE_KEY": _SECRET}):
            ok, calls, log = self._run(_posts(2), saved=None)
        self.assertIs(ok, False)
        for leaked in (_SECRET, "example.test", "제목 0"):
            self.assertNotIn(leaked, log)


class TestRealNewsBriefingIsolationKeepsSuccess(unittest.TestCase):
    def test_news_briefing_internal_exception_does_not_change_exit(self):
        # 시나리오 H(뉴스): 실제 run_news_briefing 의 기존 예외 격리를 거쳐도 종료 코드 0.
        with patch.object(main_module, "SCRAPERS", [_FakePostScraper(_posts(1))]), \
             patch.object(main_module, "KEYWORD_SCRAPERS", []), \
             patch.object(main_module, "dedup", side_effect=lambda p: p), \
             patch.object(main_module, "filter_notices", side_effect=lambda p: p), \
             patch.object(main_module, "score_all", side_effect=lambda p: p), \
             patch.object(main_module, "upsert_posts", return_value=1), \
             patch.object(main_module, "_collect_home_seeds", side_effect=RuntimeError("seed down")), \
             patch.object(main_module, "_finalize_diagnostics"), \
             self.assertLogs(main_module.logger, level="ERROR") as logs:
            self.assertEqual(main_module._main("full"), 0)
        self.assertTrue(any("실시간 이슈 브리핑 실패" in line for line in logs.output))


class TestMainExitCode(unittest.TestCase):

    def test_full_exit_code_follows_run_result(self):
        with patch.object(main_module, "run", return_value=True):
            self.assertEqual(main_module._main("full"), 0)
        with patch.object(main_module, "run", return_value=False):
            self.assertEqual(main_module._main("full"), 1)

    def test_news_top_only_unchanged(self):
        # 시나리오 G: 커뮤니티 수집·저장 없이 키워드 → 뉴스 순서, 종료 코드 0.
        order = []
        with patch.object(main_module, "run") as run_mock, \
             patch.object(main_module, "upsert_posts") as up_mock, \
             patch.object(main_module, "_collect_keyword_caches",
                          side_effect=lambda status: order.append(("keywords", status))), \
             patch.object(main_module, "run_news_briefing",
                          side_effect=lambda run_type: order.append(("briefing", run_type))):
            self.assertEqual(main_module._main("news_top_only"), 0)
        run_mock.assert_not_called()
        up_mock.assert_not_called()
        self.assertEqual(order, [("keywords", {}), ("briefing", "news_top_only")])

    def test_script_entry_passes_exit_code_to_process(self):
        # __main__ 블록은 import 로 실행되지 않으므로 소스로 고정한다: _main 의 반환값이
        # sys.exit 로 프로세스 종료 코드가 돼야 crawl.yml 의 step 이 실패한다.
        with open(main_module.__file__, encoding="utf-8") as f:
            src = f.read()
        entry = src.split('if __name__ == "__main__":', 1)[1]
        self.assertIn("sys.exit(_main(mode))", entry)

    def test_unknown_mode_still_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            main_module._main("bogus")
        self.assertIn("Unknown mode", str(cm.exception.code))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    unittest.main()
