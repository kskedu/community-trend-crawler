"""네이버 언론사별 인기뉴스 fetcher 단위 테스트 (unittest, 실제 네트워크 호출 없음).

검증 항목(구현 계획 §9):
15. HTTP 403 → 즉시 중단(재시도 없음)
16. HTTP 429 → 중단
17. timeout → 중단
- 5xx → 1회 제한적 retry 후 성공/실패
- CAPTCHA/challenge 의심(짧은 응답) → 중단
- content-type 비 HTML → 중단
- 정상 200 + HTML → ok=True
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from news_naver_press import fetcher as F


def _resp(status_code, text="", headers=None):
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    r.apparent_encoding = "utf-8"
    r.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
    return r


class TestFetcher(unittest.TestCase):
    def test_http_403_no_retry(self):
        with patch.object(F.requests, "get", return_value=_resp(403)) as m:
            result = F.fetch_popular_day()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "BLOCKED")
        self.assertEqual(result.status_code, 403)
        self.assertEqual(m.call_count, 1)  # 재시도 없음

    def test_http_429_no_retry(self):
        with patch.object(F.requests, "get", return_value=_resp(429)) as m:
            result = F.fetch_popular_day()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "RATE_LIMITED")
        self.assertEqual(m.call_count, 1)

    def test_timeout(self):
        with patch.object(F.requests, "get", side_effect=requests.exceptions.Timeout()):
            result = F.fetch_popular_day()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "TIMEOUT")

    def test_network_error(self):
        with patch.object(F.requests, "get", side_effect=requests.exceptions.ConnectionError()):
            result = F.fetch_popular_day()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "NETWORK_ERROR")

    def test_5xx_retries_once_then_succeeds(self):
        big_html = "<html>" + ("x" * 6000) + "</html>"
        responses = [_resp(503), _resp(200, text=big_html)]
        with patch.object(F.requests, "get", side_effect=responses) as m, \
             patch.object(F.time, "sleep"):
            result = F.fetch_popular_day()
        self.assertTrue(result.ok)
        self.assertEqual(m.call_count, 2)

    def test_5xx_retry_exhausted_fails(self):
        with patch.object(F.requests, "get", return_value=_resp(503)) as m, \
             patch.object(F.time, "sleep"):
            result = F.fetch_popular_day()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "HTTP_ERROR")
        self.assertEqual(m.call_count, 2)  # 최초 1회 + retry 1회 = 2회, 그 이상 없음

    def test_bad_content_type(self):
        with patch.object(F.requests, "get", return_value=_resp(200, headers={"Content-Type": "application/json"})):
            result = F.fetch_popular_day()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "BAD_CONTENT_TYPE")

    def test_challenge_suspected_short_response(self):
        with patch.object(F.requests, "get", return_value=_resp(200, text="<html>too short</html>")):
            result = F.fetch_popular_day()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "CHALLENGE_SUSPECTED")

    def test_challenge_suspected_captcha_marker(self):
        html = "<html>" + ("captcha check " * 500) + "</html>"
        with patch.object(F.requests, "get", return_value=_resp(200, text=html)):
            result = F.fetch_popular_day()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "CHALLENGE_SUSPECTED")

    def test_success(self):
        big_html = "<html>" + ("<div class='rankingnews_box'></div>" * 200) + "</html>"
        with patch.object(F.requests, "get", return_value=_resp(200, text=big_html)):
            result = F.fetch_popular_day()
        self.assertTrue(result.ok)
        self.assertEqual(result.status_code, 200)
        self.assertIsNotNone(result.html)

    def test_no_proxy_or_ua_rotation(self):
        # 요청이 항상 동일 고정 UA 1종만 사용하는지(로테이션 금지 계약) 확인.
        big_html = "<html>" + ("x" * 6000) + "</html>"
        with patch.object(F.requests, "get", return_value=_resp(200, text=big_html)) as m:
            F.fetch_popular_day()
            F.fetch_popular_day()
        calls = m.call_args_list
        uas = [c.kwargs["headers"]["User-Agent"] for c in calls]
        self.assertEqual(len(set(uas)), 1)
        self.assertEqual(uas[0], F.USER_AGENT)


if __name__ == "__main__":
    unittest.main()
