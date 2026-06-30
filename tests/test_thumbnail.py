"""thumbnail 수집/캐시 단위 테스트 (unittest, 실제 네트워크 호출 없음).

검증 항목:
- og:image 추출
- twitter:image fallback (og 없을 때)
- 상대경로 → 절대경로 변환
- data URI / base64 거부
- http thumbnail 거부 (https 만 허용)
- 너무 긴 URL 거부
- timeout/요청 실패 시 None
- 이전 news_top cache 재사용 (신규 GET 없이)
- 같은 run 내 memoization (중복 GET 방지)
- 기존 thumbnail 없음 backward compatibility
- 기사 본문 저장 없음(메타 URL 만 다룸) 확인
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news import thumbnail as T


class TestExtract(unittest.TestCase):
    def test_og_image(self):
        html = '<head><meta property="og:image" content="https://cdn.example.com/a.jpg"></head>'
        self.assertEqual(T.extract_thumbnail(html, "https://news.com/1"), "https://cdn.example.com/a.jpg")

    def test_twitter_fallback_when_no_og(self):
        html = '<meta name="twitter:image" content="https://cdn.example.com/tw.png">'
        self.assertEqual(T.extract_thumbnail(html, "https://news.com/1"), "https://cdn.example.com/tw.png")

    def test_og_preferred_over_twitter(self):
        html = ('<meta name="twitter:image" content="https://cdn.example.com/tw.png">'
                '<meta property="og:image" content="https://cdn.example.com/og.jpg">')
        self.assertEqual(T.extract_thumbnail(html, "https://news.com/1"), "https://cdn.example.com/og.jpg")

    def test_relative_to_absolute(self):
        html = '<meta property="og:image" content="/img/thumb.jpg">'
        self.assertEqual(
            T.extract_thumbnail(html, "https://news.com/article/1"),
            "https://news.com/img/thumb.jpg",
        )

    def test_protocol_relative_to_https(self):
        # //host/path → base scheme(https) 적용
        html = '<meta property="og:image" content="//cdn.example.com/p.jpg">'
        self.assertEqual(
            T.extract_thumbnail(html, "https://news.com/1"),
            "https://cdn.example.com/p.jpg",
        )

    def test_reject_data_uri(self):
        html = '<meta property="og:image" content="data:image/png;base64,AAAA">'
        self.assertIsNone(T.extract_thumbnail(html, "https://news.com/1"))

    def test_reject_http(self):
        # http 썸네일은 mixed content → 거부
        html = '<meta property="og:image" content="http://cdn.example.com/a.jpg">'
        self.assertIsNone(T.extract_thumbnail(html, "https://news.com/1"))

    def test_reject_too_long_url(self):
        long_url = "https://cdn.example.com/" + ("a" * (T.MAX_URL_LEN + 10)) + ".jpg"
        html = f'<meta property="og:image" content="{long_url}">'
        self.assertIsNone(T.extract_thumbnail(html, "https://news.com/1"))

    def test_no_meta(self):
        self.assertIsNone(T.extract_thumbnail("<html><body>no meta</body></html>", "https://news.com/1"))

    def test_empty_html(self):
        self.assertIsNone(T.extract_thumbnail("", "https://news.com/1"))


class TestAcceptable(unittest.TestCase):
    def test_https_ok(self):
        self.assertTrue(T.is_acceptable_thumbnail("https://cdn.example.com/a.jpg"))

    def test_http_reject(self):
        self.assertFalse(T.is_acceptable_thumbnail("http://cdn.example.com/a.jpg"))

    def test_data_reject(self):
        self.assertFalse(T.is_acceptable_thumbnail("data:image/png;base64,AAAA"))

    def test_base64_substr_reject(self):
        self.assertFalse(T.is_acceptable_thumbnail("https://x.com/;base64,AAA"))

    def test_none_reject(self):
        self.assertFalse(T.is_acceptable_thumbnail(None))


class TestCache(unittest.TestCase):
    def _prev(self):
        return {"keywords": [
            {"keyword": "A", "articles": [
                {"url": "https://news.com/1", "thumbnail": "https://cdn.com/1.jpg"},
                {"url": "https://news.com/2", "thumbnail": None},
                {"url": "https://news.com/3", "thumbnail": "http://bad.com/x.jpg"},  # http → 캐시 제외
            ]},
        ]}

    def test_build_cache(self):
        cache = T.build_thumbnail_cache(self._prev())
        self.assertEqual(cache, {"https://news.com/1": "https://cdn.com/1.jpg"})

    def test_build_cache_none_prev(self):
        self.assertEqual(T.build_thumbnail_cache(None), {})

    def test_build_cache_bad_shape(self):
        self.assertEqual(T.build_thumbnail_cache({"keywords": "nope"}), {})

    def test_trailing_slash_match(self):
        cache = T.build_thumbnail_cache({"keywords": [
            {"articles": [{"url": "https://news.com/1/", "thumbnail": "https://cdn.com/1.jpg"}]},
        ]})
        self.assertIn("https://news.com/1", cache)  # trailing slash 정규화


class TestEnrich(unittest.TestCase):
    def setUp(self):
        self._orig_fetch = T.fetch_thumbnail
        self.fetch_calls = []

    def tearDown(self):
        T.fetch_thumbnail = self._orig_fetch

    def _mock_fetch(self, mapping):
        def f(url, timeout=T.FETCH_TIMEOUT):
            self.fetch_calls.append(url)
            return mapping.get(url)
        return f

    def test_reuse_cache_no_fetch(self):
        # 이전 캐시에 있는 URL 은 신규 GET 하지 않는다.
        T.fetch_thumbnail = self._mock_fetch({})  # fetch 되면 None
        current = {"keywords": [{"articles": [{"url": "https://news.com/1", "thumbnail": None}]}]}
        prev = {"keywords": [{"articles": [{"url": "https://news.com/1", "thumbnail": "https://cdn.com/1.jpg"}]}]}
        out = T.enrich_issue_thumbnails(current, prev)
        self.assertEqual(out["keywords"][0]["articles"][0]["thumbnail"], "https://cdn.com/1.jpg")
        self.assertEqual(self.fetch_calls, [])  # GET 0회

    def test_new_fetch_when_no_cache(self):
        T.fetch_thumbnail = self._mock_fetch({"https://news.com/9": "https://cdn.com/9.jpg"})
        current = {"keywords": [{"articles": [{"url": "https://news.com/9", "thumbnail": None}]}]}
        out = T.enrich_issue_thumbnails(current, None)
        self.assertEqual(out["keywords"][0]["articles"][0]["thumbnail"], "https://cdn.com/9.jpg")
        self.assertEqual(self.fetch_calls, ["https://news.com/9"])

    def test_run_memoization(self):
        # 같은 URL 이 여러 키워드에 등장해도 GET 은 1회.
        T.fetch_thumbnail = self._mock_fetch({"https://news.com/dup": "https://cdn.com/d.jpg"})
        current = {"keywords": [
            {"articles": [{"url": "https://news.com/dup", "thumbnail": None}]},
            {"articles": [{"url": "https://news.com/dup", "thumbnail": None}]},
        ]}
        out = T.enrich_issue_thumbnails(current, None)
        self.assertEqual(self.fetch_calls, ["https://news.com/dup"])  # 1회만
        self.assertEqual(out["keywords"][0]["articles"][0]["thumbnail"], "https://cdn.com/d.jpg")
        self.assertEqual(out["keywords"][1]["articles"][0]["thumbnail"], "https://cdn.com/d.jpg")

    def test_fetch_fail_keeps_none(self):
        T.fetch_thumbnail = self._mock_fetch({})  # 항상 None
        current = {"keywords": [{"articles": [{"url": "https://news.com/x", "thumbnail": None}]}]}
        out = T.enrich_issue_thumbnails(current, None)
        self.assertIsNone(out["keywords"][0]["articles"][0]["thumbnail"])

    def test_fetch_returns_http_rejected(self):
        # fetch 가 http 를 돌려줘도 저장 거부 → None 유지
        T.fetch_thumbnail = self._mock_fetch({"https://news.com/x": "http://bad.com/a.jpg"})
        current = {"keywords": [{"articles": [{"url": "https://news.com/x", "thumbnail": None}]}]}
        out = T.enrich_issue_thumbnails(current, None)
        self.assertIsNone(out["keywords"][0]["articles"][0]["thumbnail"])

    def test_existing_valid_thumbnail_untouched(self):
        # 이미 유효 thumbnail 이 있으면 재수집 안 함
        T.fetch_thumbnail = self._mock_fetch({"https://news.com/1": "https://cdn.com/new.jpg"})
        current = {"keywords": [{"articles": [{"url": "https://news.com/1", "thumbnail": "https://cdn.com/keep.jpg"}]}]}
        out = T.enrich_issue_thumbnails(current, None)
        self.assertEqual(out["keywords"][0]["articles"][0]["thumbnail"], "https://cdn.com/keep.jpg")
        self.assertEqual(self.fetch_calls, [])

    def test_backward_compat_no_thumbnail_field(self):
        # thumbnail 키 자체가 없는 기존 article 도 안전(없으면 fetch 시도, 실패 시 키 미생성 가능)
        T.fetch_thumbnail = self._mock_fetch({})
        current = {"keywords": [{"articles": [{"url": "https://news.com/legacy", "title": "t"}]}]}
        out = T.enrich_issue_thumbnails(current, None)
        # 실패 시 thumbnail 키를 강제로 만들지 않음 → 기존 구조 보존
        art = out["keywords"][0]["articles"][0]
        self.assertNotIn("thumbnail", art)
        self.assertEqual(art["title"], "t")

    def test_no_body_storage(self):
        # enrich 는 thumbnail URL 만 추가 — 본문/기타 필드 추가 안 함
        T.fetch_thumbnail = self._mock_fetch({"https://news.com/1": "https://cdn.com/1.jpg"})
        current = {"keywords": [{"articles": [{"url": "https://news.com/1", "title": "t", "snippet": "s", "thumbnail": None}]}]}
        out = T.enrich_issue_thumbnails(current, None)
        art = out["keywords"][0]["articles"][0]
        self.assertEqual(set(art.keys()), {"url", "title", "snippet", "thumbnail"})  # 본문 등 신규 키 없음


class TestSSRF(unittest.TestCase):
    def test_localhost_blocked(self):
        self.assertFalse(T._is_public_host("localhost"))

    def test_loopback_ip_blocked(self):
        self.assertFalse(T._is_public_host("127.0.0.1"))

    def test_private_ip_blocked(self):
        self.assertFalse(T._is_public_host("10.0.0.5"))
        self.assertFalse(T._is_public_host("192.168.1.1"))
        self.assertFalse(T._is_public_host("172.16.0.1"))

    def test_link_local_metadata_blocked(self):
        self.assertFalse(T._is_public_host("169.254.169.254"))  # cloud metadata

    def test_public_ip_ok(self):
        self.assertTrue(T._is_public_host("8.8.8.8"))

    def test_empty_blocked(self):
        self.assertFalse(T._is_public_host(""))

    def test_fetch_rejects_private_host(self):
        # 비공개 host 면 requests.get 자체를 호출하지 않는다.
        import news.thumbnail as mod
        called = []
        orig = mod.requests.get
        try:
            mod.requests.get = lambda *a, **k: called.append(1)
            self.assertIsNone(mod.fetch_thumbnail("https://127.0.0.1/a", timeout=1))
            self.assertIsNone(mod.fetch_thumbnail("http://169.254.169.254/latest/meta-data/", timeout=1))
            self.assertEqual(called, [])  # GET 0회
        finally:
            mod.requests.get = orig


class TestExtractMultiOg(unittest.TestCase):
    def test_skip_rejected_og_use_next_valid(self):
        # 첫 og 가 http(거부), 둘째 og 가 https(채택)
        html = ('<meta property="og:image" content="http://bad.com/a.jpg">'
                '<meta property="og:image" content="https://cdn.example.com/good.jpg">')
        self.assertEqual(T.extract_thumbnail(html, "https://news.com/1"), "https://cdn.example.com/good.jpg")


class TestRedirect(unittest.TestCase):
    def test_redirect_not_followed(self):
        # 공개 host 의 302 응답은 추적하지 않고 None (SSRF 리다이렉트 우회 방지).
        import news.thumbnail as mod

        class FakeResp:
            status_code = 302
            is_redirect = True
            is_permanent_redirect = False
            headers = {"Location": "http://169.254.169.254/latest/meta-data/"}
            encoding = "utf-8"
            def raise_for_status(self): pass
            def iter_content(self, chunk_size=8192): return iter([])
            def close(self): pass

        orig = mod.requests.get
        try:
            mod.requests.get = lambda *a, **k: FakeResp()
            # 8.8.8.8 은 공개 IP → SSRF 1차 통과하지만 302 라 추적 안 함
            self.assertIsNone(mod.fetch_thumbnail("https://8.8.8.8/article", timeout=1))
        finally:
            mod.requests.get = orig


class TestFetchFailure(unittest.TestCase):
    def test_fetch_exception_returns_none(self):
        # requests.get 이 예외 던져도 None (네트워크 실제 호출 대신 monkeypatch)
        import news.thumbnail as mod
        orig = mod.requests.get
        try:
            def boom(*a, **k):
                raise RuntimeError("network down")
            mod.requests.get = boom
            # 공개 IP 리터럴(8.8.8.8) — SSRF 가드 통과 후 requests.get(boom) 경로 검증(DNS 비의존)
            self.assertIsNone(mod.fetch_thumbnail("https://8.8.8.8/1", timeout=1))
        finally:
            mod.requests.get = orig


if __name__ == "__main__":
    unittest.main()
