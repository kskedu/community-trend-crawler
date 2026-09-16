"""네이버 언론사별 인기뉴스 collector 통합 테스트 (unittest, 네트워크/DB 없음).

검증 항목(구현 계획 §9):
19. parser 실패 시 cache overwrite 금지
20. 정상 성공 시 snapshot overwrite
- fetch 실패 시에도 cache overwrite 금지
- cache write 실패 시에도 실패로 보고(성공 아님)
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news_naver_press import collector as C
from news_naver_press.fetcher import FetchResult
from news_naver_press.parser import ParseResult, PressTopItem


class TestCollector(unittest.TestCase):
    def test_fetch_failed_no_cache_write(self):
        with patch.object(C, "fetch_popular_day", return_value=FetchResult(ok=False, reason="BLOCKED", status_code=403)), \
             patch.object(C, "upsert_generic_snapshot") as mock_upsert:
            outcome = C.run_naver_press_popular_collection()
        mock_upsert.assert_not_called()
        self.assertEqual(outcome.status, "skipped_fetch_failed")
        self.assertTrue(outcome.previous_cache_preserved)

    def test_parse_failed_no_cache_write(self):
        with patch.object(C, "fetch_popular_day", return_value=FetchResult(ok=True, status_code=200, html="<html></html>")), \
             patch.object(C, "parse_popular_day_html", return_value=ParseResult(ok=False, reason="STRUCTURE_CHANGED_OR_BLOCKED", box_count=2)), \
             patch.object(C, "upsert_generic_snapshot") as mock_upsert:
            outcome = C.run_naver_press_popular_collection()
        mock_upsert.assert_not_called()
        self.assertEqual(outcome.status, "skipped_parse_failed")
        self.assertTrue(outcome.previous_cache_preserved)

    def test_success_writes_snapshot(self):
        item = PressTopItem(
            press_id="015", press_name="테스트", press_logo=None, rank=1,
            article_id="123", title="제목", article_url="https://n.news.naver.com/article/015/123",
            thumbnail=None, time_label="1시간전",
        )
        parse_ok = ParseResult(ok=True, items=[item], box_count=40, valid_item_count=1)
        with patch.object(C, "fetch_popular_day", return_value=FetchResult(ok=True, status_code=200, html="<html></html>")), \
             patch.object(C, "parse_popular_day_html", return_value=parse_ok), \
             patch.object(C, "upsert_generic_snapshot", return_value=True) as mock_upsert:
            outcome = C.run_naver_press_popular_collection()

        mock_upsert.assert_called_once()
        source_arg, payload_arg = mock_upsert.call_args[0]
        self.assertEqual(source_arg, C.CACHE_SOURCE)
        self.assertEqual(payload_arg["item_count"], 1)
        self.assertEqual(payload_arg["items"][0]["press_id"], "015")
        self.assertIn("fetched_at", payload_arg)
        self.assertEqual(outcome.status, "success")
        self.assertFalse(outcome.previous_cache_preserved)

    def test_cache_write_failure_reported_as_failure(self):
        item = PressTopItem(
            press_id="015", press_name="테스트", press_logo=None, rank=1,
            article_id="123", title="제목", article_url="https://n.news.naver.com/article/015/123",
            thumbnail=None, time_label="1시간전",
        )
        parse_ok = ParseResult(ok=True, items=[item], box_count=40, valid_item_count=1)
        with patch.object(C, "fetch_popular_day", return_value=FetchResult(ok=True, status_code=200, html="<html></html>")), \
             patch.object(C, "parse_popular_day_html", return_value=parse_ok), \
             patch.object(C, "upsert_generic_snapshot", return_value=False):
            outcome = C.run_naver_press_popular_collection()
        self.assertEqual(outcome.status, "skipped_cache_write_failed")
        self.assertTrue(outcome.previous_cache_preserved)


if __name__ == "__main__":
    unittest.main()
