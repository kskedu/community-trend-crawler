"""통합 랭킹 단위 테스트 (unittest, 외부호출/DB write 없음).

검증 항목:
- ranker: score 결정성, News 지배, 재정규화, penalty, 동점 tiebreak, 0-division
- candidates: dedup/병합/상한, 다양성 카운트, News 신호 산출
- datalab: recent_delta 계산, 0-division 방어, fixture skip
- google: stub skip
- Daum 복제 방지(순서 탈동조)
- JSON backward compatibility(기존 프론트 필드 보존)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news import ranker, candidates as cand, datalab, google
from news.builder import build_ranked_issues, build_ranked_entry


def _news(recent_count, age, diversity, relevance, articles=None):
    return {
        "recent_count": recent_count,
        "latest_age_hours": age,
        "domain_diversity": diversity,
        "title_relevance": relevance,
        "articles": articles or [{"title": "t", "url": "https://x.com/a", "snippet": "s",
                                  "press": "x", "published_at": None, "thumbnail": None}],
    }


class TestRanker(unittest.TestCase):
    def _candidates(self, kws):
        return [{"keyword": k, "sources": {"daum": i + 1}} for i, k in enumerate(kws)]

    def test_news_dominates_and_reorders(self):
        # daum 순서 [A,B] 이지만 B의 News 신호가 압도 → B가 1위
        cands = self._candidates(["A", "B"])
        signals = {
            "news": {
                "A": _news(1, 10, 1, 1.0),
                "B": _news(8, 1, 5, 1.0),
            },
            "datalab": {}, "google": {}, "daum": {"A": 1, "B": 2},
        }
        ranked = ranker.compute_scores(cands, signals)
        self.assertEqual(ranked[0]["keyword"], "B")
        self.assertGreater(ranked[0]["score"], ranked[1]["score"])

    def test_deterministic(self):
        cands = self._candidates(["A", "B"])
        signals = {"news": {"A": _news(3, 2, 2, 1.0), "B": _news(1, 5, 1, 1.0)},
                   "datalab": {}, "google": {}, "daum": {"A": 1, "B": 2}}
        r1 = ranker.compute_scores(cands, signals)
        r2 = ranker.compute_scores(cands, signals)
        self.assertEqual([x["keyword"] for x in r1], [x["keyword"] for x in r2])
        self.assertEqual(r1[0]["score"], r2[0]["score"])

    def test_renormalize_news_only(self):
        # datalab/google/daum 없음 → news weight 1.0로 재정규화
        cands = [{"keyword": "A", "sources": {}}, {"keyword": "B", "sources": {}}]
        signals = {"news": {"A": _news(5, 1, 3, 1.0), "B": _news(1, 10, 1, 1.0)},
                   "datalab": {}, "google": {}, "daum": {}}
        ranked = ranker.compute_scores(cands, signals)
        self.assertEqual(ranked[0]["used_signals"], ["news"])
        # news weight 1.0 → A의 score ≈ news_norm(A)
        self.assertGreater(ranked[0]["score"], 0)

    def test_all_signals_empty_returns_empty(self):
        cands = [{"keyword": "A", "sources": {}}]
        ranked = ranker.compute_scores(cands, {"news": {}, "datalab": {}, "google": {}, "daum": {}})
        self.assertEqual(ranked, [])

    def test_noise_penalty(self):
        cands = [{"keyword": "1", "sources": {"daum": 1}}, {"keyword": "정상키워드", "sources": {"daum": 2}}]
        signals = {"news": {"1": _news(3, 1, 2, 1.0), "정상키워드": _news(3, 1, 2, 1.0)},
                   "datalab": {}, "google": {}, "daum": {"1": 1, "정상키워드": 2}}
        ranked = ranker.compute_scores(cands, signals)
        scores = {r["keyword"]: r["score"] for r in ranked}
        self.assertLess(scores["1"], scores["정상키워드"])  # 숫자 키워드 penalty

    def test_low_relevance_penalty(self):
        cands = [{"keyword": "A", "sources": {}}, {"keyword": "B", "sources": {}}]
        signals = {"news": {"A": _news(3, 1, 2, 1.0), "B": _news(3, 1, 2, 0.0)},
                   "datalab": {}, "google": {}, "daum": {}}
        ranked = ranker.compute_scores(cands, signals)
        scores = {r["keyword"]: r["score"] for r in ranked}
        self.assertLess(scores["B"], scores["A"])

    def test_news_required_excludes_newsless_candidate(self):
        # news 소스는 가용하나 특정 후보엔 news 없음 → 그 후보는 datalab/daum 점수가 있어도 제외
        cands = [{"keyword": "A", "sources": {"daum": 1}}, {"keyword": "B", "sources": {"daum": 2}}]
        signals = {
            "news": {"A": _news(3, 1, 2, 1.0)},          # B는 news 없음
            "datalab": {"B": {"recent_delta": 2.0}},     # B에 datalab만
            "google": {}, "daum": {"A": 1, "B": 2},
        }
        ranked = ranker.compute_scores(cands, signals)
        kws = [r["keyword"] for r in ranked]
        self.assertIn("A", kws)
        self.assertNotIn("B", kws)  # news 없는 B 제외

    def test_rank_reason_only_used_signals(self):
        cands = [{"keyword": "A", "sources": {"daum": 1}}]
        signals = {"news": {"A": _news(3, 1, 2, 1.0)}, "datalab": {}, "google": {}, "daum": {"A": 1}}
        ranked = ranker.compute_scores(cands, signals)
        # datalab/google 미사용 → reason에 언급 없어야
        self.assertNotIn("구글", ranked[0]["rank_reason"])
        self.assertNotIn("검색 관심", ranked[0]["rank_reason"])


class TestDataLab(unittest.TestCase):
    def test_recent_delta_basic(self):
        pts = [{"ratio": 10}, {"ratio": 10}, {"ratio": 20}, {"ratio": 20}]
        d = datalab._compute_delta(pts)
        self.assertAlmostEqual(d, 1.0)  # 10→20 = +100%

    def test_recent_delta_prev_zero_returns_none(self):
        pts = [{"ratio": 0}, {"ratio": 0}, {"ratio": 5}, {"ratio": 5}]
        self.assertIsNone(datalab._compute_delta(pts))  # 직전 0, 최근 양수 → 신호부재

    def test_recent_delta_both_zero(self):
        pts = [{"ratio": 0}, {"ratio": 0}, {"ratio": 0}, {"ratio": 0}]
        self.assertEqual(datalab._compute_delta(pts), 0.0)

    def test_recent_delta_clamp(self):
        pts = [{"ratio": 1}, {"ratio": 1}, {"ratio": 100}, {"ratio": 100}]
        self.assertLessEqual(datalab._compute_delta(pts), datalab.DELTA_CLAMP)

    def test_too_few_points(self):
        self.assertIsNone(datalab._compute_delta([{"ratio": 5}]))

    def test_no_key_returns_empty(self):
        old_id = os.environ.pop("NAVER_CLIENT_ID", None)
        old_sec = os.environ.pop("NAVER_CLIENT_SECRET", None)
        try:
            self.assertEqual(datalab.fetch(["A", "B"]), {})
        finally:
            if old_id:
                os.environ["NAVER_CLIENT_ID"] = old_id
            if old_sec:
                os.environ["NAVER_CLIENT_SECRET"] = old_sec

    def test_fixture_filter(self):
        fx = {"환율 급등": {"recent_delta": 0.5}, "기타": {"recent_delta": 0.1}}
        out = datalab.fetch_from_fixture(fx, ["환율 급등"])
        self.assertIn("환율 급등", out)
        self.assertNotIn("기타", out)

    def _with_keys(self):
        os.environ["NAVER_CLIENT_ID"] = "x"
        os.environ["NAVER_CLIENT_SECRET"] = "y"

    def test_batch_exception_returns_empty(self):
        self._with_keys()
        orig = datalab.requests.post
        datalab.requests.post = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            self.assertEqual(datalab.fetch(["A", "B"]), {})  # 부분실패 → 전체 skip
        finally:
            datalab.requests.post = orig

    def test_malformed_results_returns_empty(self):
        self._with_keys()

        class _Resp:
            def raise_for_status(self): pass
            def json(self): return {"results": "not-a-list"}

        orig = datalab.requests.post
        datalab.requests.post = lambda *a, **k: _Resp()
        try:
            self.assertEqual(datalab.fetch(["A"]), {})  # 응답 이상 → 전체 skip
        finally:
            datalab.requests.post = orig

    def test_results_count_mismatch_returns_empty(self):
        self._with_keys()

        class _Resp:
            def raise_for_status(self): pass
            def json(self): return {"results": []}  # 요청 1개인데 0개

        orig = datalab.requests.post
        datalab.requests.post = lambda *a, **k: _Resp()
        try:
            self.assertEqual(datalab.fetch(["A"]), {})
        finally:
            datalab.requests.post = orig


class TestGoogleStub(unittest.TestCase):
    def test_candidates_skip(self):
        os.environ.pop("GOOGLE_TRENDS_ENABLED", None)
        self.assertEqual(google.fetch_candidates(), [])

    def test_signals_skip(self):
        os.environ.pop("GOOGLE_TRENDS_ENABLED", None)
        self.assertEqual(google.fetch_signals(["A"]), {})


class TestCandidates(unittest.TestCase):
    def test_merge_dedup(self):
        c = cand.collect_candidates(
            [{"keyword": "A", "rank": 1}, {"keyword": "B", "rank": 2}],
            [{"keyword": "a", "rank": 1}],  # 대소문자 dedup
            [], [],
        )
        keys = [x["keyword"] for x in c]
        self.assertEqual(len(c), 2)  # A/a 병합
        self.assertIn("daum", next(x for x in c if x["keyword"] == "A")["sources"])

    def test_limit(self):
        many = [{"keyword": f"k{i}", "rank": i} for i in range(50)]
        c = cand.collect_candidates(many, [], [], [], limit=30)
        self.assertEqual(len(c), 30)

    def test_count_non_daum_excludes_aux(self):
        # 독립 소스(danawa/google)만 카운트. daum/aux 종속은 제외.
        c = [
            {"keyword": "A", "sources": {"daum": 1}},               # daum 단독 → 제외
            {"keyword": "B", "sources": {"daum": 2, "danawa": 1}},  # danawa → 카운트
            {"keyword": "C", "sources": {"aux": True}},             # aux 단독(Daum 파생) → 제외
            {"keyword": "D", "sources": {"google": 1}},             # google → 카운트
            {"keyword": "E", "sources": {"daum": 3, "aux": True}},  # daum+aux → 제외
        ]
        self.assertEqual(cand.count_non_daum(c), 2)  # B, D만

    def test_compute_news_signal_drops_invalid(self):
        raw = [
            {"title": "유효", "originallink": "https://yna.co.kr/a", "description": "환율 뉴스",
             "pubDate": "Tue, 16 Jun 2026 21:00:00 +0900"},
            {"title": "악성", "originallink": "javascript:x", "link": "ftp://x", "description": "x"},
        ]
        sig = cand.compute_news_signal("환율", raw)
        self.assertEqual(len(sig["articles"]), 1)  # 악성 드롭

    def test_compute_news_signal_none_when_empty(self):
        self.assertIsNone(cand.compute_news_signal("X", []))


class TestBuilderBackwardCompat(unittest.TestCase):
    def test_entry_has_legacy_and_new_fields(self):
        ranked_item = {
            "keyword": "환율", "score": 0.9,
            "source_breakdown": {"news": 0.9, "datalab": 0.5, "google": 0.0, "daum": 0.3},
            "rank_reason": "최근 뉴스 다수",
            "news_meta": _news(3, 1, 2, 1.0),
            "used_signals": ["news", "datalab", "daum"],
        }
        entry = build_ranked_entry(1, ranked_item, {"keyword": "환율", "sources": {"daum": 1}})
        # 기존 프론트가 읽는 필드(legacy)
        for f in ("rank", "keyword", "summary", "signals", "articles"):
            self.assertIn(f, entry)
        self.assertIn("news", entry["signals"])  # 기존
        self.assertIn("trend", entry["signals"])  # 기존 호환
        # 신규 optional
        for f in ("score", "rank_reason", "source_breakdown"):
            self.assertIn(f, entry)
        for s in ("datalab", "google", "daum"):
            self.assertIn(s, entry["signals"])

    def test_issues_root_optional_fields(self):
        ranked_item = {"keyword": "A", "score": 0.5, "source_breakdown": {"news": 0.5},
                       "rank_reason": "최근 뉴스 다수", "news_meta": _news(1, 1, 1, 1.0),
                       "used_signals": ["news"]}
        issues = build_ranked_issues([ranked_item], {}, ["naver_news"])
        self.assertIn("keywords", issues)
        self.assertIn("data_sources", issues)
        self.assertIn("generated_at", issues)


class TestDaumDecoupling(unittest.TestCase):
    def test_order_differs_from_daum(self):
        # daum 순서 [A,B,C], News 신호로 C>B>A 가 되도록
        cands = [{"keyword": k, "sources": {"daum": i + 1}} for i, k in enumerate(["A", "B", "C"])]
        signals = {
            "news": {"A": _news(1, 10, 1, 1.0), "B": _news(4, 5, 3, 1.0), "C": _news(9, 1, 6, 1.0)},
            "datalab": {}, "google": {}, "daum": {"A": 1, "B": 2, "C": 3},
        }
        ranked = ranker.compute_scores(cands, signals)
        order = [r["keyword"] for r in ranked]
        self.assertNotEqual(order, ["A", "B", "C"])  # daum 순서와 다름
        self.assertEqual(order[0], "C")  # News 최강이 1위


if __name__ == "__main__":
    unittest.main(verbosity=2)
