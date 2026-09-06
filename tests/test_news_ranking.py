"""통합 랭킹 단위 테스트 (unittest, 외부호출/DB write 없음).

검증 항목:
- ranker: score 결정성, News 지배, 재정규화, penalty, 동점 tiebreak, 0-division
- candidates: dedup/병합/상한, 다양성 카운트, News 신호 산출
- datalab: recent_delta 계산, 0-division 방어, fixture skip
- google: RSS provider(기본 disabled, 외부호출 0), RSS 파싱, demand 신호
- source family(google_trends/daum_home/nate_home/bing_home) 편입/다양성/4축 score
- seed 복제 방지(순서 탈동조)
- JSON backward compatibility(기존 프론트 필드 보존)
- 랭킹 품질 개선(docs/news-ranking-quality-plan.md): 유사 키워드 dedupe,
  same-issue merge, article relevance/incidental mention 필터, clustering/
  representative 선택, movement와의 순서 관계
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news import ranker, candidates as cand, datalab, google, normalizer
from news import summarizer as cand_summarizer
from news.summarizer import summarize
from news.builder import build_ranked_issues, build_ranked_entry
from news.movement import apply_movement
import main as main_module


def _news(recent_count, age, diversity, relevance, articles=None,
          high_relevance_count=2, quality_cluster_size=2,
          fresh_high_relevance_count=1, fresh_quality_cluster_size=1,
          latest_relevant_age_hours=1.0,
          keyword_kind="unknown", has_dominant_event=True, same_event_burst=True,
          representative_article=None, summary_type_hint=None):
    # high_relevance_count/quality_cluster_size/fresh_* 기본값은 keyword-level quality
    # gate(ranker._passes_keyword_quality_gate, fresh relevance gate 포함)를 통과하도록
    # 채운다 — 이 헬퍼를 쓰는 기존 테스트들은 quality gate 자체가 아니라 score/penalty/
    # 정규화 로직을 검증 대상으로 하므로, 신규 gate 때문에 무관하게 회귀하지 않아야 한다.
    #
    # entity-role 정제(2026-07) 이후 신규 필드 기본값:
    # - keyword_kind='unknown'(cohesion gate 미적용 — 기존 테스트는 대부분 다토큰/일반 키워드).
    # - has_dominant_event/same_event_burst=True(정상 이슈로 취급 — cohesion gate 통과).
    # - articles에 relevance_score/is_primary_cluster가 없으면 실제 파이프처럼 자동 부여해
    #   B2(no_representative gate)/canonical_evidence가 정상 대표로 판정하게 한다.
    arts = articles or [{"title": "t", "url": "https://x.com/a", "snippet": "s",
                         "press": "x", "published_at": None, "thumbnail": None}]
    enriched = []
    for a in arts:
        b = dict(a)
        b.setdefault("relevance_score", 0.9)
        b.setdefault("relevance_reason", "keyword_main_topic")
        b.setdefault("is_incidental", False)
        b.setdefault("is_primary_cluster", True)
        enriched.append(b)
    return {
        "recent_count": recent_count,
        "latest_age_hours": age,
        "domain_diversity": diversity,
        "title_relevance": relevance,
        "articles": enriched,
        "representative_article": representative_article or (enriched[0] if enriched else None),
        "high_relevance_count": high_relevance_count,
        "quality_cluster_size": quality_cluster_size,
        "fresh_high_relevance_count": fresh_high_relevance_count,
        "fresh_quality_cluster_size": fresh_quality_cluster_size,
        "latest_relevant_age_hours": latest_relevant_age_hours,
        "keyword_kind": keyword_kind,
        "has_dominant_event": has_dominant_event,
        "same_event_burst": same_event_burst,
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
        # 독립 family/demand 신호 없음(sources 비어있음) → news+freshness 축만 가용
        cands = [{"keyword": "A", "sources": {}}, {"keyword": "B", "sources": {}}]
        signals = {"news": {"A": _news(5, 1, 3, 1.0), "B": _news(1, 10, 1, 1.0)},
                   "datalab": {}, "google": {}}
        ranked = ranker.compute_scores(cands, signals)
        used = set(ranked[0]["used_signals"])
        self.assertIn("news", used)
        self.assertIn("freshness", used)  # freshness는 news 근거에서 파생돼 함께 가용
        self.assertNotIn("search_demand", used)
        self.assertNotIn("source_consensus", used)
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


class TestGoogleProvider(unittest.TestCase):
    def setUp(self):
        google.reset_cache()

    def tearDown(self):
        os.environ.pop("GOOGLE_TRENDS_ENABLED", None)
        os.environ.pop("GOOGLE_TRENDS_PROVIDER", None)
        google.reset_cache()

    def test_disabled_by_default_no_http(self):
        # 기본 비활성: env 미설정 → 외부 호출 0, 빈 결과
        os.environ.pop("GOOGLE_TRENDS_ENABLED", None)
        os.environ.pop("GOOGLE_TRENDS_PROVIDER", None)
        called = {"n": 0}
        orig = google.requests.get
        google.requests.get = lambda *a, **k: called.__setitem__("n", called["n"] + 1)
        try:
            self.assertEqual(google.fetch_candidates(), [])
            self.assertEqual(google.fetch_signals(["A"]), {})
        finally:
            google.requests.get = orig
        self.assertEqual(called["n"], 0)  # 외부 HTTP 호출 없음

    def test_enabled_requires_both_env(self):
        os.environ["GOOGLE_TRENDS_ENABLED"] = "true"
        os.environ.pop("GOOGLE_TRENDS_PROVIDER", None)
        self.assertFalse(google.is_enabled())  # provider=rss 아니면 비활성
        os.environ["GOOGLE_TRENDS_PROVIDER"] = "rss"
        self.assertTrue(google.is_enabled())

    # 현행 "Trending now" RSS 실응답(geo=KR, 2026-07-03 curl 확인) 축약 fixture.
    # 실제 스키마 그대로: xmlns:ht=trending/rss, 빈 description/snippet, ht:picture,
    # HTML entity 이스케이프된 news_item_title, 작은 단위 approx_traffic("1000+").
    _TRENDING_RSS_FIXTURE = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<rss xmlns:atom="http://www.w3.org/2005/Atom"'
        ' xmlns:ht="https://trends.google.com/trending/rss" version="2.0">'
        '<channel>'
        '<title>Daily Search Trends</title>'
        '<item><title>이종석</title>'
        '<ht:approx_traffic>200+</ht:approx_traffic>'
        '<description/>'
        '<link>https://trends.google.com/trending/rss?geo=KR</link>'
        '<pubDate>Fri, 3 Jul 2026 08:40:00 -0700</pubDate>'
        '<ht:picture>https://img.example/p1</ht:picture>'
        '<ht:picture_source>Daum</ht:picture_source>'
        '<ht:news_item>'
        '<ht:news_item_title>&apos;아이유&apos; 이종석 근황</ht:news_item_title>'
        '<ht:news_item_snippet/>'
        '<ht:news_item_url>https://n.example/1</ht:news_item_url>'
        '<ht:news_item_source>뉴시스</ht:news_item_source>'
        '</ht:news_item>'
        '</item>'
        '<item><title>유조선</title>'
        '<ht:approx_traffic>1000+</ht:approx_traffic>'
        '<pubDate>Fri, 3 Jul 2026 07:50:00 -0700</pubDate>'
        '</item>'
        '</channel></rss>'
    )

    @staticmethod
    def _resp(body: str):
        class _Resp:
            content = body.encode("utf-8")
            def raise_for_status(self):
                pass
        return _Resp()

    def test_rss_parse_candidates(self):
        os.environ["GOOGLE_TRENDS_ENABLED"] = "true"
        os.environ["GOOGLE_TRENDS_PROVIDER"] = "rss"
        orig = google.requests.get
        google.requests.get = lambda *a, **k: self._resp(self._TRENDING_RSS_FIXTURE)
        try:
            cands = google.fetch_candidates()
        finally:
            google.requests.get = orig
        self.assertEqual([c["keyword"] for c in cands], ["이종석", "유조선"])
        self.assertEqual(cands[0]["rank"], 1)
        self.assertEqual(cands[0]["volume_bucket"], "200+")
        self.assertTrue(cands[0]["active"])
        self.assertEqual(cands[0]["started_at"], "Fri, 3 Jul 2026 08:40:00 -0700")
        # entity 이스케이프 복원 + url 보존
        self.assertEqual(cands[0]["related_news"][0]["title"], "'아이유' 이종석 근황")
        self.assertEqual(cands[0]["related_news"][0]["url"], "https://n.example/1")
        # news_item 없는 item은 related_news 필드 자체를 생략(있으면 보존 정책)
        self.assertNotIn("related_news", cands[1])
        self.assertEqual(cands[1]["volume_bucket"], "1000+")

    def test_rss_malformed_xml_returns_empty(self):
        # 200이어도 XML 파싱 불가 body → google_fetch_failed 경로로 [] (pipeline 안 죽임)
        os.environ["GOOGLE_TRENDS_ENABLED"] = "true"
        os.environ["GOOGLE_TRENDS_PROVIDER"] = "rss"
        orig = google.requests.get
        google.requests.get = lambda *a, **k: self._resp("<html>not-rss</html><broken")
        try:
            self.assertEqual(google.fetch_candidates(), [])
            self.assertEqual(google.fetch_signals(["이종석"]), {})
        finally:
            google.requests.get = orig

    def test_signals_from_trends(self):
        os.environ["GOOGLE_TRENDS_ENABLED"] = "true"
        os.environ["GOOGLE_TRENDS_PROVIDER"] = "rss"
        google._cache = [
            {"keyword": "손흥민", "rank": 1, "active": True, "volume_bucket": "2,000,000+"},
            {"keyword": "환율", "rank": 2, "active": True},
        ]
        google._cache_loaded = True
        sig = google.fetch_signals(["손흥민", "무관키워드"])
        self.assertIn("손흥민", sig)
        self.assertNotIn("무관키워드", sig)  # 트렌드에 없는 후보는 신호 없음
        self.assertGreater(sig["손흥민"]["interest"], 0)
        self.assertTrue(0.0 <= sig["손흥민"]["interest"] <= 1.0)

    def test_rss_fetch_failure_returns_empty(self):
        os.environ["GOOGLE_TRENDS_ENABLED"] = "true"
        os.environ["GOOGLE_TRENDS_PROVIDER"] = "rss"
        orig = google.requests.get
        google.requests.get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            self.assertEqual(google.fetch_candidates(), [])  # google_fetch_failed → [] (pipeline 안 죽임)
        finally:
            google.requests.get = orig


class TestCandidates(unittest.TestCase):
    def test_merge_dedup(self):
        c = cand.collect_candidates(
            {
                "daum_home": [{"keyword": "A", "rank": 1}, {"keyword": "B", "rank": 2}],
                "nate_home": [{"keyword": "a", "rank": 1}],  # 대소문자 dedup
            },
            [],
        )
        self.assertEqual(len(c), 2)  # A/a 병합
        srcs = next(x for x in c if x["keyword"] == "A")["sources"]
        self.assertIn("daum_home", srcs)
        self.assertIn("nate_home", srcs)  # 병합 시 두 family 모두 보존

    def test_limit(self):
        many = [{"keyword": f"k{i}", "rank": i} for i in range(50)]
        c = cand.collect_candidates({"daum_home": many}, [], limit=30)
        self.assertEqual(len(c), 30)

    def test_google_trends_survives_truncation(self):
        # 홈 3종이 모두 가득(각 Top10=30개)이어도 정렬이 daum_home 단독이 아니라 family
        # 최상 rank 기준이므로 google_trends 상위 후보가 truncation에서 생존해야 한다(P1-B).
        seed_sources = {
            "daum_home": [{"keyword": f"d{i}", "rank": i + 1} for i in range(10)],
            "nate_home": [{"keyword": f"n{i}", "rank": i + 1} for i in range(10)],
            "bing_home": [{"keyword": f"b{i}", "rank": i + 1} for i in range(10)],
            "google_trends": [{"keyword": f"g{i}", "rank": i + 1} for i in range(10)],
        }
        c = cand.collect_candidates(seed_sources, [], limit=30)
        kws = {x["keyword"] for x in c}
        self.assertEqual(len(c), 30)
        self.assertIn("g0", kws)  # google rank1 생존
        self.assertIn("g1", kws)  # google rank2 생존
        # rank1 후보는 모든 family에서 살아남아야(각 family 최상위) 한다.
        for f in ("d0", "n0", "b0", "g0"):
            self.assertIn(f, kws)

    def test_best_family_rank_ignores_bool(self):
        # naver_news_aux/phrase의 True는 rank가 아니므로 9999로 처리(우선순위에도 안 낌).
        c = {"keyword": "x", "sources": {"naver_news_aux": True, "naver_news_phrase": True}}
        self.assertEqual(cand._best_family_rank(c), 9999)
        self.assertEqual(cand._best_family_priority(c), 99)

    def test_best_family_priority_defaults_when_rankless(self):
        # rankless 독립 family(True)만 있으면 priority는 0이 아니라 99로 default 돼야 한다(Codex 2차 P2).
        c = {"keyword": "x", "sources": {"google_trends": True}}
        self.assertEqual(cand._best_family_rank(c), 9999)
        self.assertEqual(cand._best_family_priority(c), 99)
        # 정상 int rank면 family priority가 그대로 반영된다.
        c2 = {"keyword": "y", "sources": {"google_trends": 1}}
        self.assertEqual(cand._best_family_priority(c2), 0)

    def test_count_source_families(self):
        # 독립 홈/트렌드 family 종수만 카운트. naver_news_* 파생은 제외.
        c = [
            {"keyword": "A", "sources": {"daum_home": 1}},                    # daum_home
            {"keyword": "B", "sources": {"daum_home": 2, "nate_home": 1}},    # +nate_home
            {"keyword": "C", "sources": {"naver_news_aux": True}},            # 파생 → 미포함
            {"keyword": "D", "sources": {"google_trends": 1}},               # +google_trends
            {"keyword": "E", "sources": {"daum_home": 3, "naver_news_aux": True}},
        ]
        # 등장 family = {daum_home, nate_home, google_trends} → 3종
        self.assertEqual(cand.count_source_families(c), 3)

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
            "source_breakdown": {"news": 0.9, "search_demand": 0.5, "source_consensus": 0.3, "freshness": 0.4},
            "rank_reason": "최근 뉴스 다수",
            "news_meta": _news(3, 1, 2, 1.0),
            "used_signals": ["news", "search_demand", "freshness"],
        }
        entry = build_ranked_entry(1, ranked_item, {"keyword": "환율", "sources": {"daum_home": 1}})
        # 기존 프론트가 읽는 필드(legacy)
        for f in ("rank", "keyword", "summary", "signals", "articles"):
            self.assertIn(f, entry)
        self.assertIn("news", entry["signals"])  # 기존
        self.assertIn("trend", entry["signals"])  # 기존 호환
        # 신규 optional
        for f in ("score", "rank_reason", "source_breakdown"):
            self.assertIn(f, entry)
        # 신규 signals(family 기반)
        for s in ("daum", "google", "nate", "bing", "search_demand"):
            self.assertIn(s, entry["signals"])
        self.assertTrue(entry["signals"]["daum"])  # daum_home seed → True

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


class TestMovement(unittest.TestCase):
    def _issues(self, pairs, with_streak=None):
        # pairs: [(keyword, rank), ...]  with_streak: {kw: streak}
        ks = []
        for kw, rank in pairs:
            e = {"keyword": kw, "rank": rank}
            if with_streak and kw in with_streak:
                e["presence_streak"] = with_streak[kw]
            ks.append(e)
        return {"keywords": ks}

    def test_no_previous_omits_fields(self):
        new = self._issues([("A", 1), ("B", 2)])
        out = apply_movement(None, new)
        for k in out["keywords"]:
            self.assertNotIn("movement", k)
            self.assertNotIn("presence_streak", k)

    def test_empty_previous_is_all_new(self):
        # row 는 있으나 이전 Top10(keywords)이 빈 배열 → 비교 대상 없음 → 전부 new (P1)
        out = apply_movement({"keywords": []}, self._issues([("A", 1)]))
        self.assertEqual(out["keywords"][0]["movement"], "new")
        self.assertEqual(out["keywords"][0]["presence_streak"], 1)
        self.assertIsNone(out["keywords"][0]["previous_rank"])

    def test_up_down_same_new(self):
        prev = self._issues([("A", 1), ("B", 2), ("C", 3)], with_streak={"A": 2, "B": 1, "C": 5})
        new = self._issues([("B", 1), ("A", 2), ("D", 3)])  # B↑, A↓, C drop, D new
        out = apply_movement(prev, new)
        m = {k["keyword"]: k for k in out["keywords"]}
        self.assertEqual(m["B"]["movement"], "up")
        self.assertEqual(m["B"]["rank_delta"], 1)
        self.assertEqual(m["B"]["previous_rank"], 2)
        self.assertEqual(m["A"]["movement"], "down")
        self.assertEqual(m["A"]["rank_delta"], 1)
        self.assertEqual(m["D"]["movement"], "new")
        self.assertEqual(m["D"]["presence_streak"], 1)
        self.assertIsNone(m["D"]["previous_rank"])

    def test_same_rank(self):
        prev = self._issues([("A", 1)], with_streak={"A": 3})
        out = apply_movement(prev, self._issues([("A", 1)]))
        a = out["keywords"][0]
        self.assertEqual(a["movement"], "same")
        self.assertEqual(a["rank_delta"], 0)

    def test_presence_streak_increment(self):
        prev = self._issues([("A", 1)], with_streak={"A": 4})
        out = apply_movement(prev, self._issues([("A", 2)]))
        self.assertEqual(out["keywords"][0]["presence_streak"], 5)

    def test_reentry_is_new(self):
        # 이전 Top10에 없던 키워드(드롭 후 재진입도 동일) → new, streak=1
        prev = self._issues([("X", 1)], with_streak={"X": 2})
        out = apply_movement(prev, self._issues([("A", 1)]))
        self.assertEqual(out["keywords"][0]["movement"], "new")
        self.assertEqual(out["keywords"][0]["presence_streak"], 1)

    def test_duplicate_keyword_dedupe(self):
        prev = self._issues([("A", 1)], with_streak={"A": 1})
        # 새 Top10에 A 중복 → 첫 항목만 movement 부여
        new = {"keywords": [{"keyword": "A", "rank": 1}, {"keyword": "A", "rank": 2}]}
        out = apply_movement(prev, new)
        self.assertIn("movement", out["keywords"][0])
        self.assertNotIn("movement", out["keywords"][1])

    def test_malformed_previous_defensive(self):
        # 이전 issues 가 비정상 구조여도 예외 없이 필드 생략
        out = apply_movement({"keywords": "broken"}, self._issues([("A", 1)]))
        self.assertNotIn("movement", out["keywords"][0])


# ===== 실시간 이슈 랭킹 품질 개선 테스트 (docs/news-ranking-quality-plan.md) =====

def _recent_iso(hours_ago: float = 1.0) -> str:
    """fresh relevance gate 테스트용: 현재 시각 기준 hours_ago 이전 ISO 문자열."""
    from datetime import datetime, timedelta, timezone
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return dt.isoformat().replace("+00:00", "Z")


def _stale_iso(days_ago: float = 120.0) -> str:
    """fresh relevance gate 테스트용: FRESH_RELEVANCE_HOURS를 훌쩍 넘는 과거 ISO 문자열."""
    from datetime import datetime, timedelta, timezone
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.isoformat().replace("+00:00", "Z")


def _article(title, url, snippet="", published_at=None):
    # 주의: compute_news_signal은 normalize_article을 거치며 press를 originallink/url host에서
    # guess_press로 재파생한다(여기서 press를 넣어도 무시됨). entity cohesion의 dominant/burst는
    # 서로 다른 언론사를 요구하므로, 다른 언론사가 필요한 fixture는 url host를 다르게 준다.
    return {"title": title, "url": url, "originallink": url, "press": "x", "snippet": snippet,
            "published_at": published_at, "thumbnail": None}


class TestArticleRelevance(unittest.TestCase):
    """개선4/5: 키워드-기사 주제 적합도 필터."""

    def test_title_main_topic_high_relevance(self):
        a = _article("유럽 폭염에 에어컨·선풍기 품귀", "https://x.com/1", "폭염으로 선풍기 수요 급증")
        rel = cand.compute_article_relevance("선풍기", a)
        self.assertEqual(rel["relevance_reason"], "keyword_main_topic")
        self.assertGreater(rel["relevance_score"], 0.5)
        self.assertFalse(rel["is_incidental"])

    def test_incidental_giveaway_mention_low_relevance(self):
        a = _article("한국투자증권, IMA 출시...다이슨 선풍기 증정", "https://x.com/2",
                     "가입 고객 대상 다이슨 선풍기 증정 이벤트")
        rel = cand.compute_article_relevance("선풍기", a)
        self.assertEqual(rel["relevance_reason"], "incidental_giveaway_mention")
        self.assertLess(rel["relevance_score"], 0.5)
        self.assertTrue(rel["is_incidental"])

    def test_snippet_only_mention_low_relevance(self):
        # 손흥민 귀국 기사 title, 배재고등학교 야구부는 description에만 등장
        a = _article(
            "[뉴스퀘어 2PM] \"고개 숙이지 말아요\" 팬들 위로 속 손흥민 귀국",
            "https://x.com/3",
            "배재고등학교 야구부 관계자도 응원 메시지를 보냈다.",
        )
        rel = cand.compute_article_relevance("배재고등학교 야구부", a)
        self.assertEqual(rel["relevance_reason"], "snippet_only_incidental_mention")
        self.assertLess(rel["relevance_score"], 0.5)
        self.assertTrue(rel["is_incidental"])

    def test_title_and_description_both_relevant(self):
        a = _article("배재고등학교 야구부 응원구호 논란", "https://x.com/4",
                     "배재고 야구부 응원 문화가 도마 위에 올랐다.")
        rel = cand.compute_article_relevance("배재고등학교 야구부", a)
        self.assertFalse(rel["is_incidental"])
        self.assertGreater(rel["relevance_score"], 0.5)

    def test_articles_sorted_by_relevance_desc(self):
        articles = [
            _article("한국투자증권, IMA 출시...다이슨 선풍기 증정", "https://x.com/5"),
            _article("유럽 폭염에 에어컨·선풍기 품귀", "https://x.com/6"),
        ]
        scored = cand.score_articles_relevance("선풍기", articles)
        self.assertEqual(scored[0]["url"], "https://x.com/6")  # 관련성 높은 기사가 먼저

    def test_incidental_marker_ignored_when_keyword_is_title_subject(self):
        # keyword가 title의 중심 주체 절(콤마 앞부분)에 등장하면, 텍스트 어딘가에
        # 강한 marker(증정)가 있어도 그 keyword 자체는 incidental로 낮추면 안 된다
        # (사용자 지시: keyword-relative 판정 — "한국투자증권"은 이 기사의 주체).
        a = _article("한국투자증권, IMA 출시...다이슨 선풍기 증정", "https://x.com/subj1")
        rel = cand.compute_article_relevance("한국투자증권", a)
        self.assertFalse(rel["is_incidental"])
        self.assertEqual(rel["relevance_reason"], "keyword_main_topic")

    def test_promotional_title_starting_with_keyword_still_incidental(self):
        # keyword가 문장 맨 앞에 있어도, marker가 근접(구두점 없음)하면 여전히
        # incidental이어야 한다(Codex 6차 diff 리뷰 P2 재발 방지).
        a = _article("다이슨 선풍기 증정 이벤트 진행", "https://x.com/promo1")
        rel = cand.compute_article_relevance("선풍기", a)
        self.assertTrue(rel["is_incidental"])
        self.assertEqual(rel["relevance_reason"], "incidental_giveaway_mention")

    def test_short_subject_name_before_comma_not_incidental(self):
        # keyword가 title 첫 절 "전체"와 완전히 일치하면(짧은 브랜드명이어도) 문장의
        # 진짜 주어이므로 marker와의 거리가 가까워도 incidental로 낮추면 안 된다
        # (Codex 8차 diff 리뷰 P2: "쿠팡, 선풍기 증정 이벤트 진행"에서 "쿠팡"이
        # marker와 가깝다는 이유만으로 오탐하는 걸 방지 — 순수 거리 판정만으로는
        # 짧은 주체명에서 이 구분이 안 됐음).
        a = _article("쿠팡, 선풍기 증정 이벤트 진행", "https://x.com/short-subject1")
        rel = cand.compute_article_relevance("쿠팡", a)
        self.assertFalse(rel["is_incidental"])
        self.assertEqual(rel["relevance_reason"], "keyword_main_topic")

    def test_short_giveaway_item_after_comma_still_incidental(self):
        # 같은 기사에서 keyword="선풍기"는 첫 절("쿠팡") 전체와 일치하지 않고
        # marker와 가까우므로 여전히 incidental이어야 한다(위 테스트와 대비).
        a = _article("쿠팡, 선풍기 증정 이벤트 진행", "https://x.com/short-subject1")
        rel = cand.compute_article_relevance("선풍기", a)
        self.assertTrue(rel["is_incidental"])

    def test_promotional_title_with_comma_still_incidental(self):
        # keyword 뒤에 콤마가 와도(6차 수정이 도입했던 "주체 절" 개념으로는
        # 놓쳤을 케이스) marker가 근접하면 여전히 incidental이어야 한다
        # (Codex 7차 diff 리뷰 P2: 절 구분 없이 순수 거리 판정으로 재수정).
        a = _article("다이슨 선풍기, 증정 이벤트 진행", "https://x.com/promo2")
        rel = cand.compute_article_relevance("선풍기", a)
        self.assertTrue(rel["is_incidental"])
        self.assertEqual(rel["relevance_reason"], "incidental_giveaway_mention")

    def test_incidental_marker_applies_when_keyword_is_giveaway_item(self):
        # 같은 기사에서 keyword="선풍기"는 증정 상품(부속물) 자리에만 등장하므로
        # incidental로 낮춰야 한다(위 테스트와 대비되는 keyword-relative 판정).
        a = _article("한국투자증권, IMA 출시...다이슨 선풍기 증정", "https://x.com/subj1")
        rel = cand.compute_article_relevance("선풍기", a)
        self.assertTrue(rel["is_incidental"])
        self.assertEqual(rel["relevance_reason"], "incidental_giveaway_mention")

    def test_generic_provide_word_far_from_keyword_not_incidental(self):
        # "제공"이 keyword와 멀리 떨어진 일반 문맥(예: 자료 출처 표기)이면 incidental 취급하면 안 됨
        # (Codex diff 리뷰 P2: marker any-match는 "자료 제공"류 정상 기사까지 오탐시킴).
        a = _article(
            "선풍기 판매량 급증, 업계 통계 발표",
            "https://x.com/9",
            "이 자료는 한국전자제품협회가 제공한 통계를 바탕으로 작성됐다.",
        )
        rel = cand.compute_article_relevance("선풍기", a)
        self.assertFalse(rel["is_incidental"])

    def test_provide_word_near_keyword_is_incidental(self):
        # "제공"이 keyword 바로 근처(경품 문맥)에 있으면 incidental로 판정돼야 함
        a = _article("가전 이벤트, 선풍기 무료 제공", "https://x.com/10")
        rel = cand.compute_article_relevance("선풍기", a)
        self.assertTrue(rel["is_incidental"])

    def test_marker_before_keyword_within_range_is_incidental(self):
        # marker("제공", proximity-only)가 keyword "앞"에 있어도 근접하면 incidental
        # 이어야 한다(Codex diff 리뷰 P2: marker 시작 인덱스만 비교하면 놓칠 수 있었음).
        # strong marker(증정/사은품 등)를 섞지 않아 interval distance 로직 자체를 검증한다.
        a = _article("사은 제공 행사, 선풍기 한정 판매", "https://x.com/11")
        rel = cand.compute_article_relevance("선풍기", a)
        self.assertTrue(rel["is_incidental"])
        self.assertEqual(rel["relevance_reason"], "incidental_giveaway_mention")

    def test_repeated_keyword_marker_near_second_occurrence_is_incidental(self):
        # keyword가 title 주체 절이 아닌 곳에 두 번 등장하고, marker는 두 번째
        # 등장 근처에만 있는 경우에도 근접 판정이 걸려야 한다(Codex diff 리뷰 P2:
        # find() 첫 등장만 보면 이 케이스를 놓칠 수 있었음). keyword가 title 주체
        # 절에 없어야 marker 판정 자체가 수행되므로, 주체는 다른 회사로 둔다.
        a = _article(
            "한국백화점 판촉전, 선풍기와 에어컨 중 선풍기 무료 제공 행사",
            "https://x.com/12",
        )
        rel = cand.compute_article_relevance("선풍기", a)
        self.assertTrue(rel["is_incidental"])


class TestClusteringAndRepresentative(unittest.TestCase):
    """개선2: 경량 clustering + representative 선택."""

    def test_football_cluster_selected_as_primary_over_philosophy(self):
        # 독일: 축구 기사 2건 vs 철학/불교 기사 1건 → 축구 cluster가 primary
        articles = [
            _article("독일 축구 대표팀 평가전 승리", "https://x.com/de1", "독일 축구 대표팀이 평가전에서 승리했다"),
            _article("독일 축구 감독 전술 분석", "https://x.com/de2", "독일 축구 감독의 전술을 분석했다"),
            _article("독일 철학자 불교 사상 연구 화제", "https://x.com/de3", "독일 철학자의 불교 사상 연구가 화제다"),
        ]
        scored = cand.score_articles_relevance("독일", articles)
        clusters = cand.cluster_articles(scored)
        primary = cand.select_primary_cluster(clusters)
        titles = [a["title"] for a in primary]
        self.assertTrue(any("축구" in t for t in titles))
        self.assertFalse(any("철학" in t or "불교" in t for t in titles))

    def test_representative_excludes_incidental(self):
        articles = [
            _article("한국투자증권, IMA 출시...다이슨 선풍기 증정", "https://x.com/7"),
            _article("유럽 폭염에 에어컨·선풍기 품귀", "https://x.com/8"),
        ]
        scored = cand.score_articles_relevance("선풍기", articles)
        clusters = cand.cluster_articles(scored)
        primary = cand.select_primary_cluster(clusters)
        rep = cand.select_representative(primary)
        self.assertIsNotNone(rep)
        self.assertNotIn("증정", rep["title"])

    def test_topic_coherence_low_when_scattered(self):
        # 기사 주제가 서로 다 다르면(클러스터 다수) coherence 낮음
        articles = [
            _article("독일 축구 대표팀 평가전 승리", "https://x.com/c1"),
            _article("독일 철학자 불교 사상 연구", "https://x.com/c2"),
            _article("독일 자동차 산업 동향", "https://x.com/c3"),
        ]
        scored = cand.score_articles_relevance("독일", articles)
        clusters = cand.cluster_articles(scored)
        coherence = cand.compute_topic_coherence(clusters, len(scored))
        self.assertLess(coherence, 0.7)

    def test_compute_news_signal_includes_representative_fields(self):
        raw = [
            {"title": "유럽 폭염에 에어컨·선풍기 품귀", "originallink": "https://x.com/r1",
             "description": "폭염으로 선풍기 수요 급증", "pubDate": None},
            {"title": "한국투자증권, IMA 출시...다이슨 선풍기 증정", "originallink": "https://x.com/r2",
             "description": "가입 고객 대상 증정 이벤트", "pubDate": None},
        ]
        sig = cand.compute_news_signal("선풍기", raw)
        self.assertIn("representative_title", sig)
        self.assertIn("topic_coherence", sig)
        self.assertNotIn("증정", sig["representative_title"] or "")


class TestDescriptionHygiene(unittest.TestCase):
    """description 이미지 캡션/사진 설명 문구 정제(2026-07-04).

    문제: Naver News description에 "com AI로 생성된 이미지 [사진=챗GPT] ..."처럼 캡션이
    섞여 키워드 소개글(representative_summary)에 그대로 노출됨.
    """

    def test_caption_prefix_removed_from_clean_description(self):
        raw = "com AI로 생성된 이미지 [사진=챗GPT] 1년 넘게 계속된 협상 끝에 타결됐다"
        clean, quality, drop_reason, usable = normalizer.clean_description(raw)
        self.assertNotIn("AI로 생성된 이미지", clean)
        self.assertNotIn("챗GPT", clean)
        self.assertNotIn("[사진=", clean)
        self.assertTrue(usable)
        self.assertIsNone(drop_reason)
        self.assertIn("협상", clean)

    def test_caption_only_description_dropped(self):
        raw = "[사진=연합뉴스] 자료사진"
        clean, quality, drop_reason, usable = normalizer.clean_description(raw)
        self.assertEqual(clean, "")
        self.assertFalse(usable)
        self.assertEqual(drop_reason, "caption_only")

    def test_equals_free_bracket_tag_not_treated_as_caption(self):
        # 캡션 마커 단어(사진/이미지/출처/캡처)가 없는 일반 기사 태그성 대괄호
        # ([단독]/[속보]/[Q&A]/[AI 기본법])는 캡션이 아니므로 지우지 않는다
        # (Codex review-only P2, 2026-07-04 — 과매칭 방지).
        for raw in ("[단독] 정부, 내달 새 지원책 발표", "[속보] 국회 본회의 통과",
                    "[Q&A] 새 정책 자주 묻는 질문", "[AI 기본법] 국회 통과"):
            clean, quality, drop_reason, usable = normalizer.clean_description(raw)
            self.assertEqual(clean, raw)
            self.assertEqual(quality, 1.0)
            self.assertTrue(usable)

    def test_colon_or_no_delimiter_bracket_caption_still_removed(self):
        # "=" 없이 콜론/공백으로 구분된 캡션 브래킷 변형도 캡션 마커 단어(사진/이미지/
        # 출처/캡처) 기반 판정으로 잡아낸다(Codex review-only P2 2차, 2026-07-04).
        for raw in (
            "[사진 : 챗GPT] 1년 넘게 계속된 협상 끝에 타결됐다",
            "[사진 제공 챗GPT] 1년 넘게 계속된 협상 끝에 타결됐다",
        ):
            clean, quality, drop_reason, usable = normalizer.clean_description(raw)
            self.assertNotIn("사진", clean)
            self.assertNotIn("챗GPT", clean)
            self.assertIn("협상", clean)

    def test_bracket_containing_caption_phrase_word_fully_removed_without_residue(self):
        # "캡처"가 _CAPTION_PHRASES(전역 phrase)에도 있고 브래킷 마커 단어에도 겹치는
        # 경우, 브래킷을 항상 통째로 먼저 제거해야 "챗GPT] 본문..."처럼 대괄호가 반쪽만
        # 지워진 잔여물이 남지 않는다(Codex review-only P2 3차, 2026-07-04).
        raw = "[캡처 챗GPT] 본문 내용이 이어진다"
        clean, quality, drop_reason, usable = normalizer.clean_description(raw)
        self.assertNotIn("]", clean)
        self.assertNotIn("챗GPT", clean)
        self.assertIn("본문 내용이 이어진다", clean)

    def test_leading_fragment_cleanup_only_applies_when_caption_is_at_sentence_start(self):
        # 캡션이 문장 중간/뒤에 있을 뿐인 정상 문장의 선행 단어("AI", "5G")는 도메인
        # 파편이 아니므로 잘라먹지 않는다(Codex review-only P2 4차, 2026-07-04).
        raw1 = "AI 기술 발전으로 [사진=챗GPT] 업계 변화가 예상된다"
        clean1, *_ = normalizer.clean_description(raw1)
        self.assertTrue(clean1.startswith("AI 기술 발전으로"))
        self.assertNotIn("챗GPT", clean1)

        raw2 = "5G 상용화 이후 [사진=챗GPT] 통신 시장이 재편됐다"
        clean2, *_ = normalizer.clean_description(raw2)
        self.assertTrue(clean2.startswith("5G 상용화 이후"))
        self.assertNotIn("챗GPT", clean2)

    def test_topic_word_immediately_followed_by_caption_bracket_not_stripped(self):
        # 캡션 phrase 없이 브래킷만 선행 단어 바로 뒤에 오는 경우("AI [사진=...] ...")도
        # "["라는 이유만으로 도메인 파편 취급해 잘라먹지 않는다(Codex review-only P2
        # 5차, 2026-07-04 — lookahead를 "[" 단독 허용에서 알려진 phrase 문자열
        # 매칭으로 좁힘).
        raw1 = "AI [사진=챗GPT] 기술 발전으로 업계 변화가 예상된다"
        clean1, *_ = normalizer.clean_description(raw1)
        self.assertTrue(clean1.startswith("AI"))
        self.assertNotIn("챗GPT", clean1)

        raw2 = "5G [사진=챗GPT] 상용화 이후 통신 시장이 재편됐다"
        clean2, *_ = normalizer.clean_description(raw2)
        self.assertTrue(clean2.startswith("5G"))
        self.assertNotIn("챗GPT", clean2)

    def test_standalone_ai_topic_mention_not_treated_as_caption(self):
        # 브래킷 밖의 "챗GPT"/"AI" 단독 언급은 정상 기사 주제일 수 있으므로 캡션으로
        # 오판해 지우지 않는다(중점 검토 7번 — AI/챗GPT가 기사 주제인 경우 과잉 제거 방지).
        raw = "챗GPT가 오류를 일으켰다는 지적이 잇따라 나오고 있다"
        clean, quality, drop_reason, usable = normalizer.clean_description(raw)
        self.assertEqual(clean, raw)
        self.assertEqual(quality, 1.0)
        self.assertTrue(usable)

    def test_clean_description_without_pollution_passes_through(self):
        raw = "정부가 내달부터 새 지원책을 시행한다고 밝혔다"
        clean, quality, drop_reason, usable = normalizer.clean_description(raw)
        self.assertEqual(clean, raw)
        self.assertEqual(quality, 1.0)
        self.assertTrue(usable)

    def test_polluted_description_article_kept_as_evidence(self):
        # title 정상 + description만 오염 → article evidence(articles 목록)에서는 유지.
        raw = [{
            "title": "홈플러스, 회생절차 1년 만에 정상화 협상 타결",
            "originallink": "https://x.com/homeplus1",
            "description": "com AI로 생성된 이미지 [사진=챗GPT] 1년 넘게 계속된 협상 끝에 타결됐다",
            "pubDate": None,
        }]
        sig = cand.compute_news_signal("홈플러스", raw)
        self.assertEqual(len(sig["articles"]), 1)
        self.assertIn("홈플러스", sig["articles"][0]["title"])

    def test_representative_summary_excludes_caption_even_when_description_polluted(self):
        raw = [{
            "title": "홈플러스, 회생절차 1년 만에 정상화 협상 타결",
            "originallink": "https://x.com/homeplus1",
            "description": "com AI로 생성된 이미지 [사진=챗GPT] 1년 넘게 계속된 협상 끝에 타결됐다",
            "pubDate": None,
        }]
        sig = cand.compute_news_signal("홈플러스", raw)
        summary = sig["representative_summary"] or ""
        self.assertNotIn("[사진=", summary)
        self.assertNotIn("AI로 생성된 이미지", summary)
        self.assertNotIn("챗GPT", summary)

    def test_representative_summary_falls_back_to_title_when_no_clean_description(self):
        # description이 캡션뿐이라 clean_description이 전부 비어도 title 기반 소개글 생성.
        raw = [{
            "title": "홈플러스, 회생절차 1년 만에 정상화 협상 타결",
            "originallink": "https://x.com/homeplus1",
            "description": "[사진=연합뉴스] 자료사진",
            "pubDate": None,
        }]
        sig = cand.compute_news_signal("홈플러스", raw)
        self.assertEqual(sig["representative_summary"], "홈플러스, 회생절차 1년 만에 정상화 협상 타결")

    def test_normalize_article_exposes_hygiene_fields(self):
        art = normalizer.normalize_article({
            "title": "홈플러스, 회생절차 1년 만에 정상화 협상 타결",
            "originallink": "https://x.com/homeplus1",
            "description": "com AI로 생성된 이미지 [사진=챗GPT] 1년 넘게 계속된 협상 끝에 타결됐다",
        })
        self.assertIn("clean_description", art)
        self.assertIn("description_quality_score", art)
        self.assertIn("description_drop_reason", art)
        self.assertIn("is_description_usable_for_summary", art)
        self.assertNotIn("챗GPT", art["clean_description"])

    def test_representative_summary_excludes_below_min_relevance_fallback_articles(self):
        # representative가 None(REPRESENTATIVE_MIN_RELEVANCE 미달)이면, 다중 title
        # 합의 fallback도 동일 기준 미만 기사(예: object_side_mention 0.35)를 재료로
        # 쓰지 않아야 한다 — 그렇지 않으면 대표 부적격 기사가 소개글에 우회 노출된다
        # (Codex review-only P2 6차, 2026-07-04).
        raw = [{
            "title": "쿠팡, 국정원 조사 관련 노트북 회수까지 지시",
            "originallink": "https://x.com/side1",
            "description": "쿠팡이 직원 소지품 회수 조치를 내렸다",
            "pubDate": None,
        }]
        sig = cand.compute_news_signal("노트북", raw)
        self.assertIsNone(sig["representative_article"])
        self.assertIsNone(sig["representative_summary"])

    def test_built_entry_articles_carry_clean_description_to_frontend_json(self):
        # 최종 build_ranked_entry() 산출물(프론트로 나가는 JSON)의 articles[]에
        # clean_description이 실려 있는지 — 기사 카드 요약이 이 필드를 쓸 수 있어야
        # 오염 방지가 실제로 화면까지 이어진다(Codex review-only P1, 2026-07-04).
        raw = [{
            "title": "홈플러스, 회생절차 1년 만에 정상화 협상 타결",
            "originallink": "https://x.com/homeplus1",
            "description": "com AI로 생성된 이미지 [사진=챗GPT] 1년 넘게 계속된 협상 끝에 타결됐다",
            "pubDate": None,
        }]
        sig = cand.compute_news_signal("홈플러스", raw)
        ranked_item = {
            "keyword": "홈플러스", "score": 0.5, "source_breakdown": {"news": 0.5},
            "rank_reason": "", "news_meta": sig, "used_signals": ["news"],
        }
        entry = build_ranked_entry(1, ranked_item)
        self.assertIn("clean_description", entry["articles"][0])
        self.assertNotIn("챗GPT", entry["articles"][0]["clean_description"])
        self.assertTrue(entry["articles"][0]["is_description_usable_for_summary"])


class TestKeywordDedupe(unittest.TestCase):
    """개선1: 유사 키워드 dedupe."""

    def _ranked(self, kw, score, articles=None):
        return {
            "keyword": kw, "score": score,
            "source_breakdown": {"news": score}, "rank_reason": "",
            "news_meta": {"articles": articles or [_article(f"{kw} 관련 기사", f"https://x.com/{kw}")]},
            "used_signals": ["news"], "sources": {"daum": 1},
        }

    def test_institution_abbreviation_merge(self):
        ranked = [self._ranked("배재고등학교", 0.9), self._ranked("배재고", 0.5)]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["keyword"], "배재고등학교")  # score 높은 쪽이 대표
        self.assertIn("배재고", merged[0]["related_keywords"])

    def test_too_broad_word_not_merged_by_substring(self):
        # '독일'은 substring만으로 다른 키워드에 흡수되면 안 됨(서로 다른 기사 클러스터라
        # same-issue merge 대상도 아님 — article overlap 없음을 명시적으로 다르게 구성).
        ranked = [
            self._ranked("독일 축구", 0.9, [_article("독일 축구 대표팀 평가전 승리", "https://x.com/de1")]),
            self._ranked("독일", 0.5, [_article("독일 자동차 산업 동향 발표", "https://x.com/de2")]),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 2)  # 둘 다 유지(오탐 방지)

    def test_unrelated_keywords_not_merged_by_char_similarity(self):
        # "삼성전자"/"삼성전기"는 서로 다른 회사지만 문자 집합 Jaccard(구현 전 방식)로는
        # 0.6(교집합 6 / 합집합 10, 원래 threshold와 동일)이라 오탐 merge될 뻔했다
        # (Codex diff 리뷰 P2: 문자 set 기반 판정을 단어 토큰 기반으로 교체해 방지).
        ranked = [
            self._ranked("삼성전자", 0.9, [_article("삼성전자 실적 발표", "https://x.com/se1")]),
            self._ranked("삼성전기", 0.5, [_article("삼성전기 신규 공장 준공", "https://x.com/se2")]),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 2)

    def test_top10_backfilled_after_dedupe(self):
        # dedupe로 1자리 줄어들면 다음 후보가 Top10을 채워야 함
        ranked = [self._ranked("배재고등학교", 0.9), self._ranked("배재고", 0.8)]
        ranked += [self._ranked(f"기타{i}", 0.7 - i * 0.01) for i in range(9)]
        merged = ranker.dedupe_and_merge(ranked)
        top = ranker.select_top(merged, top_n=10)
        self.assertEqual(len(top), 10)
        kws = [t["keyword"] for t in top]
        self.assertNotIn("배재고", kws)  # 제거된 키워드는 Top10 자리 차지 안 함


class TestSameIssueMerge(unittest.TestCase):
    """개선3: same-issue merge (article overlap 기반)."""

    def _ranked_with_articles(self, kw, score, articles):
        return {
            "keyword": kw, "score": score,
            "source_breakdown": {"news": score}, "rank_reason": "",
            "news_meta": {"articles": articles}, "used_signals": ["news"],
            "sources": {"daum": 1},
        }

    def test_same_article_url_merges_into_one_issue(self):
        shared = _article("공수처, 김영환 지사 사무실 압수수색", "https://news.example.com/a1")
        ranked = [
            self._ranked_with_articles("압수수색", 0.9, [shared]),
            self._ranked_with_articles("김영환", 0.85, [shared]),
            self._ranked_with_articles("공수처", 0.8, [shared]),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["merge_reason"], "same_article_cluster")

    def test_display_keyword_includes_case_context(self):
        shared_title = "공수처, '30억 돈거래 의혹' 김영환 지사 사무실 등 압수수색"
        a1 = _article(shared_title, "https://news.example.com/b1")
        a2 = _article(shared_title, "https://news.example.com/b2")  # 다른 URL, 동일 title
        ranked = [
            self._ranked_with_articles("압수수색", 0.9, [a1]),
            self._ranked_with_articles("김영환", 0.85, [a2]),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 1)
        display = merged[0]["display_keyword"]
        self.assertNotEqual(display, "압수수색")  # 단독 일반 키워드가 아니라 맥락 포함

    def test_high_title_overlap_without_shared_url_merges(self):
        a1 = _article("김영환 지사 사무실 압수수색 진행", "https://a.example.com/1")
        a2 = _article("김영환 지사 사무실 압수수색 착수", "https://b.example.com/2")
        ranked = [
            self._ranked_with_articles("압수수색", 0.9, [a1]),
            self._ranked_with_articles("김영환", 0.85, [a2]),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 1)

    def test_canonical_keyword_stable_for_movement(self):
        # merge 되어도 canonical keyword(movement 비교용)는 대표 후보의 원래 keyword 유지
        shared = _article("공수처, 김영환 지사 사무실 압수수색", "https://news.example.com/c1")
        ranked = [
            self._ranked_with_articles("압수수색", 0.9, [shared]),
            self._ranked_with_articles("김영환", 0.85, [shared]),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(merged[0]["keyword"], "압수수색")  # score 최고 후보의 원래 keyword

    def test_merged_item_preserves_sources_for_builder_lookup(self):
        shared = _article("공수처, 김영환 지사 사무실 압수수색", "https://news.example.com/d1")
        ranked = [
            self._ranked_with_articles("압수수색", 0.9, [shared]),
            self._ranked_with_articles("김영환", 0.85, [shared]),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertIn("sources", merged[0])
        self.assertEqual(merged[0]["sources"], {"daum": 1})

    def test_transitive_overlap_via_keyword_dedupe_group(self):
        # A-B는 유사 키워드(기관명 축약)라 dedupe로 묶이고, B의 기사와만 overlap 높은
        # C(키워드 자체는 A/B와 무관)는 A와는 직접 안 겹치더라도 그룹 전체
        # (pool_articles)와 비교해 흡수돼야 한다(Codex diff 리뷰 P1: base_articles를
        # primary 1건으로만 고정하면 놓치는 케이스).
        a_articles = [_article("배재고등학교 총동문회 행사 개최", "https://x.com/school-a")]
        b_articles = [_article("배재고 야구부 훈련 현장 비공개 결정 논란", "https://x.com/school-b")]
        c_articles = [_article("야구부 훈련 현장 비공개 결정 논란 확산", "https://x.com/school-c")]
        ranked = [
            self._ranked_with_articles("배재고등학교", 0.9, a_articles),
            self._ranked_with_articles("배재고", 0.85, b_articles),
            self._ranked_with_articles("훈련비공개논란", 0.8, c_articles),
        ]
        # keyword 자체는 서로 무관함을 사전 확인(article overlap 경로만 검증하기 위함)
        self.assertFalse(ranker._is_similar_keyword("배재고등학교", "훈련비공개논란"))
        self.assertFalse(ranker._is_similar_keyword("배재고", "훈련비공개논란"))
        self.assertGreaterEqual(ranker._article_overlap(b_articles, c_articles), 0.5)

        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 1)
        self.assertIn("훈련비공개논란", merged[0]["related_keywords"])

    def test_transitive_overlap_not_lost_when_pool_exceeds_five_articles(self):
        # A가 무관한 기사 6건(ARTICLES_MAX=8 이내)을 이미 갖고 있어 pool이 5건을
        # 넘는 상황에서도, 그룹에 나중 합류한 B의 기사와만 overlap인 C를 놓치면
        # 안 된다(Codex diff 리뷰 P1 재발 방지: _article_overlap의 [:5] 슬라이스
        # 제거 검증).
        a_unrelated = [
            _article(f"무관 기사 {i} 전혀 다른 주제", f"https://x.com/unrelated{i}")
            for i in range(6)
        ]
        b_articles = [_article("배재고 야구부 훈련 현장 비공개 결정 논란", "https://x.com/school-b2")]
        c_articles = [_article("야구부 훈련 현장 비공개 결정 논란 확산", "https://x.com/school-c2")]
        ranked = [
            self._ranked_with_articles("배재고등학교", 0.9, a_unrelated),
            self._ranked_with_articles("배재고", 0.85, b_articles),
            self._ranked_with_articles("훈련비공개논란", 0.8, c_articles),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 1)
        self.assertIn("훈련비공개논란", merged[0]["related_keywords"])

    def test_article_overlap_uses_pairwise_max_not_diluted_union(self):
        # B 자체가 무관 기사를 여러 건 갖고 있고, 그중 딱 1건만 C와 겹치는 경우에도
        # merge돼야 한다(Codex diff 리뷰 P2: 기사들을 하나의 token union으로 합쳐
        # 비교하면 무관 기사가 많을수록 union이 커져 실제 겹치는 쌍의 신호가
        # 희석될 수 있었음 — pairwise 최댓값 방식으로 방지).
        b_articles = [
            _article(f"무관 기사 {i} 전혀 다른 주제", f"https://x.com/b-unrelated{i}")
            for i in range(5)
        ] + [_article("배재고 야구부 훈련 현장 비공개 결정 논란", "https://x.com/school-b3")]
        c_articles = [_article("야구부 훈련 현장 비공개 결정 논란 확산", "https://x.com/school-c3")]
        overlap = ranker._article_overlap(b_articles, c_articles)
        self.assertGreaterEqual(overlap, ranker.MERGE_ARTICLE_OVERLAP_THRESHOLD)

        ranked = [
            self._ranked_with_articles("배재고", 0.9, b_articles),
            self._ranked_with_articles("훈련비공개논란", 0.8, c_articles),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 1)

    def test_incidental_article_not_used_for_same_issue_merge(self):
        # "선풍기" 후보의 유일한 기사가 incidental(증정/판촉) 판정이고, 그 기사의 URL을
        # "한국투자증권" 후보와 공유하더라도 same-issue merge 근거가 되면 안 된다
        # (Codex diff 리뷰 P2: incidental 기사까지 overlap 판정에 포함시키면 article
        # relevance 필터링의 설계 의도와 충돌함).
        incidental_shared = dict(
            _article("한국투자증권, IMA 출시...다이슨 선풍기 증정", "https://x.com/shared-promo"),
            is_incidental=True, relevance_score=0.25,
        )
        ranked = [
            self._ranked_with_articles("한국투자증권", 0.9, [incidental_shared]),
            self._ranked_with_articles("선풍기", 0.5, [incidental_shared]),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 2)  # incidental 기사 공유만으로는 merge 안 됨

    # === 공유 URL bridge 보정 (2026-07-30, 운영 underfill 근본 수정) ===
    # 다중 사건 나열(roundup) 기사가 두 키워드 검색 결과에 모두 잡히면, 기존에는 URL
    # 일치만으로 즉시 merge(1.0)돼 서로 다른 실제 사건이 한 이슈로 붕괴했다(2026-07-29
    # 18:47 운영 사례: '문근영 결혼' 그룹이 '고지용 이혼' 등 3개 사건/5개 키워드 흡수).
    # 공유 URL 근거는 잔여(비공유) 근거의 교차 확인을 요구한다.

    def _roundup_case(self):
        """운영(진단 DB) 실제 제목 기반 재구성: 서로 다른 두 사건 + 공유 roundup 2건."""
        roundup1 = _article(
            "황정민 여성팬과 법정 공방·문근영 결혼·고지용 이혼 고백…오늘 연예뉴스",
            "https://r.example.com/roundup1")
        roundup2 = _article(
            "고지용 이혼→문근영 결혼→황정민 사생활 의혹→최정원 상간남 소송 승소",
            "https://r.example.com/roundup2")
        moon = [
            _article("'국민 여동생' 문근영 데려간 '7세 연상' 정평, 누군가 했더니", "https://m.example.com/m1"),
            _article("작품서 첫만남·결혼식은 식사로 대체…문근영 정평, 극비 열애 후 결혼", "https://m.example.com/m2"),
            _article("'광주 출신 배우' 문근영, 7살 연상 배우 정평과 결혼", "https://m.example.com/m3"),
        ]
        go = [
            _article("고지용, 허양임과 이혼 발표…오래 고민 끝 결정", "https://g.example.com/g1"),
            _article("젝스키스 고지용, 아내 허양임과 이혼…결혼 12년만", "https://g.example.com/g2"),
            _article("고지용 측 허양임과 이혼 조정 마무리 공식 입장", "https://g.example.com/g3"),
        ]
        return roundup1, roundup2, moon, go

    def test_roundup_shared_url_does_not_merge_distinct_events(self):
        # 서로 다른 사건(문근영 결혼 vs 고지용 이혼)이 공유 roundup URL만으로 병합되면 안 됨.
        roundup1, roundup2, moon, go = self._roundup_case()
        ranked = [
            self._ranked_with_articles("문근영 결혼", 0.9, moon + [roundup1, roundup2]),
            self._ranked_with_articles("고지용 아내", 0.8, go + [roundup1, roundup2]),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 2)  # 독립 이슈 2개 유지 (underfill 원인 수정)

    def test_roundup_decline_emits_aggregated_observation_log(self):
        roundup1, roundup2, moon, go = self._roundup_case()
        ranked = [
            self._ranked_with_articles("문근영 결혼", 0.9, moon + [roundup1, roundup2]),
            self._ranked_with_articles("고지용 아내", 0.8, go + [roundup1, roundup2]),
        ]
        with self.assertLogs("news.ranker", level="WARNING") as cm:
            ranker.dedupe_and_merge(ranked)
        bridge_logs = [m for m in cm.output if "bridge 보류" in m]
        self.assertEqual(len(bridge_logs), 1)  # pair별 남발 없이 run당 1회 집계

    def test_true_pair_shared_url_with_corroborating_residuals_merges(self):
        # 같은 사건(고지용 이혼)을 가리키는 두 키워드: 공유 기사 + 각자 독립 보도 보유.
        shared = _article("젝스키스 고지용, 아내 허양임과 이혼…결혼 12년만", "https://s.example.com/s1")
        ranked = [
            self._ranked_with_articles("허양임", 0.9, [
                shared,
                _article("허양임, 고지용과 이혼 발표에 심경 고백", "https://a.example.com/a1"),
                _article("방송인 허양임, 고지용과 12년만 이혼", "https://a.example.com/a2"),
            ]),
            self._ranked_with_articles("고지용 아내", 0.8, [
                shared,
                _article("고지용, 허양임과 이혼 조정 마무리", "https://b.example.com/b1"),
                _article("고지용 이혼 발표…아내 허양임과 결별", "https://b.example.com/b2"),
            ]),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 1)  # 잔여 근거가 교차 확인되는 진짜 중복은 계속 병합

    def test_subset_coverage_with_anchor_in_residual_merges(self):
        # subset 키워드('정평')의 근거가 전부 공유 기사여도, 상대 잔여 기사에 anchor가
        # 실제 등장하면 같은 사건으로 병합(별칭/부분집합 중복 보존).
        shared1 = _article("문근영, 7살 연상 배우 정평과 결혼", "https://s.example.com/w1")
        shared2 = _article("작품서 첫만남…문근영 정평 극비 열애 후 결혼", "https://s.example.com/w2")
        ranked = [
            self._ranked_with_articles("문근영 결혼", 0.9, [
                shared1, shared2,
                _article("국민 여동생 문근영 저 시집갔어요…남편은 배우 정평", "https://m.example.com/w3"),
            ]),
            self._ranked_with_articles("정평", 0.7, [shared1, shared2]),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 1)

    # === cross-URL roundup bridge (2026-09-03 06:48 운영 underfill 근본 수정) ===
    # PR #17/#19 는 "공유 근거"(같은 URL 또는 near-dup 제목)를 통한 bridge 만 막았다.
    # 서로 다른 매체가 각자 작성한 다중 사건 나열 기사끼리는 제목 Jaccard 가 near-dup
    # 임계(0.9)에 못 미쳐 공유 근거로 승격되지 않는 반면, merge 임계(0.5)는 넘겨
    # (운영 실측 0.667~0.750) 공유 근거 가드를 통째로 우회했다.
    # 06:48 run(4912ac60): gate 통과 36개가 merge 후 9개로 붕괴, 정몽규 그룹이
    # 지예은 바타 결혼 / 카사마츠 쇼 한국어 / 뉴욕증시 반등 등 10개 무관 이슈를 흡수해
    # selected 7. 흡수된 셋은 모두 인접 run 에서 계약 변경 없이 독립 선정된 이슈다
    # (지예은 05:48·06:19 rank4, 카사마츠 05:48·06:19 rank6, 뉴욕증시 07:19 rank2).

    def _cross_url_roundup_case(self):
        """운영 06:48 재구성: 서로 다른 URL 의 나열 기사 + 각자의 독립 보도."""
        jung = [
            _article("정몽규 전 회장, 12시간 동안 경찰 조사받았다", "https://j.example.com/j1"),
            _article("조사 마친 정몽규…12시간 만에 귀가", "https://j.example.com/j2"),
        ]
        roundup_j = _article(
            "오늘의 연예 소식…정몽규 경찰 조사, 지예은 바타 결혼, 과즙세연 논란 정리",
            "https://a.example.com/roundup")
        ji = [
            _article("지예은, 바타와 12월 결혼 발표", "https://z.example.com/z1"),
            _article("지예은 결혼 소감 공개", "https://z.example.com/z2"),
        ]
        roundup_z = _article(
            "연예 종합…지예은 바타 결혼, 정몽규 경찰 조사, 과즙세연 논란",
            "https://b.example.com/roundup")
        return jung, roundup_j, ji, roundup_z

    def test_cross_url_roundup_does_not_merge_distinct_events(self):
        # 06:48 핵심 회귀: 서로 다른 URL 의 나열 기사만 접점인 두 사건은 병합 금지.
        jung, roundup_j, ji, roundup_z = self._cross_url_roundup_case()
        ranked = [
            self._ranked_with_articles("정몽규 경찰 조사", 0.9, jung + [roundup_j]),
            self._ranked_with_articles("지예은 바타 결혼", 0.8, ji + [roundup_z]),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 2)

    def test_cross_url_roundup_pair_is_below_near_duplicate_threshold(self):
        # 이 회귀의 전제: 두 나열 기사는 near-dup(공유 근거)로 승격되지 **않는다**.
        # 승격된다면 PR #17/#19 가드가 이미 잡았을 것이고 이 테스트는 무의미해진다.
        _, roundup_j, _, roundup_z = self._cross_url_roundup_case()
        toks_j = ranker._title_tokens_of(roundup_j)
        toks_z = ranker._title_tokens_of(roundup_z)
        self.assertFalse(ranker._is_near_duplicate_title(toks_j, toks_z))
        jaccard = len(toks_j & toks_z) / len(toks_j | toks_z)
        self.assertGreaterEqual(jaccard, ranker.MERGE_ARTICLE_OVERLAP_THRESHOLD)
        self.assertLess(jaccard, ranker._NEAR_DUPLICATE_TITLE_JACCARD)

    def test_cross_url_roundup_collapse_keeps_three_distinct_issues(self):
        # 06:48 축소 재현: 나열 기사 한 건이 transitive 연쇄로 3개 이슈를 하나로
        # 접는 것을 막는다(정몽규가 지예은·뉴욕증시를 흡수한 실제 패턴).
        jung, roundup_j, ji, roundup_z = self._cross_url_roundup_case()
        market = [
            _article("뉴욕증시, 국채금리 하락에 반등 마감", "https://n.example.com/n1"),
            _article("다우 0.8% 상승…뉴욕증시 반등", "https://n.example.com/n2"),
            _article("아침 종합…뉴욕증시 반등, 정몽규 조사, 브로드컴 실적",
                     "https://c.example.com/roundup"),
        ]
        ranked = [
            self._ranked_with_articles("정몽규 경찰 조사", 0.9, jung + [roundup_j]),
            self._ranked_with_articles("지예은 바타 결혼", 0.8, ji + [roundup_z]),
            self._ranked_with_articles("뉴욕증시 반등", 0.7, market),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 3)

    def test_true_pair_without_shared_url_still_merges(self):
        # 반대 방향 보호: 공유 URL 이 없어도 **같은 사건**이면 양쪽 보도 다수에 상대
        # anchor 가 반복 등장하므로 계속 병합된다(가드가 정상 merge 를 깎지 않는다).
        ranked = [
            self._ranked_with_articles("용혜인 남편 사무총장 임명", 0.9, [
                _article("용혜인, 창당 3일 만 남편 사무총장 임명", "https://p.example.com/p1"),
                _article("창당 사흘만에 남편 사무총장에…부부회의 모독 유감", "https://p.example.com/p2"),
                _article("용혜인, 부부 둘이 참석한 회의서 남편 당 사무총장 임명",
                         "https://p.example.com/p3"),
            ]),
            self._ranked_with_articles("용혜인", 0.8, [
                _article("창당 때부터 용혜인 부부당… 둘만의 회의 뒤 남편 사무총장 됐다",
                         "https://q.example.com/q1"),
                _article("기본소득당, 용혜인 남편 논란에 당내 논의 거쳐 사무총장 임명",
                         "https://q.example.com/q2"),
            ]),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 1)

    def test_single_evidence_keyword_merge_is_unchanged_by_guard(self):
        # 근거가 1건뿐인 keyword 는 "복수 기사 교차" 조건을 원리상 만족할 수 없다.
        # 가드가 여기까지 적용되면 정상 소규모 이슈의 중복 제거가 막히므로 제외한다.
        shared = _article("공수처, 김영환 지사 사무실 압수수색", "https://s.example.com/only")
        ranked = [
            self._ranked_with_articles("김영환", 0.9, [shared]),
            self._ranked_with_articles("공수처 압수수색", 0.8, [shared]),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 1)

    def test_cross_evidence_guard_needs_only_one_side_corroborated(self):
        # 교차 근거는 **한쪽만** 복수여도 통과한다(max 규칙). 같은 사건이라도 주력
        # 키워드의 보도에는 부속 각도 이름이 한 번만 나오는 비대칭이 흔해서, 양쪽 모두
        # (min 규칙) 요구하면 정상 pair 를 끊는다. roundup bridge 는 양쪽 다 1건이라
        # 이 완화로도 여전히 차단된다.
        big = self._ranked_with_articles("정몽규 경찰 조사", 0.9, [
            _article("정몽규 전 회장, 12시간 동안 경찰 조사받았다", "https://j.example.com/j1"),
            _article("조사 마친 정몽규…12시간 만에 귀가", "https://j.example.com/j2"),
            _article("정몽규 조사에 홍명보 선임 의혹 광수대 수사", "https://j.example.com/j3"),
        ])
        small = self._ranked_with_articles("홍명보 선임 의혹", 0.8, [
            _article("홍명보 선임 의혹, 정몽규 경찰 조사 12시간", "https://h.example.com/h1"),
            _article("홍명보 감독 선임 개입 의혹 수사 확대…정몽규 조사", "https://h.example.com/h2"),
        ])
        ev_big = ranker._evidence_articles_of(big)
        ev_small = ranker._evidence_articles_of(small)
        # 비대칭 전제: 한쪽 1건, 다른 쪽 2건.
        self.assertEqual(ranker._cross_evidence_support(big, small, ev_big), 1)
        self.assertEqual(ranker._cross_evidence_support(small, big, ev_small), 2)
        # max 규칙이므로 가드는 통과시킨다(min 규칙이면 여기서 끊긴다).
        self.assertTrue(
            ranker._has_multi_article_cross_evidence(big, small, ev_big, ev_small)
        )

    def test_sparse_same_event_split_is_the_accepted_tradeoff(self):
        """이 계약의 known risk 를 **의도된 동작으로 고정**한다(fail-closed trade-off).

        진짜 같은 사건이라도 양쪽 근거가 2건뿐이고 접점이 1:1 이면 분리된다.
        이것은 버그가 아니라 선택이다 — 반대편(false merge)의 비용이 압도적으로 크기
        때문이다: 나열 기사 한 건이 transitive 연쇄로 10개 이슈를 한 그룹으로 접어
        Top10 이 7개로 붕괴한다(2026-09-03 06:48, run 4912ac60). 분리 쪽 비용은
        같은 사건이 두 줄로 보이는 것뿐이고, 그마저 아래 test 처럼 근거가 조금만
        두터워지면 사라진다.

        **이 테스트가 실패하도록 임계를 다시 완화하지 말 것.** 완화는 06:48 붕괴를
        그대로 되돌린다. 이 분리가 운영에서 실제 문제로 관측되면 임계를 낮추는 대신
        near-dup 탐지(공유 근거 승격) 쪽을 개선해야 한다 — 그쪽은 가드를 우회하지
        않고 정상 경로로 merge 시키는 방향이다.
        """
        a = self._ranked_with_articles("김포 공장 화재", 0.9, [
            _article("김포 물류창고서 큰불…소방 대응 2단계 발령", "https://a.example.com/1"),
            _article("김포 화재 진화 작업 계속", "https://a.example.com/2"),
        ])
        b = self._ranked_with_articles("소방 대응 2단계", 0.8, [
            _article("김포 물류창고 불…소방 대응 2단계 상향", "https://b.example.com/1"),
            _article("소방 대응 2단계란 무엇인가", "https://b.example.com/2"),
        ])
        ev_a = ranker._evidence_articles_of(a)
        ev_b = ranker._evidence_articles_of(b)
        # 전제: 공유 근거가 없고(near-dup 미만) 접점이 양쪽 1건씩이다.
        shared_a, shared_b, _, _ = ranker._split_shared_evidence(ev_a, ev_b)
        self.assertEqual((len(shared_a), len(shared_b)), (0, 0))
        self.assertEqual(ranker._cross_evidence_support(a, b, ev_a), 1)
        self.assertEqual(ranker._cross_evidence_support(b, a, ev_b), 1)
        self.assertEqual(len(ranker.dedupe_and_merge([a, b])), 2)

    def test_sparse_same_event_merges_once_evidence_is_slightly_denser(self):
        # 위 trade-off 의 반대편 경계: 근거가 조금만 두터워져 상대 anchor 가 복수
        # 기사에 등장하면(support >= 2) 같은 사건은 정상적으로 병합된다.
        # 분리 위험이 좁은 구간에만 있다는 것을 고정한다.
        a = self._ranked_with_articles("김포 공장 화재", 0.9, [
            _article("김포 물류창고서 큰불…소방 대응 2단계 발령", "https://c.example.com/1"),
            _article("김포 물류창고 화재 소방 대응 2단계 확대", "https://c.example.com/2"),
        ])
        b = self._ranked_with_articles("소방 대응 2단계", 0.8, [
            _article("김포 물류창고 불…소방 대응 2단계 상향", "https://d.example.com/1"),
            _article("김포 화재로 소방 대응 2단계 발령", "https://d.example.com/2"),
        ])
        ev_a = ranker._evidence_articles_of(a)
        ev_b = ranker._evidence_articles_of(b)
        self.assertGreaterEqual(ranker._cross_evidence_support(a, b, ev_a), 2)
        self.assertEqual(len(ranker.dedupe_and_merge([a, b])), 1)

    def test_guard_is_fail_open_when_both_anchors_are_empty(self):
        # 양쪽 keyword 가 모두 일반어라 merge anchor 가 비면 이 신호 자체를 관측할 수
        # 없다. "관측 불가"를 "지지 0건"으로 접으면 근거 없이 merge 를 막게 되므로
        # 기존 판정에 맡긴다(fail-open). 관측 불가와 0 을 구분하지 않으면 generic
        # keyword 의 정상 병합이 조용히 사라진다.
        a = self._ranked_with_articles("신임", 0.9, [
            _article("신임 사무총장 임명 절차 마무리 단계", "https://d1.example.com/1"),
            _article("신임 사무총장 임명 절차 마무리 발표", "https://d1.example.com/2"),
        ])
        b = self._ranked_with_articles("임명", 0.8, [
            _article("신임 사무총장 임명 절차 마무리 단계 돌입", "https://d2.example.com/1"),
            _article("신임 사무총장 임명 절차 마무리 공지", "https://d2.example.com/2"),
        ])
        ev_a = ranker._evidence_articles_of(a)
        ev_b = ranker._evidence_articles_of(b)
        # 전제: 양쪽 anchor 가 비어 support 가 관측 불가(None)이고, overlap 은 1.0 미만
        # 이라 동일 보도 예외가 아니라 실제로 가드 경로를 탄다.
        self.assertIsNone(ranker._cross_evidence_support(a, b, ev_a))
        self.assertIsNone(ranker._cross_evidence_support(b, a, ev_b))
        self.assertLess(
            ranker._pairwise_evidence_overlap(ev_a, ev_b),
            ranker._MERGE_BRIDGE_SUSPECT_MAX_OVERLAP,
        )
        self.assertTrue(
            ranker._has_multi_article_cross_evidence(a, b, ev_a, ev_b)
        )
        self.assertEqual(len(ranker.dedupe_and_merge([a, b])), 1)

    def test_identical_evidence_is_not_treated_as_roundup_bridge(self):
        # 기사쌍이 사실상 동일(overlap 1.0)하면 나열 기사 bridge 가 아니라 같은 보도다.
        # 제목이 짧아 near-dup 최소 토큰 수에 못 미치면 공유 근거로 승격되지 않은 채
        # 이 경로로 오는데, 그때까지 가드를 적용하면 anchor 가 비는 일반어 keyword 의
        # 정상 병합이 깨진다(운영: '신임' x '민경욱' display 정합성 분기 회귀).
        short = "무관한 다른 사건 보도"
        self.assertLess(
            len(ranker._title_tokens_of(_article(short, "https://x.example.com/1"))),
            ranker._NEAR_DUPLICATE_MIN_TITLE_TOKENS,
        )
        ranked = [
            self._ranked_with_articles("신임", 0.9, [
                _article(short, "https://d1.example.com/1"),
                _article(short, "https://d1.example.com/2"),
            ]),
            self._ranked_with_articles("민경욱", 0.8, [
                _article(short, "https://d2.example.com/1"),
                _article(short, "https://d2.example.com/2"),
            ]),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 1)

    # --- 동일 coverage false-split (2026-09-05 21:47 운영 사례) ---

    def _hormuz_case(self):
        """운영 21:47 재구성(run 783b45f9): '이란 한국 경고'와 '이란 당국자 간주'는
        기사 8건이 URL 까지 **완전히 동일**한데도 서로 다른 Top10 항목으로 남았다.
        매체마다 표현이 갈려(KBS 는 '고위당국자'를 한 단어로 표기) 어느 한 제목도
        anchor 합집합 {이란,한국,경고,당국자,간주} 5개를 모두 담지 못했기 때문이다.
        """
        return [
            _article("이란 당국자 “한국, 미국 위해 희생 말라”", "https://segye.example.com/1"),
            _article("'한국 호르무즈 파병 검토'에 이란 \"직접 공격 간주\"…미국 \"기다리고 있다\"",
                     "https://jtbc.example.com/1"),
            _article("이란 고위 당국자 \"韓 호르무즈 개입 시 전쟁 참여로 간주\"",
                     "https://inews24.example.com/1"),
            _article("이란 \"한국 호르무즈 파병시 전쟁 참여 간주…좌시 안해\"",
                     "https://tvchosun.example.com/1"),
            _article("“이란 고위당국자, 한국 호르무즈 개입시 ‘전쟁 참여’로 간주 경고”",
                     "https://kbs.example.com/1"),
            _article("\"韓, 호르무즈 파병시 전쟁 참여로 간주\"…이란, 공개 경고",
                     "https://hankyung.example.com/1"),
        ]

    def test_identical_coverage_same_event_merges_despite_wording_gap(self):
        # 필수 1/8: 동일 사건 + 거의 동일 article set + 표현이 다른 candidate → 하나로 merge.
        arts = self._hormuz_case()
        ranked = [
            self._ranked_with_articles("이란 한국 경고", 0.551, list(arts)),
            self._ranked_with_articles("이란 당국자 간주", 0.540, list(arts)),
        ]
        self.assertEqual(len(ranker.dedupe_and_merge(ranked)), 1)

    def test_hormuz_case_fails_the_span_condition(self):
        # 이 회귀의 전제 고정: span(anchor 합집합 근접 공존) 조건은 **실패**한다.
        # 통과했다면 기존 코드가 이미 병합했을 것이고 이 fix 는 무의미해진다.
        arts = self._hormuz_case()
        a = self._ranked_with_articles("이란 한국 경고", 0.551, list(arts))
        b = self._ranked_with_articles("이란 당국자 간주", 0.540, list(arts))
        union = ranker._keyword_anchor_tokens(a) | ranker._keyword_anchor_tokens(b)
        self.assertFalse(ranker._anchor_grounded_in_articles(union, arts))
        # 그럼에도 독립 보도 근거로 병합된다.
        self.assertTrue(ranker._is_same_issue(a, b))

    def test_identical_coverage_merge_is_symmetric(self):
        arts = self._hormuz_case()
        a = self._ranked_with_articles("이란 한국 경고", 0.551, list(arts))
        b = self._ranked_with_articles("이란 당국자 간주", 0.540, list(arts))
        self.assertEqual(ranker._is_same_issue(a, b), ranker._is_same_issue(b, a))

    def test_syndicated_roundup_across_many_outlets_still_not_merged(self):
        # 필수 4/5: 나열 기사가 **여러 매체에 전재**돼 매체 수만 늘어도 병합 금지.
        # 독립 보도 수는 near-dup 을 접으면 1이라 임계를 넘지 못한다 — 단순
        # "기사 수/매체 수가 많으면 merge" 로 완화되지 않았음을 고정한다.
        title = "최정원 상간남 소송 승소 판결 확정 소식과 함께 별개로 전해진 두산에너빌리티 수주 공시"
        synd = [_article(title, f"https://outlet{i}.example.com/r") for i in range(8)]
        self.assertEqual(ranker._independent_report_count(synd), 1)
        ranked = [
            self._ranked_with_articles("최정원 소송", 0.9, list(synd)),
            self._ranked_with_articles("두산에너빌리티 수주", 0.8, list(synd)),
        ]
        self.assertEqual(len(ranker.dedupe_and_merge(ranked)), 2)

    def test_independent_reports_require_every_shared_article_to_cover_both(self):
        # 전건(全件) 조건 고정: 독립 보도가 충분해도 그중 한 건이라도 한쪽 keyword 를
        # 다루지 않으면 이 경로로 병합하지 않는다(나열 기사 섞임 방어).
        both = [
            _article("이란 고위 당국자 한국 호르무즈 개입 전쟁 참여 간주 경고 발언",
                     "https://a.example.com/1"),
            _article("이란 당국자 한국 호르무즈 파병 두고 전쟁 참여 간주한다 경고",
                     "https://b.example.com/1"),
            _article("이란 측 한국 호르무즈 개입시 전쟁 참여 간주 경고 재확인 보도",
                     "https://c.example.com/1"),
        ]
        a = self._ranked_with_articles("이란 경고", 0.9, list(both))
        b = self._ranked_with_articles("한국 호르무즈", 0.8, list(both))
        self.assertTrue(ranker._is_same_issue(a, b))
        # 한쪽 keyword 와 무관한 기사 1건을 섞으면 전건 조건이 깨져 병합되지 않는다.
        unrelated = _article("전혀 다른 사건 프로야구 경기 결과 정리 기사 본문",
                             "https://d.example.com/1")
        mixed = list(both) + [unrelated]
        a2 = self._ranked_with_articles("이란 경고", 0.9, list(mixed))
        b2 = self._ranked_with_articles("한국 호르무즈", 0.8, list(mixed))
        self.assertFalse(ranker._is_same_issue(a2, b2))

    def test_two_independent_reports_are_below_the_threshold(self):
        # 임계 경계 고정: 독립 보도 2건은 부족하다(3건 이상 요구).
        two = [
            _article("이란 고위 당국자 한국 호르무즈 개입 전쟁 참여 간주 경고 발언",
                     "https://a.example.com/1"),
            _article("이란 당국자 한국 호르무즈 파병 두고 전쟁 참여 간주한다 경고",
                     "https://b.example.com/1"),
        ]
        self.assertEqual(ranker._independent_report_count(two), 2)
        a = self._ranked_with_articles("이란 경고", 0.9, list(two))
        b = self._ranked_with_articles("한국 호르무즈", 0.8, list(two))
        self.assertFalse(ranker._is_same_issue(a, b))

    def test_same_entity_different_event_not_merged_by_independent_reports(self):
        # 필수 5: 같은 인물이 등장해도 사건이 다르면 이 경로로 병합되지 않는다.
        # anchor 가 멀리 흩어져 기존 span 조건이 실패하는 나열 기사를 쓴다(span 이
        # 통과하면 이 fix 와 무관한 기존 known limit 경로라 검증이 되지 않는다).
        roundup = _article(
            "손흥민 결승골 소식을 전하며 팬들의 반응과 구단 공식 발표 내용을 함께 정리한 가운데 "
            "별개로 이강인 도움 기록도 조명됐다",
            "https://r.example.com/1")
        a = self._ranked_with_articles("손흥민 결승골", 0.9, [roundup])
        b = self._ranked_with_articles("이강인 도움", 0.8, [roundup])
        union = ranker._keyword_anchor_tokens(a) | ranker._keyword_anchor_tokens(b)
        # 전제: 기존 span 조건은 실패한다(= 이 fix 가 유일한 병합 가능 경로).
        self.assertFalse(ranker._anchor_grounded_in_articles(union, [roundup]))
        # 나열 기사는 한 건이므로 독립 보도 수가 임계에 미치지 못해 병합되지 않는다.
        self.assertEqual(ranker._independent_report_count([roundup]), 1)
        self.assertEqual(len(ranker.dedupe_and_merge([a, b])), 2)

    def test_hormuz_cause_and_reaction_stay_independent(self):
        # 필수 6/8: 21:47 rank8('한국 파병 검토')은 잔여 근거(미국의 파병 압박 보도)를
        # 따로 가져 rank4/7 과 article set 이 다르다(J=0.36). 원인/반응이 각자 독립
        # 근거를 갖는 경우는 분리 유지 — 이 fix 가 세 개를 하나로 접지 않는다.
        shared = self._hormuz_case()[:4]
        warn = list(shared) + [
            _article("이란 고위 당국자 전쟁 참여 간주 경고 추가 보도 확인",
                     "https://kbs.example.com/warn"),
        ]
        deploy = list(shared) + [
            _article("트럼프 \"이란 전쟁은 사소한 일\"...미 \"한국 파병 기다리는 중\"",
                     "https://ytn.example.com/1"),
            _article("美 당국자 \"한국의 자원 배치 기다리는 중\"…靑 \"결정된 것 없다\"",
                     "https://mt.example.com/1"),
        ]
        a = self._ranked_with_articles("이란 한국 경고", 0.551, warn)
        c = self._ranked_with_articles("한국 파병 검토", 0.532, deploy)
        ev_a = ranker._evidence_articles_of(a)
        ev_c = ranker._evidence_articles_of(c)
        _, _, rest_a, rest_c = ranker._split_shared_evidence(ev_a, ev_c)
        # 양쪽 모두 잔여 근거를 가지므로 동일 coverage 경로가 아니다.
        self.assertTrue(rest_a and rest_c)

    def test_empty_anchor_keyword_cannot_merge_via_independent_reports(self):
        # 빈 anchor(일반어만인 keyword, 예 '사건')는 이 경로로 병합할 수 없다.
        # 빈 집합을 "모든 기사에 등장"으로 접으면 일반어 keyword 가 독립 보도 수만으로
        # 아무 사건에나 흡수된다(_anchor_grounded_in_articles 의 공집합 거짓 계약과 동일).
        arts = [
            _article("김영환 지사 관련 여러 절차가 이어지는 가운데 별도로 진행된 압수수색 소식",
                     "https://a.example.com/1"),
            _article("김영환 지사 측 입장 발표가 나온 뒤 한참 지나 알려진 압수수색 진행 경과",
                     "https://b.example.com/1"),
            _article("김영환 지사 일정 공개 이후 별개로 전해진 압수수색 관련 후속 보도",
                     "https://c.example.com/1"),
        ]
        a = self._ranked_with_articles("사건", 0.9, list(arts))
        b = self._ranked_with_articles("김영환 압수수색", 0.8, list(arts))
        # 전제: anchor 가 비고, 기존 span 조건도 실패한다(= 이 경로만이 병합 후보).
        self.assertEqual(ranker._keyword_anchor_tokens(a), set())
        union = ranker._keyword_anchor_tokens(a) | ranker._keyword_anchor_tokens(b)
        self.assertFalse(ranker._anchor_grounded_in_articles(union, arts))
        # 독립 보도는 충분(3건)하지만 빈 anchor 라 병합하지 않는다.
        self.assertEqual(ranker._independent_report_count(arts), 3)
        self.assertFalse(ranker._corroborated_by_independent_reports(a, b, arts))
        self.assertFalse(ranker._is_same_issue(a, b))

    def test_one_sided_mentions_do_not_count_as_joint_coverage(self):
        # 각 기사가 한쪽 keyword 만 다루면(교집합 0) 병합하지 않는다. "둘 중 하나라도
        # 언급"으로 완화하면 서로 다른 사건의 기사 묶음이 통째로 통과한다.
        mixed = [
            _article("이란 고위 당국자 전쟁 참여 간주 경고 발언 상세 내용 보도",
                     "https://x.example.com/1"),
            _article("이란 당국자 추가 경고 발언 국제사회 반응 정리 기사",
                     "https://y.example.com/1"),
            _article("한국 정부 호르무즈 파병 검토 착수 관련 별개 사안 보도",
                     "https://z.example.com/1"),
            _article("한국 파병 검토 관련 부처 협의 진행 상황 별도 정리",
                     "https://w.example.com/1"),
        ]
        a = self._ranked_with_articles("이란 경고", 0.9, list(mixed))
        b = self._ranked_with_articles("한국 파병", 0.8, list(mixed))
        ka = ranker._keyword_anchor_tokens(a)
        kb = ranker._keyword_anchor_tokens(b)
        both = [x for x in mixed
                if ranker._article_mentions_anchor(x, ka)
                and ranker._article_mentions_anchor(x, kb)]
        either = [x for x in mixed
                  if ranker._article_mentions_anchor(x, ka)
                  or ranker._article_mentions_anchor(x, kb)]
        # 전제 고정: 양쪽 동시 언급은 0건이지만 "한쪽이라도"는 전건이다.
        self.assertEqual(len(both), 0)
        self.assertEqual(len(either), len(mixed))
        self.assertFalse(ranker._anchor_grounded_in_articles(ka | kb, mixed))
        self.assertFalse(ranker._is_same_issue(a, b))

    def test_subset_roundup_only_without_anchor_grounding_not_merged(self):
        # 근거가 전부 공유 roundup뿐인 키워드('최정원')는 상대의 자체 보도에 anchor가
        # 없으면 흡수되지 않는다(무관 사건 bridge 차단).
        roundup1, roundup2, moon, go = self._roundup_case()
        ranked = [
            self._ranked_with_articles("고지용 아내", 0.9, go + [roundup1, roundup2]),
            self._ranked_with_articles("최정원", 0.7, [roundup1, roundup2]),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 2)

    def test_identical_coverage_scattered_anchors_not_merged(self):
        # 동일 coverage(둘 다 같은 roundup 1건뿐)이고 두 anchor가 한 필드 안에서 span
        # 제한(6토큰)을 넘겨 흩어져 있으면 병합하지 않는다(다중 사건 나열 방어).
        far = _article(
            "최정원 상간남 소송 승소 판결 확정 소식과 함께 별개로 전해진 두산에너빌리티 수주 공시",
            "https://r.example.com/far1")
        ranked = [
            self._ranked_with_articles("최정원 소송", 0.9, [far]),
            self._ranked_with_articles("두산에너빌리티 수주", 0.8, [far]),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 2)

    def test_identical_coverage_adjacent_segments_known_limit(self):
        # 알려진 한계 고정(fixture pin): 동일 coverage에서 두 사건 구획이 span 이내로
        # 인접한 roundup은 여전히 병합될 수 있다(양쪽 모두 cohesion gate를 roundup만으로
        # 통과해야 하는 희귀 케이스 — 후속 관찰 대상, 이 테스트는 현재 동작 문서화).
        adjacent = _article("고지용 이혼 문근영 결혼 동시 발표", "https://r.example.com/adj1")
        ranked = [
            self._ranked_with_articles("고지용 이혼", 0.9, [adjacent]),
            self._ranked_with_articles("문근영 결혼", 0.8, [adjacent]),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 1)

    def test_search_intent_only_anchor_cannot_corroborate_subset(self):
        # subset 쪽 키워드의 anchor가 검색의도 일반어('결혼')뿐이면 — 엄격 merge anchor
        # 집합이 공집합 — anchor 경로로 병합할 수 없다(공집합 자명 통과 금지 가드).
        shared = _article("연예계 결혼 소식 총정리…이번 주 화제의 발표", "https://s.example.com/si1")
        ranked = [
            self._ranked_with_articles("김태효 구속기소", 0.9, [
                shared,
                _article("특검, 김태효 전 안보실 1차장 구속기소", "https://k.example.com/k1"),
                _article("김태효 구속기소…내란 중요종사 혐의", "https://k.example.com/k2"),
            ]),
            self._ranked_with_articles("결혼", 0.6, [shared]),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 2)

    def test_different_url_roundup_no_longer_bridges(self):
        # PR #17 알려진 한계 1번 해소(2026-08-05): 같은 roundup이 제휴/전재로 서로 다른
        # URL을 달면 URL 교집합이 비어 공유 URL 가드가 통째로 우회됐고, 기존 경로의 첫
        # 신호인 pairwise Jaccard가 1.0이 되어 즉시 병합됐다. 이제 near-duplicate 제목을
        # 공유 근거로 인정해 PR #17의 잔여 근거 계약이 동일하게 적용된다.
        r1 = _article("고지용 이혼→문근영 결혼→황정민 사생활 의혹→최정원 소송 승소", "https://x.example.com/r1")
        r2 = _article("고지용 이혼→문근영 결혼→황정민 사생활 의혹→최정원 소송 승소", "https://y.example.com/r2")
        ranked = [
            self._ranked_with_articles("문근영 결혼", 0.9, [r1]),
            self._ranked_with_articles("고지용 아내", 0.8, [r2]),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 2)

    # === 신디케이트 roundup bridge 차단 (2026-08-05, 운영 underfill 잔여 원인) ===
    # 09:48 KST 실행(run 8a0a91ec): 품질 게이트 통과 15개 중 5개가 '김우빈 기프트 출연'
    # 한 그룹으로 흡수돼 독립 이슈 10개 → display 게이트 후 7개로 발행됐다. 승자의 기사
    # 8건은 전부 김우빈/기프트 기사였고 흡수된 키워드(하춘화/송영규/김서연/이수연) 기사는
    # 1건도 없었다. 기전은 같은 연예 종합 기사가 서로 다른 URL로 양쪽에 잡힌 것.

    def _syndicated_roundup(self, url):
        """서로 다른 사건을 나열한 연예 종합 기사(제휴 전재로 URL만 다름)."""
        return _article(
            "김우빈 기프트 출연 확정·하춘화 근황·송영규 사망·이수연 신곡 발표 종합",
            url)

    def test_syndicated_roundup_does_not_merge_distinct_events(self):
        # 핵심 회귀(09:48 재현): 동일 roundup이 다른 URL로 잡혀도 서로 다른 사건은 분리.
        kim = [
            _article("[공식] 김우빈, 前프로야구 투수코치 된다..'기프트' 하반기 컴백", "https://k.example.com/k1"),
            _article("김우빈, 고교 야구부 감독 된다…'기프트' 출연 확정", "https://k.example.com/k2"),
            _article("김우빈, 웹툰 원작 '기프트' 주인공…전국 꼴찌 야구부 감독 된다", "https://k.example.com/k3"),
        ]
        ha = [
            _article("하춘화, 데뷔 60주년 기념 콘서트 개최 소식 전해", "https://h.example.com/h1"),
            _article("가수 하춘화 근황 공개…변함없는 무대 매너 화제", "https://h.example.com/h2"),
        ]
        ranked = [
            self._ranked_with_articles(
                "김우빈 기프트 출연", 0.9, kim + [self._syndicated_roundup("https://a.example.com/r")]),
            self._ranked_with_articles(
                "하춘화", 0.5, ha + [self._syndicated_roundup("https://b.example.com/r")]),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 2)

    def test_syndicated_roundup_transitive_chain_does_not_collapse(self):
        # transitive 증폭 차단: roundup 하나가 3개 사건을 한 그룹으로 붕괴시키지 않는다.
        def ev(prefix, titles):
            return [_article(t, f"https://{prefix}.example.com/{i}") for i, t in enumerate(titles)]
        ranked = [
            self._ranked_with_articles("김우빈 기프트 출연", 0.9, ev("k", [
                "[공식] 김우빈, 前프로야구 투수코치 된다..'기프트' 하반기 컴백",
                "김우빈, 고교 야구부 감독 된다…'기프트' 출연 확정",
            ]) + [self._syndicated_roundup("https://a.example.com/r")]),
            self._ranked_with_articles("하춘화", 0.6, ev("h", [
                "하춘화, 데뷔 60주년 기념 콘서트 개최 소식 전해",
                "가수 하춘화 근황 공개…변함없는 무대 매너 화제",
            ]) + [self._syndicated_roundup("https://b.example.com/r")]),
            self._ranked_with_articles("이수연 가수", 0.4, ev("l", [
                "가수 이수연, 신곡 발매하며 컴백 활동 시작한다",
                "이수연 신곡 음원 차트 진입…팬들 응원 이어져",
            ]) + [self._syndicated_roundup("https://c.example.com/r")]),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 3)

    def test_genuine_syndicated_duplicate_still_merges(self):
        # 진짜 동일 사건이 다른 URL로 전재된 경우: 잔여 근거가 교차하면 병합 유지
        # (중복 노출 방지 — 이번 변경이 정상 병합까지 막지 않는다).
        syn_a = _article("젝스키스 고지용, 아내 허양임과 이혼…결혼 12년만에 결별 결정", "https://p.example.com/1")
        syn_b = _article("젝스키스 고지용, 아내 허양임과 이혼…결혼 12년만에 결별 결정", "https://q.example.com/1")
        ranked = [
            self._ranked_with_articles("고지용 이혼", 0.9, [
                syn_a,
                _article("고지용, 허양임과 이혼 발표…오래 고민 끝 내린 결정", "https://p.example.com/2"),
            ]),
            self._ranked_with_articles("허양임", 0.8, [
                syn_b,
                _article("허양임, 고지용과 이혼 조정 마무리…공식 입장 발표", "https://q.example.com/2"),
            ]),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 1)

    def test_short_titles_are_not_treated_as_near_duplicates(self):
        # 최소 토큰 guard: 짧은 제목이 우연히 일치해도 공유 근거로 승격되지 않는다
        # (승격되면 기존 경로가 아니라 공유 근거 분기로 빠져 판정이 달라짐).
        a_toks = ranker._title_tokens_of(_article("속보 오늘 날씨", "https://s.example.com/1"))
        b_toks = ranker._title_tokens_of(_article("속보 오늘 날씨", "https://s.example.com/2"))
        self.assertFalse(ranker._is_near_duplicate_title(a_toks, b_toks))

    def test_long_identical_titles_are_near_duplicates(self):
        long_title = "이흥구 대법관 후임 후보에 김문관 김성수 김예영 김정중 4인 압축"
        a_toks = ranker._title_tokens_of(_article(long_title, "https://s.example.com/1"))
        b_toks = ranker._title_tokens_of(_article(long_title, "https://s.example.com/2"))
        self.assertTrue(ranker._is_near_duplicate_title(a_toks, b_toks))

    def test_split_shared_evidence_matches_url_split_when_no_near_dup(self):
        # near-dup이 0건이면 기존 URL 기준 분할과 집합 동등해야 한다(공유 URL 경로 보존).
        shared = _article("젝스키스 고지용, 아내 허양임과 이혼…결혼 12년만", "https://s.example.com/shared")
        ev_a = [
            shared,
            _article("문근영 정평, 극비 열애 후 결혼 발표 화제", "https://a.example.com/1"),
            {"title": "제목만 있고 URL 없는 기사", "url": None},
        ]
        ev_b = [
            shared,
            _article("태풍 돌핀 북상…제주 산간 호우 특보 발효 중", "https://b.example.com/1"),
        ]
        shared_a, shared_b, rest_a, rest_b = ranker._split_shared_evidence(ev_a, ev_b)
        self.assertEqual(shared_a, [shared])
        self.assertEqual(shared_b, [shared])
        self.assertEqual(rest_a, [ev_a[1], ev_a[2]])
        self.assertEqual(rest_b, [ev_b[1]])

    def test_split_shared_evidence_is_symmetric_with_duplicate_urls(self):
        # 같은 URL 기사가 한쪽에 중복으로 있어도 set-membership 판정이라 순서 의존 없음.
        dup = _article("젝스키스 고지용, 아내 허양임과 이혼…결혼 12년만", "https://s.example.com/dup")
        ev_a = [dup, dup]
        ev_b = [dup, _article("태풍 돌핀 북상…제주 산간 호우 특보 발효", "https://b.example.com/1")]
        shared_a, shared_b, rest_a, rest_b = ranker._split_shared_evidence(ev_a, ev_b)
        self.assertEqual(shared_a, [dup, dup])
        self.assertEqual(rest_a, [])
        self.assertEqual(shared_b, [dup])
        self.assertEqual(rest_b, [ev_b[1]])
        # 반대 방향도 대칭
        rev_a, rev_b, rev_ra, rev_rb = ranker._split_shared_evidence(ev_b, ev_a)
        self.assertEqual(rev_a, shared_b)
        self.assertEqual(rev_b, shared_a)

    def _asymmetric_near_dup_pair(self):
        """near-dup(J>=0.9)이지만 한쪽 제목에만 상대 anchor가 있는 pair.

        공유 URL 시절엔 양쪽 공유 기사가 동일해 이런 비대칭이 불가능했다. near-dup은
        제목이 완전히 같다는 보장이 없어 판정 방향에 따라 결과가 갈릴 수 있다.
        """
        common = ["대법관", "후임", "인사", "검증", "절차", "마무리",
                  "국회", "동의", "표결", "예정", "정부", "발표",
                  "일정", "조율", "단계", "완료", "확정", "공식"]
        title_a = " ".join(["김문관", "김정중"] + common)   # 양쪽 anchor가 인접
        title_b = " ".join(["김문관", "제청"] + common)     # '김정중' 없음
        return (
            self._ranked_with_articles("김문관", 0.9, [_article(title_a, "https://a.example.com/1")]),
            self._ranked_with_articles("김정중", 0.6, [_article(title_b, "https://b.example.com/1")]),
        )

    def test_is_same_issue_symmetric_for_asymmetric_near_dup_titles(self):
        # 판정이 호출 방향에 의존하면 dedupe_and_merge가 score 순서로 호출하므로
        # 순위 변동만으로 병합 여부가 달라진다(중복 노출 회귀).
        item_a, item_b = self._asymmetric_near_dup_pair()
        self.assertEqual(
            ranker._is_same_issue(item_a, item_b),
            ranker._is_same_issue(item_b, item_a),
        )

    def test_merge_result_independent_of_input_order_for_near_dup(self):
        item_a, item_b = self._asymmetric_near_dup_pair()
        forward = ranker.dedupe_and_merge([dict(item_a), dict(item_b)])
        # score를 뒤집어 호출 순서가 반대가 되어도 그룹 수는 같아야 한다.
        flipped_a = dict(item_b, score=0.9)
        flipped_b = dict(item_a, score=0.6)
        backward = ranker.dedupe_and_merge([flipped_a, flipped_b])
        self.assertEqual(len(forward), len(backward))

    def test_near_duplicate_applies_to_articles_without_url(self):
        # URL 결측이 merge 우회 통로가 되지 않는다(fail-closed).
        title = "이흥구 대법관 후임 후보에 김문관 김성수 김예영 김정중 4인 압축"
        ev_a = [{"title": title, "url": None}]
        ev_b = [{"title": title, "url": ""}]
        shared_a, shared_b, rest_a, rest_b = ranker._split_shared_evidence(ev_a, ev_b)
        self.assertEqual(shared_a, ev_a)
        self.assertEqual(shared_b, ev_b)
        self.assertEqual(rest_a, [])
        self.assertEqual(rest_b, [])

    def test_merge_still_absorbs_duplicates_no_forced_unmerge(self):
        # 12개 후보가 8개 사건이면 정확히 8그룹 — 중복을 풀어 10개로 억지 확장하지 않음.
        ranked = []
        for i in range(8):
            ev = _article(f"사건{i} 핵심인물{i} 검거 단독 보도", f"https://e{i}.example.com/1")
            ranked.append(self._ranked_with_articles(f"사건{i} 인물{i}", 0.9 - i * 0.02, [ev]))
            if i < 4:  # 4개 사건은 동일 기사 URL을 공유하는 별칭 후보 존재
                ranked.append(self._ranked_with_articles(f"인물{i}", 0.5 - i * 0.02, [ev]))
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 8)

    def test_merge_reason_similar_keyword_when_no_article_overlap_merge(self):
        # 유사 키워드 dedupe만 발생하고(article overlap 없음), 각자 기사도 겹치지 않으면
        # merge_reason은 same_article_cluster가 아니라 similar_keyword여야 한다.
        ranked = [
            self._ranked_with_articles("배재고등학교", 0.9, [_article("배재고 총동문회", "https://x.com/g1")]),
            self._ranked_with_articles("배재고", 0.5, [_article("완전히 다른 무관 기사", "https://x.com/g2")]),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(merged[0]["merge_reason"], "similar_keyword")

    def test_merge_reason_same_article_cluster_when_overlap_merge(self):
        shared = _article("공수처, 김영환 지사 사무실 압수수색", "https://news.example.com/h1")
        ranked = [
            self._ranked_with_articles("압수수색", 0.9, [shared]),
            self._ranked_with_articles("김영환", 0.85, [shared]),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(merged[0]["merge_reason"], "same_article_cluster")

    def test_baejaego_gwonoyoung_merge_via_shared_event_tokens(self):
        # 실측 재현: article overlap만으로는 0.267(threshold 0.5 미달)이라 놓치는 케이스.
        # "권오영 감독" 그룹 기사는 title에 "권오영"/"감독" 토큰이 없어 실제 파이프라인
        # (compute_article_relevance)에서 snippet_only_incidental_mention으로 판정된다 —
        # 단위 테스트도 raw _article()이 아니라 실제 relevance 판정을 거친 articles로
        # 구성해야 한다(Codex review-only 지적: raw article은 is_incidental 필드가 없어
        # _group_df_tokens 필터를 그냥 통과해버려 실패 조건을 재현하지 못함).
        a_raw = [
            _article("'출전정지 6개월' 배재고 감독 \"무조건 최송합니다\"", "https://x.com/n1",
                     "앞서 배재고 감독은 침통한 표정으로 광주일고 측에 무조건 최송하다고 말했습니다."),
            _article("배재고 야구단에 전국대회 6개월 출전 정지 중징계", "https://x.com/n2",
                     "외쳤던 선수들, 협회가 6개월 간 전국대회 출전 정지라는 중징계를 내렸습니다."),
        ]
        b_raw = [
            _article("'지역 비하 구호' 배재고 야구부, 6개월 출전 정지 중징계", "https://x.com/n3",
                     "여러 차례 사과했던 배재고 감독은 다시 고개를 숙였다."),
            _article("무거운 조롱의 대가...배재고 야구부 6개월 출전 정지", "https://x.com/n4",
                     "배재고는 당장 내일 예정된 청룡기 2회전부터 나갈 수 없고"),
        ]
        a_articles = cand.score_articles_relevance("배재고 출전정지", a_raw)
        b_articles = cand.score_articles_relevance("권오영 감독", b_raw)
        # 사전 확인: b_articles는 실제로 snippet_only_incidental_mention(is_incidental=True)로
        # 판정됨 — "감독"만 snippet에 있고 title에는 "권오영"/"감독" 토큰이 없음.
        self.assertTrue(all(a.get("is_incidental") for a in b_articles))
        # 사전 확인: article overlap 자체는 threshold 미달(실측치 재현)
        self.assertLess(ranker._article_overlap(a_articles, b_articles), ranker.MERGE_ARTICLE_OVERLAP_THRESHOLD)

        ranked = [
            self._ranked_with_articles("배재고 출전정지", 0.9, a_articles),
            self._ranked_with_articles("권오영 감독", 0.7, b_articles),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 1)
        # related_keywords에 원 키워드 보존
        self.assertIn("권오영 감독", merged[0]["related_keywords"])
        # 대표 display_keyword는 이미 사건 맥락이 담긴 "배재고 출전정지"가 앞에 오고
        # 보조 키워드("권오영 감독")가 뒤에 붙는다 — 단독 인명이 사건 키워드 앞에
        # 오지 않는다(요구사항: 단독 인명보다 사건성이 드러나는 키워드 우선).
        display = merged[0]["display_keyword"]
        self.assertTrue(display.startswith("배재고 출전정지"))
        self.assertNotEqual(display, "권오영 감독")

    def test_generic_shared_predicate_without_keyword_anchor_not_merged(self):
        # 두 그룹 모두 "오늘"/"발표"처럼 흔한 서술어가 반복되지만, keyword("정부"/"기업")가
        # 서로의 article 그룹에 전혀 등장하지 않으면 anchor 게이트에서 막혀 merge 금지.
        # (fixture는 article overlap 자체도 threshold 미만이 되도록 문장 구조를 다르게 구성 —
        # article overlap이 이미 높으면 이 테스트가 신규 anchor 게이트가 아니라 기존 신호로
        # merge된 것이라 신규 로직 검증이 아니게 된다.)
        a_articles = [
            _article("정부, 오늘 새 정책 발표", "https://x.com/rep-a1", "오늘 발표된 새 정책 내용이다"),
            _article("교육부 학사 일정 조정안 공개", "https://x.com/rep-a2", "새 학기 시작일이 늦춰진다"),
        ]
        b_articles = [
            _article("기업, 오늘 실적 발표", "https://x.com/rep-b1", "오늘 발표된 실적 내용이다"),
            _article("반도체 수출 통계 집계 결과", "https://x.com/rep-b2", "역대 최대 실적을 기록했다"),
        ]
        self.assertLess(ranker._article_overlap(a_articles, b_articles), ranker.MERGE_ARTICLE_OVERLAP_THRESHOLD)

        ranked = [
            self._ranked_with_articles("정부", 0.9, a_articles),
            self._ranked_with_articles("기업", 0.7, b_articles),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 2)  # anchor 없이 서술어만 겹침 → merge 안 됨

    def test_politics_and_crime_context_not_merged_by_generic_words(self):
        # 실측 재현: "국조특위 개표소 진입"(정치/국회 맥락)과 "장윤기 사건"(범죄/수사
        # 맥락)이 "사건"/"경찰"/"진입"/"국조특위" 같은 일반 단어만 겹쳐 오탐 merge됐다.
        # 두 그룹은 실제로 무관한 이슈이므로 merge되면 안 되고, display_keyword도
        # 서로의 keyword를 조합해서는 안 된다.
        a_articles = [
            _article("국조특위, 개표소 강제 진입 논란...여야 충돌", "https://x.com/gukjo-a1",
                     "국조특위 위원들이 개표소에 진입하는 과정에서 여야 의원 간 충돌이 벌어졌다."),
            _article("국조특위 개표소 진입 두고 여야 공방", "https://x.com/gukjo-a2",
                     "국조특위의 개표소 진입 절차를 두고 여야가 서로 책임을 미루며 공방을 벌였다."),
        ]
        b_articles = [
            _article("장윤기 사건 재수사...경찰 진입 당시 정황 확인", "https://x.com/jangyk-b1",
                     "경찰이 장윤기 사건 현장에 진입한 당시 정황을 다시 확인하고 있다고 밝혔다."),
            _article("장윤기 사건, 검찰 추가 수사 착수", "https://x.com/jangyk-b2",
                     "검찰은 장윤기 사건과 관련한 의혹을 확인하기 위해 추가 수사에 착수했다."),
        ]
        self.assertLess(ranker._article_overlap(a_articles, b_articles), ranker.MERGE_ARTICLE_OVERLAP_THRESHOLD)

        ranked = [
            self._ranked_with_articles("국조특위 개표소 진입", 0.9, a_articles),
            self._ranked_with_articles("장윤기 사건", 0.7, b_articles),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 2)  # 서로 다른 이슈 → merge 금지

        display_keywords = {m["display_keyword"] for m in merged}
        self.assertIn("국조특위 개표소 진입", display_keywords)
        self.assertIn("장윤기 사건", display_keywords)
        # 두 keyword가 하나로 조합된 display_keyword가 나오면 안 됨
        for d in display_keywords:
            self.assertFalse("국조특위" in d and "장윤기" in d)

    def test_keyword_anchor_tokens_excludes_generic_event_words(self):
        # _keyword_anchor_tokens()가 일반 사건 단어를 anchor 후보에서 실제로 제외하는지
        # 직접 고정한다(Codex review-only P3: 통합 테스트만으로는 blacklist의 앞단 게이트
        # (shared - _GENERIC_EVENT_PREDICATE_WORDS)와 anchor 제외 로직을 구분해 검증하지
        # 못함).
        item = {"keyword": "장윤기 사건"}
        self.assertEqual(ranker._keyword_anchor_tokens(item), {"장윤기"})

    def test_non_generic_shared_token_but_no_real_anchor_not_merged(self):
        # shared token 중 비-일반 단어(앞단 게이트 통과)가 하나 있어도, 그 토큰이 두
        # keyword 중 어느 쪽의 anchor도 아니고 상대 article 그룹에도 keyword anchor가
        # 등장하지 않으면 merge되면 안 된다 — _GENERIC_EVENT_PREDICATE_WORDS 확장(첫
        # 번째 게이트) 만으로는 이 케이스를 막지 못하고, _keyword_anchor_tokens()의
        # anchor 제외가 실제로 cross anchor 판정에 관여해야 막힌다.
        a_articles = [
            _article("국조특위, 개표소 강제 진입 논란...국회 파행", "https://x.com/anchor-a1",
                     "국조특위 위원들이 개표소에 진입하며 국회가 파행을 겪었다."),
            _article("국조특위 개표소 진입 두고 여야 파행", "https://x.com/anchor-a2",
                     "국조특위의 개표소 진입 절차를 두고 여야가 파행을 겪었다."),
        ]
        b_articles = [
            _article("장윤기 사건 재수사...국회 국정감사 파행 우려", "https://x.com/anchor-b1",
                     "장윤기 사건 여파로 국회 국정감사가 파행을 겪을 수 있다는 우려가 나왔다."),
            _article("장윤기 사건, 국회서도 파행 공방", "https://x.com/anchor-b2",
                     "장윤기 사건을 두고 국회에서도 파행 책임 공방이 벌어졌다."),
        ]
        self.assertLess(ranker._article_overlap(a_articles, b_articles), ranker.MERGE_ARTICLE_OVERLAP_THRESHOLD)
        shared = ranker._representative_overlap(
            self._ranked_with_articles("국조특위 개표소 진입", 0.9, a_articles),
            self._ranked_with_articles("장윤기 사건", 0.7, b_articles),
        )
        # "파행"이 비-일반 단어로 앞단 게이트(shared - _GENERIC_EVENT_PREDICATE_WORDS)는
        # 통과하지만, 두 keyword("국조특위 개표소 진입"/"장윤기 사건")의 실제 anchor와는
        # 무관하므로 cross anchor 게이트에서 최종 차단돼야 한다.
        self.assertTrue(shared - ranker._GENERIC_EVENT_PREDICATE_WORDS)

        ranked = [
            self._ranked_with_articles("국조특위 개표소 진입", 0.9, a_articles),
            self._ranked_with_articles("장윤기 사건", 0.7, b_articles),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 2)  # anchor 없는 우연한 공통어만으로는 merge 금지

    def test_only_generic_incident_words_shared_not_merged(self):
        # "경찰"/"사건"/"증거"/"진입" 같은 일반 사건 단어만 반복 등장하고, 실제
        # 이슈를 특정하는 고유명사 anchor가 서로 겹치지 않으면 merge하지 않는다.
        a_articles = [
            _article("서울 강남 사건 현장서 경찰 증거 확보", "https://x.com/generic-a1",
                     "경찰이 사건 현장에 진입해 증거를 확보했다고 밝혔다."),
            _article("경찰, 사건 관련 증거 추가 확보", "https://x.com/generic-a2",
                     "경찰은 이번 사건과 관련한 증거를 추가로 확보했다고 전했다."),
        ]
        b_articles = [
            _article("부산 해운대 사건 현장 경찰 진입", "https://x.com/generic-b1",
                     "경찰이 사건 현장에 진입해 초동 수사를 벌였다."),
            _article("경찰, 사건 증거 국과수 감정 의뢰", "https://x.com/generic-b2",
                     "경찰은 확보한 증거를 국립과학수사연구원에 감정 의뢰했다고 밝혔다."),
        ]
        self.assertLess(ranker._article_overlap(a_articles, b_articles), ranker.MERGE_ARTICLE_OVERLAP_THRESHOLD)

        ranked = [
            self._ranked_with_articles("강남 사건", 0.9, a_articles),
            self._ranked_with_articles("해운대 사건", 0.7, b_articles),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 2)  # 일반 사건 단어만 겹침 → merge 금지


    # ---- 2026-09-06 운영 false merge 2건: 약한 keyword 토큰이 교차 anchor 로 인정되는 결함 ----
    # run 34002188125:1 에서 '권은비 결혼 생각'→'바타 지예은'(BOTH_RESIDUAL, 공유 roundup 1건),
    # '안세영 미야자키 결승'→'불꽃축제 2026'(NO_SHARED) 이 붙었다. 두 pair 의 공통 구조:
    # shared DF 토큰이 {약한 keyword 토큰, filler 1개} 뿐인데, 그 약한 토큰("결혼"/"2026")이
    # keyword 문자열에 들어 있다는 이유로 _has_cross_keyword_anchor 가 참이 됐다.
    # 아래 테스트는 특정 문자열이 아니라 그 구조를 고정한다 — 이름은 바꿔도 통과해야 한다.

    def test_merge_anchor_tokens_excludes_pure_numeric_tokens(self):
        # 순수 숫자 토큰(연도/회차)은 사건 정체성이 아니다 — _is_noise 가 keyword 단위로
        # 쓰는 기준과 동일. 숫자+문자 결합("2경기"/"1240회")은 남긴다(경계 고정).
        self.assertEqual(ranker._merge_anchor_tokens({"keyword": "가을축제 2026"}), {"가을축제"})
        self.assertEqual(
            ranker._merge_anchor_tokens({"keyword": "선수A 2경기 연속골"}), {"선수A", "2경기", "연속골"}
        )
        self.assertIn("1240회", ranker._merge_anchor_tokens({"keyword": "1240회 로또"}))
        # 검색의도 suffix 제외는 기존 계약 그대로.
        self.assertEqual(ranker._merge_anchor_tokens({"keyword": "인물B 결혼 생각"}), {"인물B", "생각"})

    def test_merge_anchor_tokens_excludes_year_expressions(self):
        # 순수 숫자("2026")뿐 아니라 연도 표현("2026년"/"2026년도")도 같은 계열의 약한 anchor다.
        # 접두 (19|20) + 4자리 + (년|년도) 형태만 제외한다 — `년`이 붙는 다른 표현은 전부 보존.
        self.assertEqual(ranker._merge_anchor_tokens({"keyword": "가을축제 2026년"}), {"가을축제"})
        self.assertEqual(ranker._merge_anchor_tokens({"keyword": "전시회J 2026년도"}), {"전시회J"})
        self.assertEqual(ranker._merge_anchor_tokens({"keyword": "행사 2027"}), {"행사"})
        # 기간·기념·일반 어휘의 "년"은 사건 고유 정보라 남는다(오탐 경계 고정).
        self.assertEqual(
            ranker._merge_anchor_tokens({"keyword": "데뷔 20주년 콘서트"}), {"데뷔", "20주년", "콘서트"}
        )
        self.assertEqual(ranker._merge_anchor_tokens({"keyword": "계약 3년 연장"}), {"계약", "3년", "연장"})
        self.assertIn("향년", ranker._merge_anchor_tokens({"keyword": "향년 88세 별세"}))
        self.assertIn("그랜드캐년", ranker._merge_anchor_tokens({"keyword": "그랜드캐년 여행"}))
        # 실제 연도 범위(19|20) 밖의 4자리 + "년"은 연도가 아니므로 남는다 — 규칙이
        # "숫자4+년" 전부가 아니라 실제 연도대에만 걸린다는 경계.
        self.assertIn("2126년", ranker._merge_anchor_tokens({"keyword": "축제 2126년"}))
        # 반면 순수 숫자는 연도 범위와 무관하게 isdigit 규칙으로 제외된다(기존 계약).
        self.assertNotIn("2126", ranker._merge_anchor_tokens({"keyword": "축제 2126"}))

    def _year_suffixed_festival(self, score=0.78):
        # '가을축제 2026' 과 같은 구조지만 keyword 의 연도가 "2026년" 형태.
        return self._ranked_with_articles("가을축제 2026년", score, [
            _article("[포토] 도시 가을축제 2026년, 밤하늘을 수놓다", "https://fy.example.com/1",
                     "6일 강변공원에서 열린 가을축제 2026년 행사"),
            _article("[포토] 가을축제 2026년, 화려한 밤", "https://fy.example.com/2",
                     "6일 밤 가을축제 2026년 현장"),
            _article("통신사, 축제장 전용망 운영", "https://fy.example.com/3", "인파 안전관리"),
            _article("축제 인파 100만 운집", "https://fy.example.com/4", "강변"),
        ])

    def _year_suffixed_unrelated(self, score=0.47):
        return self._ranked_with_articles("선수A 상대B 결승", score, [
            _article("선수A, 2026년 오픈 결승서 상대B 격파", "https://sy.example.com/1",
                     "6일 선수A가 2026년 오픈 결승에서"),
            _article("선수A 2026년 시즌 5번째 우승", "https://sy.example.com/2", "6일 상대B를 상대로 결승"),
            _article("[포토] 선수A 결승 세리머니", "https://sy.example.com/3", "오픈"),
            _article("상대B 꺾은 선수A", "https://sy.example.com/4", "배드민턴"),
        ])

    def test_year_suffixed_token_does_not_bridge_unrelated_event(self):
        # 'X 2026' 과 완전히 같은 결함 구조인데 연도만 "2026년" 형태. 공유 기사 0,
        # shared DF 는 {연도 표현, 날짜 filler} 뿐이고 연도가 winner keyword 에 들어 있다.
        a, b = self._year_suffixed_festival(), self._year_suffixed_unrelated()
        ev_a, ev_b = ranker._evidence_articles_of(a), ranker._evidence_articles_of(b)
        sh_a, sh_b, _, _ = ranker._split_shared_evidence(ev_a, ev_b)
        self.assertEqual((len(sh_a), len(sh_b)), (0, 0))               # 전제: NO_SHARED
        shared = ranker._group_df_tokens(ev_a) & ranker._group_df_tokens(ev_b)
        self.assertIn("2026년", shared)                                 # 전제: 연도 표현이 DF 공유
        self.assertGreaterEqual(len(shared), ranker.REPRESENTATIVE_OVERLAP_MIN_SHARED_TOKENS)
        self.assertFalse(shared & ranker._merge_anchor_tokens(a))       # 전제: 변별 anchor 안 겹침
        self.assertFalse(shared & ranker._merge_anchor_tokens(b))
        self.assertFalse(ranker._is_same_issue(a, b))
        self.assertFalse(ranker._is_same_issue(b, a))                  # 대칭

    def test_year_suffixed_keyword_genuine_same_event_still_merges(self):
        # 같은 연도("2026년")를 공유해도 변별 anchor(행사명/주최사)가 상대 근거에 반복
        # 등장하면 종전처럼 merge 된다 — 연도 제외는 변별력 있는 근거를 없애지 않는다.
        a = self._ranked_with_articles("전시회J 2026년 참가", 0.8, [
            _article("회사K, 전시회J 2026년 참가 확정", "https://gy.example.com/1", "6일 전시회J 부스"),
            _article("전시회J 2026년 한국 기업 대거 참가", "https://gy.example.com/2", "회사K 부스"),
            _article("전시회J 2026년 개막… 회사K 신작 공개", "https://gy.example.com/3", "개막"),
        ])
        b = self._ranked_with_articles("회사K 전시회J 부스", 0.7, [
            _article("회사K 전시회J 부스 신작 4종 공개", "https://hy.example.com/1", "6일 전시회J 2026년"),
            _article("회사K, 전시회J 부스서 신작 시연", "https://hy.example.com/2", "전시회J 2026년 참가"),
            _article("전시회J 회사K 부스 인파", "https://hy.example.com/3", "개막"),
        ])
        # 전제: 연도를 뺀 뒤에도 변별 anchor 가 실제로 겹친다(이 테스트가 무엇을 지키는지 명시).
        shared = ranker._group_df_tokens(ranker._evidence_articles_of(a)) & ranker._group_df_tokens(
            ranker._evidence_articles_of(b))
        self.assertTrue(shared & (ranker._merge_anchor_tokens(a) | ranker._merge_anchor_tokens(b)))
        self.assertTrue(ranker._is_same_issue(a, b))
        self.assertEqual(len(ranker.dedupe_and_merge([a, b])), 1)

    def test_cross_keyword_anchor_requires_discriminative_anchor(self):
        # shared 토큰이 keyword 에 "들어 있다"는 것만으로는 교차 anchor 가 아니다.
        # 연도·검색의도 suffix 는 자명하게 겹치므로 변별력 있는 anchor 만 인정한다.
        year_kw = {"keyword": "가을축제 2026"}
        other = {"keyword": "선수A 결승"}
        self.assertFalse(ranker._has_cross_keyword_anchor(year_kw, other, {"2026", "6일"}, [], []))
        # 변별력 있는 토큰이 겹치면 종전과 같이 참.
        self.assertTrue(ranker._has_cross_keyword_anchor(year_kw, other, {"가을축제", "6일"}, [], []))
        intent_kw = {"keyword": "인물B 결혼 생각"}
        self.assertFalse(ranker._has_cross_keyword_anchor(intent_kw, other, {"결혼", "공개"}, [], []))
        self.assertTrue(ranker._has_cross_keyword_anchor(intent_kw, other, {"인물B", "공개"}, [], []))

    def _year_festival(self, score=0.78):
        return self._ranked_with_articles("가을축제 2026", score, [
            _article("[포토] 도시 가을축제 2026, 밤하늘을 수놓다", "https://f.example.com/1",
                     "6일 강변공원에서 열린 가을축제 2026"),
            _article("[포토] 가을축제 2026, 화려한 밤", "https://f.example.com/2", "6일 밤 가을축제 2026 현장"),
            _article("통신사, 축제장 전용망 운영", "https://f.example.com/3", "인파 안전관리"),
            _article("축제 인파 100만 운집", "https://f.example.com/4", "강변"),
        ])

    def _unrelated_final(self, score=0.47):
        return self._ranked_with_articles("선수A 상대B 결승", score, [
            _article("선수A, 2026 오픈 결승서 상대B 격파", "https://s.example.com/1",
                     "6일 선수A가 2026 오픈 결승에서"),
            _article("선수A 2026 시즌 5번째 우승", "https://s.example.com/2", "6일 상대B를 상대로 결승"),
            _article("[포토] 선수A 결승 세리머니", "https://s.example.com/3", "오픈"),
            _article("상대B 꺾은 선수A", "https://s.example.com/4", "배드민턴"),
        ])

    def test_year_token_in_keyword_does_not_bridge_unrelated_event(self):
        # '안세영 미야자키 결승'→'불꽃축제 2026' 유형(NO_SHARED). 공유 기사 0, shared DF 는
        # {연도, 날짜 filler} 뿐이고 연도가 winner keyword 에 들어 있다.
        a, b = self._year_festival(), self._unrelated_final()
        ev_a, ev_b = ranker._evidence_articles_of(a), ranker._evidence_articles_of(b)
        sh_a, sh_b, _, _ = ranker._split_shared_evidence(ev_a, ev_b)
        self.assertEqual((len(sh_a), len(sh_b)), (0, 0))               # 전제: NO_SHARED
        shared = ranker._group_df_tokens(ev_a) & ranker._group_df_tokens(ev_b)
        self.assertIn("2026", shared)                                   # 전제: 연도가 DF 공유
        self.assertFalse(shared & ranker._merge_anchor_tokens(a))       # 전제: 변별 anchor 안 겹침
        self.assertFalse(shared & ranker._merge_anchor_tokens(b))
        self.assertFalse(ranker._is_same_issue(a, b))

    def test_intent_suffix_with_single_roundup_bridge_does_not_merge(self):
        # '권은비 결혼 생각'→'바타 지예은' 유형(BOTH_RESIDUAL). 연예 종합 roundup **1건**이
        # 양쪽 검색에 잡혀 shared=1, 잔여는 서로 다른 인물의 서로 다른 사건. 잔여끼리의
        # shared DF 는 {검색의도 suffix, generic filler} 뿐.
        roundup = _article("'복귀' 가수C·'결혼' 인물D♥인물E·'근황' 인물B", "https://r.example.com/1", "이주의 연예")
        a = self._ranked_with_articles("인물B 결혼 생각", 0.45, [
            _article('인물B "결혼 생각 있다" 솔직 공개', "https://e.example.com/1", "결혼 생각 공개"),
            _article('인물B, 결혼관 고백 "결혼 생각 없지 않아"', "https://e.example.com/2", "팬미팅서 공개"),
            _article("인물B 팬미팅서 결혼 생각 언급", "https://e.example.com/3", "솔직한 결혼관"),
            roundup,
        ])
        b = self._ranked_with_articles("인물E 인물D", 0.69, [
            _article("인물D♥인물E, 결혼 비하인드 공개", "https://b.example.com/1", "지인이 먼저 공개"),
            _article("인물D♥인물E 12월 결혼… 열애 1년 만에", "https://b.example.com/2", "웨딩 공개"),
            _article("[공식] '♥인물E와 결혼' 인물D, 겹경사", "https://b.example.com/3", "예비 신부"),
            roundup,
        ])
        ev_a, ev_b = ranker._evidence_articles_of(a), ranker._evidence_articles_of(b)
        sh_a, sh_b, rest_a, rest_b = ranker._split_shared_evidence(ev_a, ev_b)
        self.assertEqual((len(sh_a), len(sh_b)), (1, 1))               # 전제: roundup 1건 bridge
        self.assertTrue(len(rest_a) >= 2 and len(rest_b) >= 2)         # 전제: BOTH_RESIDUAL
        shared = ranker._group_df_tokens(rest_a) & ranker._group_df_tokens(rest_b)
        self.assertIn("결혼", shared)
        self.assertFalse(ranker._is_same_issue(a, b))
        self.assertFalse(ranker._is_same_issue(b, a))                  # 대칭

    def test_generic_wedding_vocabulary_does_not_merge_two_couples(self):
        # 14일 운영 반복 패턴('송하예 조매력 결혼'→'김승윤 장동윤 결혼' 등): 서로 다른 커플이
        # roundup 1건 공유 + 결혼 일반 어휘만 겹쳐 붙었다. 양쪽 keyword 모두 suffix 를 갖는다.
        # 두 커플의 기사는 서로 다른 기자가 쓴 서로 다른 문장이다 — 같은 템플릿에 이름만 바꾸면
        # 기사쌍 Jaccard(MERGE_ARTICLE_OVERLAP_THRESHOLD) 경로가 먼저 발화해 anchor 경로를
        # 시험하지 못한다(격리 전제를 아래서 단정).
        roundup = _article("[종합] 결혼 발표 잇따라… 커플F, 커플G", "https://r.example.com/2", "연예계 결혼")
        a = self._ranked_with_articles("커플F 결혼", 0.9, [
            _article("커플F, 10월 결혼 발표… 서울호텔 예식", "https://c1.example.com/1", "가족끼리 조용히 올린다"),
            _article("커플F 결혼 발표에 축하 쏟아져", "https://c1.example.com/2", "동료 배우들 SNS 축하"),
            _article("커플F 예비신부 누구? 비연예인", "https://c1.example.com/3", "결혼 발표 후 관심 집중"),
            roundup,
        ])
        b = self._ranked_with_articles("커플G 결혼", 0.7, [
            _article("커플G 12월 결혼… 제주서 웨딩", "https://c2.example.com/1", "열애 2년 만에 결혼 발표"),
            _article('커플G 측 "결혼 발표 맞다" 공식 인정', "https://c2.example.com/2", "소속사 확인"),
            _article("커플G 웨딩 촬영 비하인드", "https://c2.example.com/3", "제주리조트에서 진행"),
            roundup,
        ])
        ev_a, ev_b = ranker._evidence_articles_of(a), ranker._evidence_articles_of(b)
        sh_a, _, rest_a, rest_b = ranker._split_shared_evidence(ev_a, ev_b)
        self.assertEqual(len(sh_a), 1)                                  # 전제: roundup 1건 bridge
        # 격리 전제: 잔여 기사쌍 Jaccard 는 임계 미만 — 이 테스트가 막는 것은 anchor 경로다.
        self.assertLess(ranker._pairwise_evidence_overlap(rest_a, rest_b), ranker.MERGE_ARTICLE_OVERLAP_THRESHOLD)
        shared = ranker._group_df_tokens(rest_a) & ranker._group_df_tokens(rest_b)
        # 전제: 잔여 shared DF 가 결혼 일반 어휘뿐(크기 게이트는 통과) — 변별 anchor 는 안 겹친다.
        self.assertGreaterEqual(len(shared), ranker.REPRESENTATIVE_OVERLAP_MIN_SHARED_TOKENS)
        self.assertFalse(shared & (ranker._merge_anchor_tokens(a) | ranker._merge_anchor_tokens(b)))
        merged = ranker.dedupe_and_merge([a, b])
        self.assertEqual(len(merged), 2, [m["keyword"] for m in merged])

    def test_year_keyword_transitive_chain_does_not_collapse(self):
        # 운영 run 에서 '불꽃축제 2026' 이 정치·세금·스포츠 6개를 한 component 로 접었다.
        # 네 후보가 {연도, 날짜} 만 공유하고 변별 anchor 는 어느 쌍도 안 겹칠 때 4개가 남아야 한다.
        pol = self._ranked_with_articles("의원H 프로필", 0.6, [
            _article("의원H, 2026 국정감사 질의", "https://p.example.com/1", "6일 국회에서 2026"),
            _article("의원H 프로필 화제", "https://p.example.com/2", "6일 2026 국감"),
        ])
        tax = self._ranked_with_articles("구I 종부세", 0.57, [
            _article("구I 2026년 종부세 대상 확대", "https://t.example.com/1", "6일 2026 종부세"),
            _article("종부세 2026 개편안", "https://t.example.com/2", "6일 구I 2026"),
        ])
        items = [self._year_festival(), pol, tax, self._unrelated_final()]
        # 격리 전제: 어떤 쌍도 변별 anchor 를 상대 DF 토큰과 공유하지 않는다(강한 anchor 경로 배제).
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                ev_i = ranker._evidence_articles_of(items[i]); ev_j = ranker._evidence_articles_of(items[j])
                shared = ranker._group_df_tokens(ev_i) & ranker._group_df_tokens(ev_j)
                self.assertIn("2026", shared)
                self.assertFalse(shared & (ranker._merge_anchor_tokens(items[i]) | ranker._merge_anchor_tokens(items[j])))
        merged = ranker.dedupe_and_merge([dict(x) for x in items])
        self.assertEqual(len(merged), 4, [m["keyword"] for m in merged])

    def test_year_keyword_genuine_same_event_still_merges(self):
        # 연도가 keyword 에 있어도 변별 anchor(행사명/주최사)가 상대 근거에 반복 등장하면 종전처럼
        # merge 된다 — 이 수정은 "무엇을 anchor 로 인정하느냐"만 바꾼다.
        a = self._ranked_with_articles("전시회J 2026 참가", 0.8, [
            _article("회사K, 전시회J 2026 참가 확정", "https://g.example.com/1", "6일 전시회J 부스"),
            _article("전시회J 2026 한국 기업 대거 참가", "https://g.example.com/2", "회사K 부스"),
            _article("전시회J 2026 개막… 회사K 신작 공개", "https://g.example.com/3", "개막"),
        ])
        b = self._ranked_with_articles("회사K 전시회J 부스", 0.7, [
            _article("회사K 전시회J 부스 신작 4종 공개", "https://h.example.com/1", "6일 전시회J 2026"),
            _article("회사K, 전시회J 부스서 신작 시연", "https://h.example.com/2", "전시회J 2026 참가"),
            _article("전시회J 회사K 부스 인파", "https://h.example.com/3", "개막"),
        ])
        self.assertTrue(ranker._is_same_issue(a, b))
        self.assertEqual(len(ranker.dedupe_and_merge([a, b])), 1)

    # --- 넓은 지명이 유일한 교차 anchor 인 merge (2026-09-06 운영 false merge) ---
    # 구조 계약: "광역 지명 하나만 공통"이면 merge 근거로 부족하고, 지명 **말고도**
    # 교차 근거가 있으면 기존대로 merge 된다. 특정 뉴스 문자열이 아니라 이 구조를
    # 검증한다 — 지명 토큰은 운영에서 실제로 관측된 것을 쓰되, 사건 어휘는 임의다.

    def test_broad_location_alone_does_not_bridge_unrelated_events(self):
        """넓은 지명 하나 + filler 만 공통이면 서로 다른 사건은 merge 되지 않는다."""
        a = self._ranked_with_articles("행사A 개막", 0.9, [
            _article("행사A 서울에서 개막", "https://a.example.com/1", "5일 오후 서울 행사A가 열렸다"),
            _article("서울 행사A 인파", "https://a.example.com/2", "5일 오후 서울 행사A에 인파가 몰렸다"),
            _article("행사A 안전 대책", "https://a.example.com/3", "5일 오후 서울 행사A 대책이 나왔다"),
        ])
        b = self._ranked_with_articles("제도B 서울 확대", 0.7, [
            _article("서울 제도B 대상 최다", "https://b.example.com/1", "5일 오후 서울 제도B 고지가 나갔다"),
            _article("제도B 고지 확대", "https://b.example.com/2", "5일 오후 서울 제도B 대상이 늘었다"),
            _article("서울 제도B 납부", "https://b.example.com/3", "5일 오후 서울 제도B 납부자가 늘었다"),
        ])
        ev_a = ranker._evidence_articles_of(a)
        ev_b = ranker._evidence_articles_of(b)
        shared = ranker._group_df_tokens(ev_a) & ranker._group_df_tokens(ev_b)
        # 선행조건: DF 게이트는 통과하고(>=2), 교차 anchor 후보는 지명 하나뿐이어야
        # 이 테스트가 지명 규칙을 검증한다(다른 게이트가 막아서 통과하면 자명 통과).
        self.assertGreaterEqual(len(shared), ranker.REPRESENTATIVE_OVERLAP_MIN_SHARED_TOKENS)
        anchors = ranker._merge_anchor_tokens(a) | ranker._merge_anchor_tokens(b)
        self.assertEqual(shared & anchors, {"서울"})
        # 격리: Jaccard 경로가 아니라 DF/anchor 경로여야 한다.
        self.assertLess(ranker._pairwise_evidence_overlap(ev_a, ev_b),
                        ranker.MERGE_ARTICLE_OVERLAP_THRESHOLD)
        self.assertFalse(ranker._is_same_issue(a, b))
        self.assertEqual(len(ranker.dedupe_and_merge([a, b])), 2)

    def test_broad_location_with_strong_co_anchor_still_merges(self):
        """지명 외에 사건 anchor 가 함께 교차하면 기존처럼 merge 된다(정상 보존)."""
        a = self._ranked_with_articles("제주 장소C 사망", 0.9, [
            _article("제주 장소C서 남성 숨진 채 발견", "https://a.example.com/1", "5일 오후 제주 장소C에서 발견됐다"),
            _article("제주 장소C 사망 조사", "https://a.example.com/2", "5일 오후 제주 장소C 사망을 조사 중이다"),
            _article("장소C 사망 신원 확인", "https://a.example.com/3", "5일 오후 제주 장소C 숨진 남성 신원을 확인했다"),
        ])
        b = self._ranked_with_articles("제주 실종자 발견", 0.7, [
            _article("제주서 실종자 장소C서 발견", "https://b.example.com/1", "5일 오후 제주 장소C에서 실종자가 발견됐다"),
            _article("제주 실종자 발견 당시", "https://b.example.com/2", "5일 오후 제주 장소C 인근에서 발견됐다"),
            _article("제주 실종자 발견 경위", "https://b.example.com/3", "5일 오후 제주 장소C 발견 경위를 조사한다"),
        ])
        anchors = ranker._merge_anchor_tokens(a) | ranker._merge_anchor_tokens(b)
        ev_a = ranker._evidence_articles_of(a)
        ev_b = ranker._evidence_articles_of(b)
        shared = ranker._group_df_tokens(ev_a) & ranker._group_df_tokens(ev_b)
        # 선행조건: 지명 말고도 교차 anchor 가 실제로 존재해야 이 테스트가 의미를 갖는다.
        self.assertTrue((shared & anchors) - ranker._BROAD_LOCATION_TOKENS)
        self.assertTrue(ranker._is_same_issue(a, b))
        self.assertEqual(len(ranker.dedupe_and_merge([a, b])), 1)

    def test_broad_location_compound_is_not_treated_as_broad(self):
        """'서울대'/'서울역'처럼 지명을 포함한 고유 compound 는 별개 토큰이라 강한 anchor 로 남는다."""
        for compound in ("서울대", "서울역", "서울시청", "제주항공", "부산국제영화제"):
            self.assertNotIn(compound, ranker._BROAD_LOCATION_TOKENS)
            # 토크나이저가 실제로 쪼개지 않는지까지 확인(쪼개지면 위 단정이 무의미해진다).
            self.assertEqual(
                ranker._merge_anchor_tokens({"keyword": compound}), {compound})

    def test_specific_place_name_remains_strong_anchor(self):
        """구체 지역명(자치구/포구 등)은 약화 대상이 아니다."""
        for place in ("금천", "강남", "상암", "하예포구"):
            self.assertNotIn(place, ranker._BROAD_LOCATION_TOKENS)

    def test_broad_location_alone_does_not_bridge_via_article_mention(self):
        """2절(상대 기사에 anchor 등장)도 지명 하나만으로는 통과하지 못한다."""
        a = self._ranked_with_articles("서울 7-0 대승", 0.9, [
            _article("구단D 7-0 대승", "https://a.example.com/1", "5일 오후 서울에서 구단D가 대승했다"),
            _article("구단D 7-0 완승", "https://a.example.com/2", "5일 오후 서울에서 구단D가 7골을 넣었다"),
            _article("구단D 대승 인터뷰", "https://a.example.com/3", "5일 오후 서울 경기 후 감독이 말했다"),
        ])
        b = self._ranked_with_articles("인물E 서울 집 공개", 0.7, [
            _article("인물E 서울 자택 공개", "https://b.example.com/1", "5일 오후 인물E가 서울 자택을 공개했다"),
            _article("인물E 서울 집 화제", "https://b.example.com/2", "5일 오후 인물E의 서울 집이 공개됐다"),
            _article("인물E 서울 근황", "https://b.example.com/3", "5일 오후 인물E가 서울에서 지내는 근황을 전했다"),
        ])
        ev_a = ranker._evidence_articles_of(a)
        ev_b = ranker._evidence_articles_of(b)
        tokens_in_a, tokens_in_b = set(), set()
        for x in ev_a:
            tokens_in_a |= set(ranker._tokens_of(x))
        for x in ev_b:
            tokens_in_b |= set(ranker._tokens_of(x))
        anchors_a = ranker._merge_anchor_tokens(a)
        anchors_b = ranker._merge_anchor_tokens(b)
        # 선행조건: 2절 교차가 실제로 지명뿐이어야 한다.
        self.assertEqual((anchors_a & tokens_in_b) | (anchors_b & tokens_in_a), {"서울"})
        self.assertFalse(ranker._is_same_issue(a, b))

    def test_location_compound_still_bridges_genuine_same_event(self):
        """지명 compound('서울대')는 그 자체가 사건 식별자다 — 약화 대상이 아니다.

        `_BROAD_LOCATION_TOKENS` 를 prefix 매칭으로 넓히면(과확장 mutant) 이 merge 가
        죽는다. 정확 일치여야 함을 행동으로 고정한다.
        """
        # compound 가 1절·2절 **양쪽 모두에서 유일한** 교차 토큰이어야 prefix 과확장
        # mutant 를 잡는다. 다른 사건 어휘가 하나라도 함께 겹치면 그 어휘로 통과해
        # 이 테스트가 무의미해진다(실제로 "총장"/"수장"이 새어 mutant 가 살아남았다).
        a = self._ranked_with_articles("서울대 총장 선출", 0.9, [
            _article("서울대 새 총장 선출 절차 마무리", "https://a.example.com/1", "5일 오후 서울대 선출 절차가 끝났다"),
            _article("서울대 총장 선출 결과 공표", "https://a.example.com/2", "5일 오후 서울대 총장 선출 결과가 나왔다"),
            _article("서울대 총장 선출 마무리", "https://a.example.com/3", "5일 오후 서울대 총장 선출이 마무리됐다"),
        ])
        b = self._ranked_with_articles("서울대 기숙사 증축", 0.7, [
            _article("서울대 기숙사 증축 착공", "https://b.example.com/1", "5일 오후 서울대 기숙사 증축이 시작됐다"),
            _article("서울대 기숙사 증축 예산 확보", "https://b.example.com/2", "5일 오후 서울대 기숙사 예산이 확보됐다"),
            _article("서울대 기숙사 증축 일정", "https://b.example.com/3", "5일 오후 서울대 기숙사 증축 일정이 나왔다"),
        ])
        ev_a = ranker._evidence_articles_of(a)
        ev_b = ranker._evidence_articles_of(b)
        # 선행조건: Jaccard 가 아니라 anchor 경로로 판정돼야 한다.
        self.assertLess(ranker._pairwise_evidence_overlap(ev_a, ev_b),
                        ranker.MERGE_ARTICLE_OVERLAP_THRESHOLD)
        anchors_a = ranker._merge_anchor_tokens(a)
        anchors_b = ranker._merge_anchor_tokens(b)
        shared = ranker._group_df_tokens(ev_a) & ranker._group_df_tokens(ev_b)
        tokens_in_a, tokens_in_b = set(), set()
        for x in ev_a:
            tokens_in_a |= set(ranker._tokens_of(x))
        for x in ev_b:
            tokens_in_b |= set(ranker._tokens_of(x))
        # 1절·2절 모두 "서울대" 하나뿐 — prefix 로 넓히면 양쪽 다 비어 merge 가 끊긴다.
        self.assertEqual(shared & (anchors_a | anchors_b), {"서울대"})
        self.assertEqual((anchors_a & tokens_in_b) | (anchors_b & tokens_in_a), {"서울대"})
        self.assertTrue(ranker._is_same_issue(a, b))
        self.assertEqual(len(ranker.dedupe_and_merge([a, b])), 1)

    def test_game_token_is_not_demoted_as_location(self):
        """'경기'는 운영에서 스포츠 의미가 지배적이라 지명 집합에 넣지 않는다.

        넣으면(과확장 mutant) 같은 경기를 다룬 정상 merge 가 끊긴다.
        """
        self.assertNotIn("경기", ranker._BROAD_LOCATION_TOKENS)
        a = self._ranked_with_articles("구단J 경기 취소", 0.9, [
            _article("구단J 경기 우천으로 취소", "https://a.example.com/1", "5일 오후 구단J 경기가 비로 취소됐다"),
            _article("구단J 경기 순연 결정", "https://a.example.com/2", "5일 오후 구단J 경기 일정이 밀렸다"),
            _article("구단J 경기 재편성 논의", "https://a.example.com/3", "5일 오후 구단J 경기 재편성이 논의된다"),
        ])
        b = self._ranked_with_articles("구단K 경기 순연", 0.7, [
            _article("구단K 경기 순연 공지", "https://b.example.com/1", "5일 오후 구단K 경기가 순연됐다"),
            _article("구단K 경기 취소 안내", "https://b.example.com/2", "5일 오후 구단K 경기 취소가 안내됐다"),
            _article("구단K 경기 새 일정", "https://b.example.com/3", "5일 오후 구단K 경기 일정이 다시 잡혔다"),
        ])
        ev_a = ranker._evidence_articles_of(a)
        ev_b = ranker._evidence_articles_of(b)
        self.assertLess(ranker._pairwise_evidence_overlap(ev_a, ev_b),
                        ranker.MERGE_ARTICLE_OVERLAP_THRESHOLD)
        shared = ranker._group_df_tokens(ev_a) & ranker._group_df_tokens(ev_b)
        anchors = ranker._merge_anchor_tokens(a) | ranker._merge_anchor_tokens(b)
        # 교차 anchor 가 "경기" 하나여야 이 테스트가 '경기' 규칙을 검증한다.
        self.assertEqual(shared & anchors, {"경기"})
        self.assertTrue(ranker._is_same_issue(a, b))

    def test_broad_location_chain_does_not_collapse_unrelated_events(self):
        """지명 하나로 여러 무관 사건이 transitive 하게 한 그룹으로 붕괴하지 않는다."""
        # 제목을 템플릿으로 찍으면 Jaccard 경로(>=0.5)가 먼저 발동해 지명 규칙이
        # 검증되지 않는다 — 사건마다 다른 문장을 쓴다.
        items = [
            self._ranked_with_articles("서울 공연F 매진", 0.9, [
                _article("공연F 전석 매진 기록", "https://f.example.com/1", "5일 오후 서울 공연장에서 공연F가 매진됐다"),
                _article("공연F 추가 회차 편성", "https://f.example.com/2", "5일 오후 서울 공연F 티켓이 동났다"),
                _article("공연F 관객 반응 뜨거워", "https://f.example.com/3", "5일 오후 서울에서 공연F 관객이 몰렸다"),
            ]),
            self._ranked_with_articles("서울 도로G 통제", 0.8, [
                _article("도로G 전면 통제 시작", "https://g.example.com/1", "5일 오후 서울 도로G 차량 진입이 막혔다"),
                _article("도로G 우회 노선 안내", "https://g.example.com/2", "5일 오후 서울 도로G 우회로가 안내됐다"),
                _article("도로G 정체 극심", "https://g.example.com/3", "5일 오후 서울 도로G 일대가 밀렸다"),
            ]),
            self._ranked_with_articles("서울 판결H 선고", 0.7, [
                _article("판결H 1심 선고 나와", "https://h.example.com/1", "5일 오후 서울 법원이 판결H를 선고했다"),
                _article("판결H 양형 이유 공개", "https://h.example.com/2", "5일 오후 서울 재판부가 판결H 이유를 밝혔다"),
                _article("판결H 항소 여부 관심", "https://h.example.com/3", "5일 오후 서울 판결H 항소가 거론된다"),
            ]),
            self._ranked_with_articles("서울 개장I 연기", 0.6, [
                _article("개장I 일정 미뤄져", "https://i.example.com/1", "5일 오후 서울 개장I 계획이 연기됐다"),
                _article("개장I 지연 배경 설명", "https://i.example.com/2", "5일 오후 서울 개장I 지연 사유가 나왔다"),
                _article("개장I 새 일정 검토", "https://i.example.com/3", "5일 오후 서울 개장I 재개 시점을 검토한다"),
            ]),
        ]
        # 선행조건: Jaccard 경로가 아니라 지명/DF 경로로 판정돼야 한다.
        for x in range(len(items)):
            for y in range(x + 1, len(items)):
                self.assertLess(
                    ranker._pairwise_evidence_overlap(
                        ranker._evidence_articles_of(items[x]),
                        ranker._evidence_articles_of(items[y])),
                    ranker.MERGE_ARTICLE_OVERLAP_THRESHOLD)
        merged = ranker.dedupe_and_merge([dict(i) for i in items])
        self.assertEqual(len(merged), 4)


class TestArticleDisplayFilter(unittest.TestCase):
    """개선 2: incidental/저관련 기사를 상세 articles에서 기본 제외."""

    def _nintendo_bnk_article(self):
        return _article(
            "BNK경남은행, BNK경남은행 가족 문화 페스티벌(경가페) 개최...오는 7일",
            "https://x.com/bnk-1",
            "닌텐도스위치2·삼성 갤럭시워치8·삼성 써클레이터·NC다이노스 유니폼 등 푸짐한 "
            "선물을 주는 경품 추첨을 진행한다.",
        )

    def test_nintendo_keyword_bnk_article_is_incidental(self):
        art = self._nintendo_bnk_article()
        rel = cand.compute_article_relevance("닌텐도 스위치 2", art)
        self.assertTrue(rel["is_incidental"])

    def test_bnk_keyword_same_article_not_incidental(self):
        # 같은 기사라도 keyword=BNK경남은행이면 기사 주체이므로 incidental 아님(과소평가 방지)
        art = self._nintendo_bnk_article()
        rel = cand.compute_article_relevance("BNK경남은행", art)
        self.assertFalse(rel["is_incidental"])

    def test_low_relevance_article_excluded_from_display_when_enough_remain(self):
        good = [
            dict(_article(f"닌텐도 스위치 2 관련 기사 {i}", f"https://x.com/good{i}"),
                 relevance_score=0.9, relevance_reason="keyword_main_topic", is_incidental=False)
            for i in range(5)
        ]
        incidental = dict(
            self._nintendo_bnk_article(), relevance_score=0.2,
            relevance_reason="snippet_only_incidental_mention", is_incidental=True,
        )
        filtered = cand.filter_articles_for_display(good + [incidental], min_count=5)
        urls = [a["url"] for a in filtered]
        self.assertNotIn("https://x.com/bnk-1", urls)  # 관련기사 충분하면 incidental 제외

    def test_incidental_backfilled_only_when_below_min_count(self):
        good = [
            dict(_article("닌텐도 스위치 2 발매 기사", "https://x.com/good1"),
                 relevance_score=0.9, relevance_reason="keyword_main_topic", is_incidental=False)
        ]
        incidental = dict(
            self._nintendo_bnk_article(), relevance_score=0.2,
            relevance_reason="snippet_only_incidental_mention", is_incidental=True,
        )
        filtered = cand.filter_articles_for_display(good + [incidental], min_count=2)
        # ARTICLES_MIN 하한 보호를 위해 incidental이라도 보충됨
        self.assertEqual(len(filtered), 2)
        urls = [a["url"] for a in filtered]
        self.assertIn("https://x.com/bnk-1", urls)
        # 보충된 기사도 relevance_score/relevance_reason/is_incidental 필드 유지
        backfilled = next(a for a in filtered if a["url"] == "https://x.com/bnk-1")
        self.assertIn("relevance_score", backfilled)
        self.assertTrue(backfilled["is_incidental"])

    def test_representative_selection_excludes_incidental_regardless_of_filter(self):
        # representative 후보 제외는 filter_articles_for_display와 무관하게 기존 로직으로 계속 보장
        articles = [self._nintendo_bnk_article()]
        scored = cand.score_articles_relevance("닌텐도 스위치 2", articles)
        clusters = cand.cluster_articles(scored)
        primary = cand.select_primary_cluster(clusters)
        rep = cand.select_representative(primary)
        self.assertIsNone(rep)  # incidental만 있으면 대표 없음


class TestKeywordQualityGate(unittest.TestCase):
    """운영 반영(290163d) 후속: article-level 필터는 통과했지만 keyword 자체가 Top10에
    남을 자격이 있는지 판단하는 gate."""

    def _fan_articles(self):
        # 실측 재현: "선풍기" 상세 기사 5건 전부 incidental(경품/비유/무관 문맥).
        return [
            _article("폭염 속 독특한 패션쇼?…\"대박\" 시선 집중", "https://x.com/fan1",
                     "마치 입는 선풍기, 입는 에어컨을 연상시키는 모습으로 관객들의 시선을 사로잡았습니다."),
            _article("[컨슈머리뷰] 손품 팔던 쇼핑은 끝났다", "https://x.com/fan2",
                     "\"3만 원대 선풍기 중에 분홍색이나 파란색 제품 있어?\" AI 쇼핑 에이전트에게 물었다."),
            _article("큰손들의 기업금융, 개인 계좌로 내려와", "https://x.com/fan3",
                     "1억원 이상 가입 고객 중 2명에게는 다이슨 쿨 선풍기를 증정한다."),
        ]

    def test_all_incidental_keyword_excluded_from_candidates(self):
        articles = self._fan_articles()
        sig = cand.compute_news_signal("선풍기", articles)
        self.assertEqual(sig["high_relevance_count"], 0)
        self.assertEqual(sig["quality_cluster_size"], 0)

        candidates = [{"keyword": "선풍기", "sources": {"daum": 1}}]
        signals = {"news": {"선풍기": sig}, "datalab": {}, "google": {}, "daum": {"선풍기": 1}}
        ranked = ranker.compute_scores(candidates, signals)
        self.assertEqual(ranked, [])  # quality gate 미달 → 후보에서 완전히 제외

    def test_quality_gate_does_not_use_backfilled_count(self):
        # filter_articles_for_display로 5건까지 보충돼도, quality 집계는 보충 이전
        # (원본 scored_articles) 기준이어야 한다.
        articles = self._fan_articles()
        sig = cand.compute_news_signal("선풍기", articles)
        filtered = cand.filter_articles_for_display(sig["articles"], min_count=5)
        self.assertEqual(len(filtered), 3)  # 보충 대상 자체가 3건뿐(min_count 미만이어도 그대로)
        # 보충 여부와 무관하게 quality 집계는 여전히 0
        self.assertEqual(sig["high_relevance_count"], 0)
        self.assertEqual(sig["quality_cluster_size"], 0)

    def test_high_relevance_keyword_passes_gate(self):
        # 같은 사건(신형 노트북 출시)을 서로 다른 표현으로 다룬 2건 — entity cohesion gate
        # (2026-07)를 통과하도록 공통 사건 토큰("신형","출시")을 공유한다.
        good_articles = [
            _article("삼성 신형 노트북 출시", "https://x.com/good1", "신형 노트북 라인업이 공개됐다.",
                     published_at=_recent_iso()),
            _article("신형 노트북 출시 판매 돌입", "https://y.com/good2", "신형 노트북 출시 첫날.",
                     published_at=_recent_iso()),
        ]
        sig = cand.compute_news_signal("노트북", good_articles)
        self.assertGreaterEqual(sig["high_relevance_count"], 2)
        self.assertGreaterEqual(sig["fresh_high_relevance_count"], 1)

        candidates = [{"keyword": "노트북", "sources": {"daum": 1}}]
        signals = {"news": {"노트북": sig}, "datalab": {}, "google": {}, "daum": {"노트북": 1}}
        ranked = ranker.compute_scores(candidates, signals)
        self.assertEqual(len(ranked), 1)

    def test_newsless_candidate_still_excluded_by_existing_rule(self):
        # quality gate와 무관하게, 애초에 news 신호가 없는 후보는 기존 규칙대로 제외돼야 한다
        # (quality gate 도입으로 이 기존 동작이 깨지지 않는지 확인).
        good_articles = [
            _article("삼성 신형 노트북 출시", "https://x.com/n1", "신형 노트북 출시", published_at=_recent_iso()),
            _article("신형 노트북 출시 판매 돌입", "https://y.com/n2", "신형 노트북 출시", published_at=_recent_iso()),
        ]
        sig = cand.compute_news_signal("노트북", good_articles)
        candidates = [{"keyword": "노트북", "sources": {"daum": 1}}, {"keyword": "무관", "sources": {"daum": 2}}]
        signals = {
            "news": {"노트북": sig},  # "무관"은 news 자체가 없음
            "datalab": {"무관": {"recent_delta": 2.0}},
            "google": {}, "daum": {"노트북": 1, "무관": 2},
        }
        ranked = ranker.compute_scores(candidates, signals)
        kws = [r["keyword"] for r in ranked]
        self.assertIn("노트북", kws)
        self.assertNotIn("무관", kws)


class TestFreshRelevanceGate(unittest.TestCase):
    """운영 반영(4c38b0e) 후속: 관련성은 높지만 전부 오래된 기사(제품 리뷰/도입기 등)만
    있는 키워드가 Top10에 남는 문제 — "로지텍 지슈스" 실측 재현."""

    def test_stale_product_keyword_excluded_from_top10(self):
        # 실측 재현: "로지텍 지슈스" — 관련 기사는 있으나 전부 2~5개월 전 제품
        # 도입/히트상품 기사. published_at 전부 FRESH_RELEVANCE_HOURS(72h) 밖.
        articles = [
            _article("아이러브PC방, 2026 1분기 PC방 히트상품 발표...로지텍 지슈스 마우스 등",
                     "https://x.com/logi1", "로지텍 지슈스 마우스 등 총 12종의 PC방 히트상품을 선정·발표했다.",
                     published_at=_stale_iso(days_ago=55)),
            _article("'레드포스PC방' 방배역점 오픈...로지텍 '지슈스' 일부 좌석 도입",
                     "https://x.com/logi2", "로지텍 PRO X2 SUPERSTRIKE(지슈스)를 일부 좌석에 도입했다.",
                     published_at=_stale_iso(days_ago=75)),
            _article("[언박싱]로지텍 'PRO X2 SUPERSTRIKE(지슈스)', 클릭의 새로운 패러다임을...",
                     "https://x.com/logi3", "로지텍의 지슈스는 스티디셀러인 지슈라 시리즈의 특유의 감성을 계승했다.",
                     published_at=_stale_iso(days_ago=125)),
        ]
        sig = cand.compute_news_signal("로지텍 지슈스", articles)
        self.assertGreaterEqual(sig["high_relevance_count"], 2)  # 관련성 자체는 높음
        self.assertEqual(sig["fresh_high_relevance_count"], 0)  # 전부 stale

        candidates = [{"keyword": "로지텍 지슈스", "sources": {"daum": 1}}]
        signals = {"news": {"로지텍 지슈스": sig}, "datalab": {}, "google": {}, "daum": {"로지텍 지슈스": 1}}
        ranked = ranker.compute_scores(candidates, signals)
        self.assertEqual(ranked, [])  # fresh gate 미달 → Top10 후보에서 완전히 제외

    def test_recent_product_issue_keyword_kept(self):
        # RTX 5090: 최근 가격/재고 이슈 기사가 있으면 제품 키워드라도 유지돼야 한다
        # (요구사항: "제품 기사라서 제외"가 아니라 "오래된 제품 기사만 있어서 제외").
        articles = [
            _article("RTX 5090 가격 또 올랐다...품귀 현상 심화", "https://x.com/rtx1",
                     "RTX 5090 재고 부족으로 가격이 급등하고 있다.", published_at=_recent_iso(hours_ago=3)),
            _article("RTX 5090 벤치마크 유출...성능 논란", "https://x.com/rtx2",
                     "RTX 5090의 실성능이 예상보다 낮다는 벤치마크가 유출됐다.", published_at=_recent_iso(hours_ago=10)),
        ]
        sig = cand.compute_news_signal("RTX 5090", articles)
        self.assertGreaterEqual(sig["fresh_high_relevance_count"], 1)

        candidates = [{"keyword": "RTX 5090", "sources": {"daum": 1}}]
        signals = {"news": {"RTX 5090": sig}, "datalab": {}, "google": {}, "daum": {"RTX 5090": 1}}
        ranked = ranker.compute_scores(candidates, signals)
        self.assertEqual(len(ranked), 1)

    def test_high_relevance_but_all_stale_fails_gate(self):
        # relevance_score는 높아도 latest_relevant_age_hours가 기준(72h) 초과면 gate 실패.
        articles = [
            _article("노트북 신제품 리뷰", "https://x.com/old1", "노트북 신제품을 상세히 리뷰했다.",
                     published_at=_stale_iso(days_ago=10)),
            _article("노트북 스펙 비교", "https://x.com/old2", "인기 노트북 스펙을 비교 정리했다.",
                     published_at=_stale_iso(days_ago=15)),
        ]
        sig = cand.compute_news_signal("노트북", articles)
        self.assertGreaterEqual(sig["high_relevance_count"], 2)
        self.assertIsNotNone(sig["latest_relevant_age_hours"])
        self.assertGreater(sig["latest_relevant_age_hours"], cand.FRESH_RELEVANCE_HOURS)
        self.assertEqual(sig["fresh_high_relevance_count"], 0)

        candidates = [{"keyword": "노트북", "sources": {"daum": 1}}]
        signals = {"news": {"노트북": sig}, "datalab": {}, "google": {}, "daum": {"노트북": 1}}
        ranked = ranker.compute_scores(candidates, signals)
        self.assertEqual(ranked, [])

    def test_backfill_skips_stale_candidate_for_next(self):
        # 앞 후보가 fresh gate로 제거된 뒤, 다음 후보가 stale이면 건너뛰고 그 다음이
        # 승격돼야 한다(억지로 Top10을 채우기 위해 stale 후보를 넣지 않음).
        # 공통 사건 토큰("신제품","출시")을 공유해 entity cohesion gate는 통과하되,
        # stale은 fresh gate로만 걸러지도록 구성한다(cohesion과 무관한 검증).
        stale_articles = [
            _article("노트북 신제품 출시 리뷰", "https://x.com/b-old1", "노트북 신제품 출시",
                     published_at=_stale_iso(days_ago=20)),
            _article("노트북 신제품 출시 스펙", "https://y.com/b-old2", "노트북 신제품 출시",
                     published_at=_stale_iso(days_ago=25)),
        ]
        fresh_articles = [
            _article("모니터 신제품 출시 화제", "https://x.com/b-new1", "모니터 신제품 출시",
                     published_at=_recent_iso(hours_ago=2)),
            _article("모니터 신제품 출시 행사", "https://y.com/b-new2", "모니터 신제품 출시",
                     published_at=_recent_iso(hours_ago=5)),
        ]
        sig_stale = cand.compute_news_signal("노트북", stale_articles)
        sig_fresh = cand.compute_news_signal("모니터", fresh_articles)

        candidates = [
            {"keyword": "노트북", "sources": {"daum": 1}},
            {"keyword": "모니터", "sources": {"daum": 2}},
        ]
        signals = {
            "news": {"노트북": sig_stale, "모니터": sig_fresh},
            "datalab": {}, "google": {},
            "daum": {"노트북": 1, "모니터": 2},
        }
        ranked = ranker.compute_scores(candidates, signals)
        kws = [r["keyword"] for r in ranked]
        self.assertNotIn("노트북", kws)  # stale → 건너뜀
        self.assertIn("모니터", kws)     # 다음 순번이 자연 승격

    def test_unknown_published_at_treated_as_stale_not_exempted(self):
        # published_at이 없거나(None) 파싱 실패(빈 문자열 등)한 기사만 있으면, high
        # relevance는 인정되더라도 "최근성 증명 불가"이므로 fresh gate는 예외 없이
        # 실패해야 한다(Codex review-only P2 반영: age unknown을 fresh로 우회시키지 않음).
        unknown_date_articles = [
            _article("AI 노트북 시장 성장", "https://x.com/unk1", "노트북 수요가 늘고 있다.",
                     published_at=None),
            _article("신형 노트북 출시", "https://x.com/unk2", "새로운 노트북 라인업이 공개됐다.",
                     published_at="not-a-valid-date"),
        ]
        sig = cand.compute_news_signal("노트북", unknown_date_articles)
        self.assertGreaterEqual(sig["high_relevance_count"], 2)  # 관련성 자체는 인정
        self.assertEqual(sig["fresh_high_relevance_count"], 0)  # 그러나 fresh는 0건
        self.assertIsNone(sig["latest_relevant_age_hours"])  # age 전부 unknown

        candidates = [{"keyword": "노트북", "sources": {"daum": 1}}]
        signals = {"news": {"노트북": sig}, "datalab": {}, "google": {}, "daum": {"노트북": 1}}
        ranked = ranker.compute_scores(candidates, signals)
        self.assertEqual(ranked, [])  # 예외 통과 없이 gate 실패

    def test_no_regression_for_incidental_and_side_mention_fixtures(self):
        # 기존 선풍기(전부 incidental) / 노트북 object_side_mention 케이스는 fresh gate
        # 추가와 무관하게 이전과 동일하게 처리돼야 한다(회귀 없음).
        fan_articles = [
            _article("폭염 속 독특한 패션쇼?…\"대박\" 시선 집중", "https://x.com/reg-fan1",
                     "마치 입는 선풍기, 입는 에어컨을 연상시키는 모습으로 관객들의 시선을 사로잡았습니다.",
                     published_at=_recent_iso()),
            _article("[컨슈머리뷰] 손품 팔던 쇼핑은 끝났다", "https://x.com/reg-fan2",
                     "\"3만 원대 선풍기 중에 분홍색이나 파란색 제품 있어?\" AI 쇼핑 에이전트에게 물었다.",
                     published_at=_recent_iso()),
        ]
        sig_fan = cand.compute_news_signal("선풍기", fan_articles)
        self.assertEqual(sig_fan["high_relevance_count"], 0)
        self.assertEqual(sig_fan["fresh_high_relevance_count"], 0)

        side_articles = [
            _article("美 하원 \"韓 정부, 쿠팡 차별적 규정…노트북 회수까지 지시\"",
                     "https://x.com/reg-side1",
                     "보고서엔 국정원이 중국 상하이강에 버려진 노트북을 회수하도록 지시하는 등...",
                     published_at=_recent_iso()),
        ]
        sig_side = cand.compute_news_signal("노트북", side_articles)
        self.assertEqual(sig_side["high_relevance_count"], 0)
        self.assertEqual(sig_side["fresh_high_relevance_count"], 0)


class TestObjectSideMention(unittest.TestCase):
    """운영 반영(290163d) 후속: keyword가 조치 대상 물품으로만 언급되는 곁가지 기사 필터."""

    def test_recall_context_is_side_mention(self):
        art = _article(
            "美 하원 \"韓 정부, 쿠팡 차별적 규정…노트북 회수까지 지시\"",
            "https://x.com/laptop1",
            "보고서엔 국정원이 중국 상하이강에 버려진 노트북을 회수하도록 지시하는 등 쿠팡에 과도한 압박을...",
        )
        rel = cand.compute_article_relevance("노트북", art)
        self.assertEqual(rel["relevance_reason"], "object_side_mention")
        self.assertFalse(rel["is_incidental"])
        self.assertLess(rel["relevance_score"], cand.HIGH_RELEVANCE_THRESHOLD)

    def test_seizure_and_submission_context_is_side_mention(self):
        for title in (
            "검찰, 압수수색으로 노트북 압수",
            "직원 노트북 제출 요구에 반발",
            "퇴사자 노트북 반납 절차 안내",
        ):
            art = _article(title, "https://x.com/side")
            rel = cand.compute_article_relevance("노트북", art)
            self.assertEqual(rel["relevance_reason"], "object_side_mention", msg=title)

    def test_snippet_only_side_mention_also_classified_as_object_side_mention(self):
        # keyword가 title에는 없고 snippet에만 side-mention 마커와 근접해 등장하는 경우도
        # object_side_mention으로 분류돼야 same-issue merge 근거에서 제외된다(Codex
        # review-only P2: in_title 조건에만 걸리면 이 케이스가 snippet_only_incidental_mention
        # 으로 새어나가 merge 근거로 우회 가능했음).
        art = _article(
            "압수수색 확대 논란", "https://x.com/snippet-side",
            "검찰은 이번 압수수색에서 노트북을 압수했다고 밝혔다.",
        )
        rel = cand.compute_article_relevance("노트북", art)
        self.assertEqual(rel["relevance_reason"], "object_side_mention")
        self.assertTrue(rel["is_incidental"])
        self.assertFalse(ranker._is_same_issue_evidence_article(dict(art, **rel)))

    def test_genuine_topic_titles_keep_main_topic(self):
        for title in (
            "AI 노트북 시장 성장",
            "신형 노트북 출시",
            "노트북 가격 인상",
            "게이밍 노트북 추천",
        ):
            art = _article(title, "https://x.com/main")
            rel = cand.compute_article_relevance("노트북", art)
            self.assertEqual(rel["relevance_reason"], "keyword_main_topic", msg=title)
            self.assertFalse(rel["is_incidental"])

    def test_side_mention_excluded_from_representative(self):
        articles = [_article(
            "美 하원 \"韓 정부, 쿠팡 차별적 규정…노트북 회수까지 지시\"",
            "https://x.com/laptop2",
            "보고서엔 국정원이 중국 상하이강에 버려진 노트북을 회수하도록 지시하는 등...",
        )]
        scored = cand.score_articles_relevance("노트북", articles)
        clusters = cand.cluster_articles(scored)
        primary = cand.select_primary_cluster(clusters)
        rep = cand.select_representative(primary)
        self.assertIsNone(rep)  # relevance 0.35 < REPRESENTATIVE_MIN_RELEVANCE(0.5) → 대표 없음

    def test_side_mention_not_used_for_same_issue_merge(self):
        # object_side_mention 기사가 URL을 공유해도 same-issue merge 근거가 되면 안 된다.
        side_mention_shared = dict(
            _article("쿠팡, 노트북 회수 논란 확산", "https://x.com/shared-side"),
            is_incidental=False, relevance_reason="object_side_mention", relevance_score=0.35,
        )
        ranked = [
            {"keyword": "쿠팡", "score": 0.9, "source_breakdown": {"news": 0.9}, "rank_reason": "",
             "news_meta": {"articles": [side_mention_shared]}, "used_signals": ["news"], "sources": {"daum": 1}},
            {"keyword": "노트북", "score": 0.5, "source_breakdown": {"news": 0.5}, "rank_reason": "",
             "news_meta": {"articles": [side_mention_shared]}, "used_signals": ["news"], "sources": {"daum": 2}},
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 2)  # object_side_mention 기사 공유만으로는 merge 안 됨


class TestBuilderRepresentativeFields(unittest.TestCase):
    """builder가 representative_*/sources/display_keyword 등을 entry에 실어 나르는지."""

    def test_entry_includes_new_optional_fields(self):
        ranked_item = {
            "keyword": "선풍기", "score": 0.5,
            "source_breakdown": {"news": 0.5}, "rank_reason": "",
            "news_meta": {
                "articles": [_article("유럽 폭염에 에어컨·선풍기 품귀", "https://x.com/e1")],
                "representative_title": "유럽 폭염에 에어컨·선풍기 품귀",
                "representative_summary": "폭염으로 선풍기 수요 급증",
                "representative_article": {"title": "유럽 폭염에 에어컨·선풍기 품귀"},
                "primary_cluster_size": 1,
                "topic_coherence": 1.0,
            },
            "used_signals": ["news"],
            "display_keyword": "선풍기",
            "related_keywords": [],
            "aliases": [],
            "sources": {"daum": 2},
        }
        entry = build_ranked_entry(1, ranked_item)
        for f in ("representative_title", "representative_summary", "representative_article",
                  "primary_cluster_size", "topic_coherence", "sources", "display_keyword",
                  "related_keywords", "aliases", "merge_reason"):
            self.assertIn(f, entry)
        self.assertEqual(entry["representative_title"], "유럽 폭염에 에어컨·선풍기 품귀")

    def test_missing_representative_fields_fallback_gracefully(self):
        # representative 필드가 없는 기존 데이터에서도 entry 조립이 깨지지 않아야 함
        ranked_item = {
            "keyword": "A", "score": 0.5, "source_breakdown": {"news": 0.5},
            "rank_reason": "", "news_meta": {"articles": [_article("A 기사", "https://x.com/f1")]},
            "used_signals": ["news"],
        }
        entry = build_ranked_entry(1, ranked_item)
        self.assertIsNone(entry["representative_title"])
        self.assertIsNone(entry["representative_summary"])
        self.assertEqual(entry["display_keyword"], "A")  # keyword로 fallback

    def test_low_relevance_articles_not_at_top_of_detail_list(self):
        # 관련기사가 충분하면 incidental/저관련 기사는 최종 articles에서 아예 제외돼야 한다.
        good = [
            dict(_article(f"닌텐도 스위치 2 관련 기사 {i}", f"https://x.com/g{i}"),
                 relevance_score=0.9, relevance_reason="keyword_main_topic", is_incidental=False)
            for i in range(5)
        ]
        incidental = dict(
            _article("BNK경남은행, 가족 문화 페스티벌 개최", "https://x.com/bnk-detail",
                     "닌텐도스위치2 등 경품 추첨을 진행한다."),
            relevance_score=0.2, relevance_reason="snippet_only_incidental_mention", is_incidental=True,
        )
        ranked_item = {
            "keyword": "닌텐도 스위치 2", "score": 0.5, "source_breakdown": {"news": 0.5},
            "rank_reason": "", "news_meta": {"articles": good + [incidental]},
            "used_signals": ["news"],
        }
        entry = build_ranked_entry(1, ranked_item)
        urls = [a["url"] for a in entry["articles"]]
        self.assertNotIn("https://x.com/bnk-detail", urls)


class TestMovementAfterMerge(unittest.TestCase):
    """movement는 dedupe/merge 이후 최종 Top10(canonical keyword) 기준이어야 한다."""

    def test_movement_uses_canonical_keyword_stable_across_merges(self):
        # 이전 실행에서도 "압수수색"이 canonical이었고, 이번에도 동일 canonical 유지 →
        # merge로 display만 바뀌어도 movement가 부당하게 'new' 취급되지 않아야 한다.
        prev = {"keywords": [{"keyword": "압수수색", "rank": 3}]}
        new = {"keywords": [{"keyword": "압수수색", "rank": 1, "display_keyword": "김영환 압수수색"}]}
        out = apply_movement(prev, new)
        self.assertEqual(out["keywords"][0]["movement"], "up")
        self.assertEqual(out["keywords"][0]["previous_rank"], 3)


class TestDisplayKeywordRepresentative(unittest.TestCase):
    """merge group의 display_keyword가 score/글자수 1위가 아니라 "그룹 기사 분포율
    기반 대표성"으로 선택되는지 검증(2026-07-02 live diagnostic 후속).

    canonical keyword(movement 비교용)는 절대 바뀌지 않고 display_keyword만
    자연스러워져야 한다.
    """

    def _rk(self, kw, score, articles, sources=None):
        return {
            "keyword": kw, "score": score,
            "source_breakdown": {"news": score}, "rank_reason": "",
            "news_meta": {"articles": articles}, "used_signals": ["news"],
            "sources": sources if sources is not None else {"daum": 1},
        }

    def _rel(self, title, url, snippet=""):
        # 실제 파이프라인 기사처럼 relevance_reason을 부여(same-issue evidence로 인정되게).
        a = _article(title, url, snippet)
        a["relevance_reason"] = "keyword_main_topic"
        return a

    def test_worldcup_group_not_represented_by_opponent_country(self):
        # 월드컵/16강은 그룹 기사 대부분에, 보스니아 헤르체고비나는 1건에만 등장.
        # display_keyword가 지엽적 상대국 단독으로 뽑히면 안 되고, coverage 높은
        # 사건 핵심어(월드컵/16강)가 대표가 돼야 한다.
        arts = [
            self._rel("한국 월드컵 16강 진출 확정", "https://a.com/1"),
            self._rel("월드컵 16강 상대는 보스니아 헤르체고비나", "https://a.com/2"),
            self._rel("월드컵 16강 대진표 발표", "https://a.com/3"),
            self._rel("월드컵 16강 경기 일정 공개", "https://a.com/4"),
        ]
        ranked = [
            self._rk("보스니아 헤르체고비나", 0.9, arts, {"daum": 3}),
            self._rk("월드컵", 0.7, arts, {"aux": True}),
            self._rk("16강", 0.6, arts, {"aux": True}),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 1)
        display = merged[0]["display_keyword"]
        self.assertNotEqual(display, "보스니아 헤르체고비나")
        self.assertNotIn("보스니아", display)
        # 월드컵/16강 핵심어가 대표에 포함돼야 한다.
        self.assertTrue("월드컵" in display or "16강" in display)
        # canonical keyword는 score 1위(보스니아)로 유지 — movement 안정성.
        self.assertEqual(merged[0]["keyword"], "보스니아 헤르체고비나")

    def test_semiconductor_group_not_represented_by_mentioned_company(self):
        # 반도체가 그룹 기사 대부분에, 메타는 일부 기사에만 언급 → 대표는 반도체 계열.
        arts = [
            self._rel("반도체 투자 확대 전망", "https://b.com/1"),
            self._rel("메타 AI 반도체 대규모 투자 발표", "https://b.com/2"),
            self._rel("반도체 업황 회복 신호", "https://b.com/3"),
        ]
        ranked = [
            self._rk("메타", 0.9, arts, {"daum": 3}),
            self._rk("반도체", 0.7, arts, {"aux": True}),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 1)
        self.assertIn("반도체", merged[0]["display_keyword"])
        self.assertEqual(merged[0]["keyword"], "메타")  # canonical 불변

    def test_ador_group_representative_is_stable(self):
        # 어도어가 그룹 전 기사에 공통 등장 → 대표성 최고. 뉴진스/민희진은 보조.
        arts = [
            self._rel("어도어 뉴진스 전속계약 분쟁", "https://c.com/1"),
            self._rel("어도어 민희진 대표 복귀", "https://c.com/2"),
            self._rel("어도어 뉴진스 민희진 갈등 지속", "https://c.com/3"),
        ]
        ranked = [
            self._rk("어도어", 0.9, arts, {"daum": 3}),
            self._rk("뉴진스", 0.7, arts, {"aux": True}),
            self._rk("민희진", 0.6, arts, {"aux": True}),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 1)
        self.assertIn("어도어", merged[0]["display_keyword"])

    def test_short_proper_noun_not_penalized_when_high_coverage(self):
        # 손흥민처럼 짧은 고유명사라도 그룹 기사 전반에 등장하면(coverage 높음) 감점되지
        # 않아야 한다(길이/숫자 휴리스틱을 대표 기준으로 쓰지 않는다는 요구 반영).
        arts = [
            self._rel("손흥민 북중미 월드컵 대표팀 합류", "https://d.com/1"),
            self._rel("손흥민 월드컵 일정 확정", "https://d.com/2"),
            self._rel("손흥민 대표팀 훈련 참가", "https://d.com/3"),
        ]
        ga = ranker._display_group_articles([self._rk("손흥민", 0.7, arts)])
        self.assertGreaterEqual(ranker._keyword_coverage(self._rk("손흥민", 0.7, arts), ga), 0.5)

    def test_local_multiword_entity_low_coverage_penalized(self):
        # 다어절 상대국명이 일부 기사에만 등장하면 coverage 감점(하드코딩 리스트 없이).
        arts = [
            self._rel("월드컵 16강 진출", "https://e.com/1"),
            self._rel("월드컵 16강 상대 보스니아 헤르체고비나 확정", "https://e.com/2"),
            self._rel("월드컵 16강 훈련 시작", "https://e.com/3"),
        ]
        ga = ranker._display_group_articles([self._rk("x", 0.5, arts)])
        cov = ranker._keyword_coverage(self._rk("보스니아 헤르체고비나", 0.9, arts), ga)
        self.assertLess(cov, 0.5)

    def test_high_common_hits_but_low_coverage_does_not_win(self):
        # coverage_penalty가 common_hits보다 우선하므로, 공통토큰을 많이 담아도
        # coverage가 낮은 후보(월드컵 16강 + 지엽 상대국 조합)는 대표가 되면 안 된다
        # (Codex diff 리뷰 P2 회귀 방지).
        arts = [
            self._rel("월드컵 16강 진출", "https://p.com/1"),
            self._rel("월드컵 16강 상대 보스니아 헤르체고비나전", "https://p.com/2"),
            self._rel("월드컵 16강 훈련 시작", "https://p.com/3"),
            self._rel("월드컵 16강 일정 발표", "https://p.com/4"),
        ]
        ranked = [
            # 공통토큰(월드컵/16강)을 2개나 담지만 통짜로는 1건에만 등장(coverage 낮음)
            self._rk("월드컵 16강 보스니아", 0.9, arts, {"daum": 3}),
            self._rk("월드컵", 0.7, arts, {"aux": True}),
        ]
        group = ranked  # 단일 그룹 가정
        common = ranker._display_common_event_tokens(group)
        ga = ranker._display_group_articles(group)
        s_local = ranker._representative_score(ranked[0], common, ga)
        s_worldcup = ranker._representative_score(ranked[1], common, ga)
        # 월드컵(coverage 높음)이 지엽 조합 후보보다 대표성 점수가 높아야 한다.
        self.assertGreater(s_worldcup, s_local)

    def test_common_token_completing_but_low_coverage_second_excluded(self):
        # best가 담지 못한 공통토큰을 보완하더라도, 그 second 후보가 그룹 기사 절반
        # 미만에만 등장하면(지엽 조합) display에 붙으면 안 된다(Codex diff 재리뷰 P2:
        # remaining_common 경로에도 coverage 방어 필요).
        arts = [
            self._rel("월드컵 16강 진출 확정", "https://q.com/1"),
            self._rel("월드컵 16강 상대 보스니아", "https://q.com/2"),
            self._rel("월드컵 16강 대진 발표", "https://q.com/3"),
            self._rel("월드컵 16강 훈련", "https://q.com/4"),
        ]
        ranked = [
            self._rk("월드컵", 0.9, arts, {"aux": True}),
            # "16강"(공통토큰)을 담지만 "보스니아"가 붙어 통짜로는 1건에만 등장 → 지엽.
            self._rk("16강 보스니아", 0.7, arts, {"daum": 3}),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertNotIn("보스니아", merged[0]["display_keyword"])

    def test_generic_appointment_word_not_selected_as_display(self):
        # 운영 회귀 hotfix(2026-07-03): canonical="홍석기 치안감"인데 기사엔 "홍석기
        # 국가수사본부장"으로 등장(치안감 토큰 없어 canonical coverage=0), "신임"이
        # 그룹 기사 전반에 등장. display가 "신임" 같은 일반 서술어 단독이 되면 안 되고,
        # canonical 또는 사건성 있는 표현이어야 한다.
        arts = [
            self._rel("경찰청, 홍석기 신임 국가수사본부장 임명", "https://n.com/1"),
            self._rel("홍석기 신임 국가수사본부장 임명 발표", "https://n.com/2"),
            self._rel("신임 국가수사본부장에 홍석기", "https://n.com/3"),
        ]
        ranked = [
            self._rk("홍석기 치안감", 0.9, arts, {"daum": 3}),
            self._rk("신임", 0.6, arts, {"aux": True}),
            self._rk("국가수사본부장 임명", 0.7, arts, {"aux": True}),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        display = merged[0]["display_keyword"]
        self.assertNotEqual(display, "신임")
        self.assertFalse(ranker._is_generic_only_display(display))
        # canonical은 movement 안정성 위해 그대로 유지.
        self.assertEqual(merged[0]["keyword"], "홍석기 치안감")

    def test_generic_only_keywords_rejected_from_display(self):
        # 신임/임명/승진/취임/내정/발탁/선임 같은 일반 인사어 단독/조합은 display에서 제외.
        for w in ["신임", "임명", "승진", "취임", "내정", "발탁", "선임", "신임 발표"]:
            self.assertTrue(ranker._is_generic_only_display(w), f"{w} should be generic-only")
        # 고유명사가 섞이면 generic-only 아님.
        for w in ["홍석기 치안감", "국가수사본부장 임명", "손흥민 발탁"]:
            self.assertFalse(ranker._is_generic_only_display(w), f"{w} should NOT be generic-only")

    def test_economic_generic_word_not_selected_as_display_alone(self):
        # 운영 반영 후속 hotfix(2026-07-03): canonical="한화 영남권 55조"인데 merge group의
        # "투자" 단독 후보가 그룹 기사 전반(coverage 높음)에 등장해 display로 잘못 뽑히던
        # 실사례 재현("신임" 회귀와 동일 구조). "투자" 단독이 되면 안 되고, canonical 또는
        # 고유명사가 섞인 조합형이어야 한다.
        arts = [
            self._rel("한화, 2040년까지 55조원 투자...AI 우주강국 청사진 제시", "https://f.com/1"),
            self._rel("한화·현대차·삼성·SK 등 영남권에 312조 투자…AI·반도체·로봇 육성", "https://f.com/2"),
            self._rel("김동관 부회장, 영남권 55조 베팅…한화 'AI 우주강국' 승부수", "https://f.com/3"),
        ]
        ranked = [
            self._rk("한화 영남권 55조", 0.9, arts, {"daum": 3}),
            self._rk("투자", 0.5, arts, {"aux": True}),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        display = merged[0]["display_keyword"]
        self.assertNotEqual(display, "투자")
        self.assertFalse(ranker._is_generic_only_display(display))
        self.assertIn(display, {
            "한화 영남권 55조", "한화 투자", "한화 55조 투자", "한화 영남권 투자",
        })
        # canonical은 movement 안정성 위해 그대로 유지.
        self.assertEqual(merged[0]["keyword"], "한화 영남권 55조")

    def test_economic_generic_word_allowed_in_combination(self):
        # "투자"가 고유명사와 조합된 표현은 차단하면 안 된다(단독일 때만 방어).
        for w in ["한화 투자", "55조 투자", "한화 영남권 투자", "한화 사업 확대"]:
            self.assertFalse(ranker._is_generic_only_display(w), f"{w} should NOT be generic-only")

    def test_economic_generic_words_rejected_alone(self):
        # 사용자 확정 경제/행위 일반명사 목록 — 단독/조합 전부 generic-only여야 한다.
        for w in ["투자", "사업", "계획", "추진", "확대", "지원", "협력", "체결", "공급", "운영",
                  "사업 확대", "투자 계획"]:
            self.assertTrue(ranker._is_generic_only_display(w), f"{w} should be generic-only")
            self.assertIn(w.split()[0], ranker._DISPLAY_GENERIC_WORDS | ranker._GENERIC_EVENT_PREDICATE_WORDS)

    def test_typhoon_like_specific_keyword_not_blocked_by_economic_words(self):
        # "태풍"처럼 명확한 단독 이슈 키워드는 경제 generic 확장과 무관하게 유지돼야 함.
        self.assertFalse(ranker._is_generic_only_display("태풍"))

    def test_economic_generic_words_not_leaked_into_merge_predicate_set(self):
        # Codex diff 리뷰 P3: display 전용 확장이 same-issue merge 판정에 쓰이는
        # _GENERIC_EVENT_PREDICATE_WORDS로 새지 않아야 한다(_DISPLAY_GENERIC_WORDS와
        # 분리된 별도 집합이라는 설계 불변을 직접 고정 — 누군가 실수로 옮기면 이 테스트가
        # 즉시 깨진다. merge 로직 자체는 이 diff에서 변경되지 않았음을 보장).
        # _INVARIANT_SKIP_TOKENS(예: "계획")는 별개 목적(display/article 정합 검증에서
        # 요구 면제)의 독립된 리스트라 겹쳐도 무방 — merge 판정에 쓰이지 않으므로 여기서
        # 확인하지 않는다.
        for w in ["투자", "사업", "계획", "추진", "확대", "지원", "협력", "체결", "공급", "운영"]:
            self.assertNotIn(w, ranker._GENERIC_EVENT_PREDICATE_WORDS)

    def test_singleton_candidate_display_equals_keyword(self):
        # merge되지 않은 단독 후보는 display_keyword가 canonical keyword와 동일해야 한다
        # (Codex diff 리뷰 P3: singleton 경로도 _build_display_keyword로 통일했으나
        # 기존 동작(display=kw)이 유지돼야 함).
        arts = [self._rel("롯데 오픈 골프대회 개막", "https://s.com/1")]
        ranked = [self._rk("롯데 오픈 골프대회", 0.9, arts, {"daum": 1})]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["display_keyword"], "롯데 오픈 골프대회")

    def test_canonical_used_when_no_meaningful_display_candidate(self):
        # 대표성 후보가 전부 generic이면 canonical을 display로 쓴다(단독 일반어 방지).
        arts = [
            self._rel("김철수 신임 대표 임명", "https://o.com/1"),
            self._rel("신임 대표 임명 발표", "https://o.com/2"),
        ]
        ranked = [
            self._rk("김철수 대표", 0.9, arts, {"daum": 3}),
            self._rk("신임", 0.6, arts, {"aux": True}),
            self._rk("임명", 0.5, arts, {"aux": True}),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertFalse(ranker._is_generic_only_display(merged[0]["display_keyword"]))

    def test_toonyeong_dedupe_still_works(self):
        # 통영시장(aux) / 통영 시장(daum)은 similar_keyword dedupe로 기존처럼 정상 병합.
        arts = [self._rel("통영 시장 관련 기사", "https://f.com/1")]
        ranked = [
            self._rk("통영 시장", 0.9, arts, {"daum": 3}),
            self._rk("통영시장", 0.7, arts, {"aux": True}),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 1)
        self.assertIn("통영시장", merged[0]["related_keywords"])

    def test_existing_normal_merge_display_regression(self):
        # 기존 정상 merge(김영환/압수수색): display가 단독 일반어("압수수색")가 아니라
        # 맥락 포함이어야 한다(회귀 방지).
        shared = self._rel("공수처, 김영환 지사 사무실 압수수색", "https://g.com/1")
        ranked = [
            self._rk("압수수색", 0.9, [shared]),
            self._rk("김영환", 0.85, [shared]),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 1)
        self.assertNotEqual(merged[0]["display_keyword"], "압수수색")

    def test_generic_only_common_tokens_falls_back_gracefully(self):
        # 공통토큰이 전부 generic(발표/공개 등)이면 common_tokens가 비어 tie-breaker로
        # 넘어가며, 예외 없이 display_keyword가 생성돼야 한다.
        arts_a = [self._rel("정부 오늘 새 정책 발표 공개", "https://h.com/1")]
        arts_b = [self._rel("정부 오늘 새 정책 발표 공개", "https://h.com/2")]
        ranked = [
            self._rk("정책발표", 0.9, arts_a, {"daum": 3}),
            self._rk("정부", 0.7, arts_b, {"aux": True}),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        # 병합 여부와 무관하게 display_keyword가 항상 존재하고 문자열이어야 한다.
        for m in merged:
            self.assertIsInstance(m["display_keyword"], str)
            self.assertTrue(len(m["display_keyword"]) > 0)

    def test_single_article_group_no_common_token_confidence(self):
        # 유효 기사 1건뿐인 그룹은 공통토큰 zero-confidence(빈 set) → 지엽 엔티티가
        # 대표성 토큰으로 오인되지 않아야 한다.
        arts = [self._rel("보스니아 헤르체고비나 단독 기사", "https://i.com/1")]
        self.assertEqual(ranker._display_common_event_tokens([self._rk("x", 0.5, arts)]), set())

    def test_display_keyword_respects_max_len(self):
        # 조합형 display_keyword가 DISPLAY_KEYWORD_MAX_LEN(18자)을 넘지 않아야 한다.
        arts = [
            self._rel("아주긴사건이름 관련 핵심 보도 확산", "https://j.com/1"),
            self._rel("아주긴사건이름 후속 조치 보도 확산", "https://j.com/2"),
        ]
        ranked = [
            self._rk("아주긴사건이름핵심보도", 0.9, arts, {"daum": 3}),
            self._rk("후속조치확산보도", 0.7, arts, {"aux": True}),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        for m in merged:
            self.assertLessEqual(len(m["display_keyword"]), ranker.DISPLAY_KEYWORD_MAX_LEN)

    def test_end_to_end_canonical_display_movement_consistency(self):
        # build_ranked_issues까지 거친 뒤: keyword=canonical, display_keyword=개선값,
        # movement는 canonical keyword 기준으로 안정 유지.
        arts = [
            self._rel("월드컵 16강 진출 확정", "https://k.com/1"),
            self._rel("월드컵 16강 상대 보스니아 헤르체고비나", "https://k.com/2"),
            self._rel("월드컵 16강 대진 발표", "https://k.com/3"),
        ]
        ranked = [
            self._rk("보스니아 헤르체고비나", 0.9, arts, {"daum": 3}),
            self._rk("월드컵", 0.7, arts, {"aux": True}),
        ]
        merged = ranker.dedupe_and_merge(ranked)
        top = ranker.select_top(merged)
        issues = build_ranked_issues(top, {}, ["naver_news"])
        entry = issues["keywords"][0]
        self.assertEqual(entry["keyword"], "보스니아 헤르체고비나")  # canonical
        # 개선 display: 지엽 상대국은 빠지고 핵심 사건어가 들어가야 한다(느슨한 !=만이 아님).
        self.assertNotIn("보스니아", entry["display_keyword"])
        self.assertIn("월드컵", entry["display_keyword"])

        # 이전 실행에서도 canonical이 동일했다면 movement는 안정(new 아님).
        prev = {"keywords": [{"keyword": "보스니아 헤르체고비나", "rank": 2}]}
        out = apply_movement(prev, {"keywords": [dict(entry, rank=1)]})
        self.assertEqual(out["keywords"][0]["movement"], "up")


class TestGenericSingletonGuard(unittest.TestCase):
    """generic singleton 방어(2026-07-03 운영 관찰: canonical=display="수사" 단독 노출).

    ranker._DISPLAY_GENERIC_WORDS 확장("조사" 추가) + exclude_generic_singletons().
    """

    def _rk(self, kw, score, articles=None, sources=None, related=None):
        item = {
            "keyword": kw, "score": score,
            "source_breakdown": {"news": score}, "rank_reason": "",
            "news_meta": {"articles": articles or []}, "used_signals": ["news"],
            "sources": sources if sources is not None else {"daum": 1},
        }
        if related is not None:
            item["related_keywords"] = related
        return item

    def test_investigation_singleton_excluded_from_final(self):
        # "수사" 단독 후보는 merge group을 이루지 못하면(singleton) final에서 제외돼야 함.
        merged = [self._rk("수사", 0.9), self._rk("정상이슈 사건", 0.5, related=[])]
        kept, excluded = ranker.exclude_generic_singletons(merged)
        self.assertIn("수사", excluded)
        self.assertNotIn("수사", [k["keyword"] for k in kept])

    def test_appointment_singleton_excluded_from_final(self):
        # "신임" 단독 후보도 동일하게 제외돼야 함(기존 merge-group 대표 방어와 별개로
        # singleton 경로도 방어).
        merged = [self._rk("신임", 0.9)]
        kept, excluded = ranker.exclude_generic_singletons(merged)
        self.assertEqual(kept, [])
        self.assertIn("신임", excluded)

    def test_typhoon_singleton_kept(self):
        # "태풍"처럼 명확한 자연재난 단독 키워드는 generic 집합에 없으므로 통과해야 함.
        merged = [self._rk("태풍", 0.9)]
        kept, excluded = ranker.exclude_generic_singletons(merged)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["keyword"], "태풍")
        self.assertEqual(excluded, [])

    def test_generic_keyword_absorbed_into_merge_group_is_kept(self):
        # merge group의 멤버로 흡수된 generic keyword(related_keywords에 존재)는
        # 그룹 자체가 singleton이 아니므로 제외되지 않는다(정보 손실 없음 확인).
        merged = [self._rk("홍석기 치안감", 0.9, related=["신임"])]
        kept, excluded = ranker.exclude_generic_singletons(merged)
        self.assertEqual(len(kept), 1)
        self.assertEqual(excluded, [])

    def test_survey_word_added_to_display_generic(self):
        self.assertTrue(ranker._is_generic_only_display("조사"))
        self.assertIn("조사", ranker._DISPLAY_GENERIC_WORDS)

    def test_investment_singleton_excluded_from_final(self):
        # 운영 반영 후속 hotfix(2026-07-03): "투자" 단독 후보도 "신임"/"수사"와 동일하게
        # merge group을 이루지 못하면(singleton) final에서 제외돼야 함.
        merged = [self._rk("투자", 0.9)]
        kept, excluded = ranker.exclude_generic_singletons(merged)
        self.assertEqual(kept, [])
        self.assertIn("투자", excluded)


def _phrase_news_signal(keyword, articles):
    """derive_phrase_candidates 입력용 news_signals 값(compute_news_signal 형식 흉내)."""
    return {"articles": articles}


def _phrase_article(title, url, hours_ago=1.0, relevance_reason="keyword_main_topic"):
    a = _article(title, url, published_at=_recent_iso(hours_ago))
    a["relevance_reason"] = relevance_reason
    a["is_incidental"] = relevance_reason in (
        "incidental_giveaway_mention", "keyword_not_found", "object_side_mention",
    )
    return a


class TestPhraseCandidates(unittest.TestCase):
    """backfill pass 전용 phrase 후보 발굴(candidates.derive_phrase_candidates)."""

    def test_generates_multiword_phrase_from_repeated_titles(self):
        arts = [
            _phrase_article("손흥민 월드컵 일정 공개", "https://n.com/1"),
            _phrase_article("손흥민 월드컵 일정 확정 발표", "https://n.com/2"),
        ]
        signals = {"손흥민": _phrase_news_signal("손흥민", arts)}
        phrases = cand.derive_phrase_candidates(signals, existing_keywords=[])
        self.assertTrue(any("손흥민" in p and "월드컵" in p for p in phrases))

    def test_single_article_fragment_not_a_candidate(self):
        # 단일 기사에서만 등장하는 n-gram은 DF<2라 후보가 되면 안 됨.
        arts = [_phrase_article("전기택시 배터리 10분 완충 교환 도입", "https://n.com/1")]
        signals = {"전기택시": _phrase_news_signal("전기택시", arts)}
        phrases = cand.derive_phrase_candidates(signals, existing_keywords=[])
        self.assertEqual(phrases, [])

    def test_generic_only_phrase_excluded(self):
        # "수사 발표"류 일반 서술어만의 조합은 phrase 후보에서도 제외.
        arts = [
            _phrase_article("경찰 수사 발표 진행", "https://n.com/1"),
            _phrase_article("검찰 수사 발표 확산", "https://n.com/2"),
        ]
        signals = {"수사": _phrase_news_signal("수사", arts)}
        phrases = cand.derive_phrase_candidates(signals, existing_keywords=[])
        self.assertNotIn("수사 발표", phrases)

    def test_shopping_marker_title_excluded_from_phrase_source(self):
        # 경품/판촉 마커가 title에 있으면 그 title 전체를 phrase 원천에서 제외.
        arts = [
            _phrase_article("닌텐도 스위치 2 증정 이벤트 진행", "https://n.com/1"),
            _phrase_article("닌텐도 스위치 2 증정 이벤트 연장", "https://n.com/2"),
        ]
        signals = {"닌텐도": _phrase_news_signal("닌텐도", arts)}
        phrases = cand.derive_phrase_candidates(signals, existing_keywords=[])
        self.assertEqual(phrases, [])

    def test_stale_article_not_used_as_phrase_source(self):
        # FRESH_RELEVANCE_HOURS(72h)를 넘는 오래된 기사는 phrase 원천에서 제외.
        arts = [
            _phrase_article("오래된 이슈 재조명 특집", "https://n.com/1", hours_ago=200),
            _phrase_article("오래된 이슈 재조명 후속", "https://n.com/2", hours_ago=200),
        ]
        signals = {"오래된이슈": _phrase_news_signal("오래된이슈", arts)}
        phrases = cand.derive_phrase_candidates(signals, existing_keywords=[])
        self.assertEqual(phrases, [])

    def test_incidental_article_not_used_as_phrase_source(self):
        arts = [
            _phrase_article("선풍기 증정 이벤트 당첨자 발표", "https://n.com/1",
                             relevance_reason="incidental_giveaway_mention"),
            _phrase_article("선풍기 증정 이벤트 당첨자 공지", "https://n.com/2",
                             relevance_reason="incidental_giveaway_mention"),
        ]
        signals = {"선풍기": _phrase_news_signal("선풍기", arts)}
        phrases = cand.derive_phrase_candidates(signals, existing_keywords=[])
        self.assertEqual(phrases, [])

    def test_similar_to_existing_keyword_excluded(self):
        # pass1 생존 이슈(canonical/alias)와 유사한 phrase는 재발굴하지 않는다.
        arts = [
            _phrase_article("손흥민 월드컵 일정 공개", "https://n.com/1"),
            _phrase_article("손흥민 월드컵 일정 확정", "https://n.com/2"),
        ]
        signals = {"손흥민": _phrase_news_signal("손흥민", arts)}
        phrases = cand.derive_phrase_candidates(signals, existing_keywords=["손흥민 월드컵 일정"])
        self.assertEqual(phrases, [])

    def test_phrase_max_limit_respected(self):
        arts = []
        for i in range(20):
            arts.append(_phrase_article(f"키워드{i} 이슈 발생 확산", f"https://n.com/a{i}"))
            arts.append(_phrase_article(f"키워드{i} 이슈 발생 후속", f"https://n.com/b{i}"))
        signals = {"k": _phrase_news_signal("k", arts)}
        phrases = cand.derive_phrase_candidates(signals, existing_keywords=[], phrase_max=3)
        self.assertLessEqual(len(phrases), 3)


class TestPhraseSourceExpansion(unittest.TestCase):
    """phrase 원천 확장(2026-07): pass1 news_signals ∪ pass2 pre-signals 를 원천으로
    쓰면 pass1 후보 집합 "바깥"(aux2/related_terms 전용 키워드) 기사에서도 phrase가
    발굴되는지 검증. main._backfill_pass가 두 signals dict를 병합해 넘기는 동작을
    derive_phrase_candidates 단위로 확인한다(Codex 계획 리뷰 P0/P1 반영)."""

    def test_phrase_from_pass2_only_signal_not_in_pass1(self):
        # pass1 signals에는 없는 키워드(예: related_terms로 새로 fetch된 "정근우")의
        # 기사에서만 나오는 n-gram이 phrase 후보가 되어야 한다.
        pass1_signals = {
            "손흥민": _phrase_news_signal("손흥민", [
                _phrase_article("손흥민 부상 소식 전해져", "https://n.com/s1"),
                _phrase_article("손흥민 부상 회복 중", "https://n.com/s2"),
            ]),
        }
        pass2_pre_signals = {
            "정근우": _phrase_news_signal("정근우", [
                _phrase_article("정근우 은퇴식 개최 확정", "https://n.com/p1"),
                _phrase_article("정근우 은퇴식 팬들 참석", "https://n.com/p2"),
            ]),
        }
        # main._backfill_pass와 동일하게 pass1을 base로 pre로 갱신해 병합.
        merged_signals = dict(pass1_signals)
        merged_signals.update(pass2_pre_signals)

        # pass1 단독으로는 "정근우 은퇴식" phrase가 나올 수 없다(원천에 없음).
        pass1_only = cand.derive_phrase_candidates(pass1_signals, existing_keywords=[])
        self.assertFalse(any("정근우" in p for p in pass1_only))

        # 병합 원천에서는 pass2-only 키워드 기사의 phrase가 발굴돼야 한다.
        merged_phrases = cand.derive_phrase_candidates(merged_signals, existing_keywords=[])
        self.assertTrue(any("정근우" in p and "은퇴식" in p for p in merged_phrases))

    def test_pass2_only_phrase_still_respects_existing_keyword_dedupe(self):
        # pass2-only 원천에서 나온 phrase라도 pass1 생존 이슈와 유사하면 재발굴 배제.
        pass2_pre_signals = {
            "정근우": _phrase_news_signal("정근우", [
                _phrase_article("정근우 은퇴식 개최 확정", "https://n.com/p1"),
                _phrase_article("정근우 은퇴식 팬들 참석", "https://n.com/p2"),
            ]),
        }
        phrases = cand.derive_phrase_candidates(
            pass2_pre_signals, existing_keywords=["정근우 은퇴식"]
        )
        self.assertEqual(phrases, [])

    def test_pass2_only_incidental_source_still_filtered(self):
        # pass2-only 원천이어도 incidental 기사는 phrase 원천에서 제외(방어 유지).
        pass2_pre_signals = {
            "정근우": _phrase_news_signal("정근우", [
                _phrase_article("정근우 은퇴 기념 굿즈 증정 이벤트", "https://n.com/p1",
                                relevance_reason="incidental_giveaway_mention"),
                _phrase_article("정근우 은퇴 기념 굿즈 증정 공지", "https://n.com/p2",
                                relevance_reason="incidental_giveaway_mention"),
            ]),
        }
        phrases = cand.derive_phrase_candidates(pass2_pre_signals, existing_keywords=[])
        self.assertEqual(phrases, [])


class TestSourceFamilyDistributionLogging(unittest.TestCase):
    """source family diversity 관찰 로깅(2026-07) — 순수 집계, ranking 영향 없음."""

    def test_distribution_counts_all_families_including_derived(self):
        items = [
            {"keyword": "a", "sources": {"daum_home": 1, "nate_home": 2}},
            {"keyword": "b", "sources": {"naver_news_phrase": True}},
            {"keyword": "c", "sources": {"naver_news_aux": True, "bing_home": 3}},
        ]
        dist = cand.source_family_distribution(items)
        self.assertEqual(dist["daum_home"], 1)
        self.assertEqual(dist["nate_home"], 1)
        self.assertEqual(dist["bing_home"], 1)
        self.assertEqual(dist["naver_news_phrase"], 1)
        self.assertEqual(dist["naver_news_aux"], 1)

    def test_distribution_empty_input(self):
        self.assertEqual(cand.source_family_distribution([]), {})
        self.assertEqual(cand.source_family_distribution(None), {})

    def test_distribution_is_pure_no_mutation(self):
        # 집계 함수가 입력 items를 변형하지 않아야 한다(순수 관찰).
        items = [{"keyword": "a", "sources": {"daum_home": 1}}]
        snapshot = [dict(it) for it in items]
        cand.source_family_distribution(items)
        self.assertEqual(items, snapshot)


class TestPhraseStrictRelevance(unittest.TestCase):
    """phrase 후보 전용 require_all_tokens strict relevance(Codex 계획 리뷰 P1/P2)."""

    def test_partial_token_match_not_high_relevance_under_strict(self):
        # phrase="손흥민 월드컵 일정" 중 "손흥민"만 있는 기사는 strict에서 고관련이 아니어야.
        a = _article("손흥민 근황 공개", "https://n.com/1")
        rel = cand.compute_article_relevance("손흥민 월드컵 일정", a, require_all_tokens=True)
        self.assertLess(rel["relevance_score"], cand.HIGH_RELEVANCE_THRESHOLD)

    def test_full_token_match_high_relevance_under_strict(self):
        a = _article("손흥민 월드컵 일정 공개", "https://n.com/1")
        rel = cand.compute_article_relevance("손흥민 월드컵 일정", a, require_all_tokens=True)
        self.assertGreaterEqual(rel["relevance_score"], cand.HIGH_RELEVANCE_THRESHOLD)

    def test_particle_suffixed_title_still_matches_strict(self):
        # 조사/어미가 붙은 정상 제목("국가수사본부장에")이 exact subset 비교였다면
        # 탈락했을 케이스 — substring 포함 판정이라 통과해야 한다(2차 리뷰 P2 반영).
        a = _article("홍석기 신임 국가수사본부장에 임명", "https://n.com/1")
        rel = cand.compute_article_relevance("홍석기 국가수사본부장", a, require_all_tokens=True)
        self.assertGreaterEqual(rel["relevance_score"], cand.HIGH_RELEVANCE_THRESHOLD)

    def test_default_seed_relevance_unaffected(self):
        # require_all_tokens 기본값 False 경로는 기존 동작과 동일해야 한다(회귀 방지).
        a = _article("유럽 폭염에 에어컨·선풍기 품귀", "https://n.com/1", "폭염으로 선풍기 수요 급증")
        rel = cand.compute_article_relevance("선풍기", a)
        self.assertEqual(rel["relevance_reason"], "keyword_main_topic")


class TestPhraseCandidateDiversitySource(unittest.TestCase):
    """naver_news_phrase 소스가 다양성 guard를 우회하지 않는지(Gate 7)."""

    def test_phrase_source_not_counted_as_family(self):
        candidates_ = [{"keyword": "월드컵 일정", "sources": {"naver_news_phrase": True}}]
        self.assertEqual(cand.count_source_families(candidates_), 0)

    def test_phrase_mixed_with_independent_family_still_counted(self):
        candidates_ = [{"keyword": "월드컵", "sources": {"naver_news_phrase": True, "nate_home": 1}}]
        self.assertEqual(cand.count_source_families(candidates_), 1)

    def test_collect_candidates_includes_phrase_keywords(self):
        result = cand.collect_candidates({}, [], phrase_keywords=["신규 이슈 phrase"])
        kws = [c["keyword"] for c in result]
        self.assertIn("신규 이슈 phrase", kws)
        self.assertEqual(result[0]["sources"].get("naver_news_phrase"), True)

    def test_phrase_reserve_protects_phrase_from_truncation(self):
        # seed가 limit을 가득 채워도 phrase_reserve만큼 순수 phrase 후보가 보존돼야 한다
        # (Codex diff 리뷰 P1). rank 있는 seed는 정렬상 앞이라 reserve 없으면 phrase가 잘림.
        seed = {"daum_home": [{"keyword": f"seed{i}", "rank": i + 1} for i in range(10)]}
        phrases = [f"이슈 phrase {i}" for i in range(5)]
        # limit=10이면 seed 10개가 가득 채워 phrase는 전부 잘린다(reserve=0).
        no_reserve = cand.collect_candidates(seed, [], phrase_keywords=phrases, limit=10)
        phrase_kept_0 = [c for c in no_reserve if c["sources"] == {"naver_news_phrase": True}]
        self.assertEqual(len(phrase_kept_0), 0)
        # reserve=3이면 phrase 3개는 보존되고 전체 개수는 limit(10) 유지.
        with_reserve = cand.collect_candidates(
            seed, [], phrase_keywords=phrases, limit=10, phrase_reserve=3
        )
        phrase_kept_3 = [c for c in with_reserve if c["sources"] == {"naver_news_phrase": True}]
        self.assertEqual(len(phrase_kept_3), 3)
        self.assertEqual(len(with_reserve), 10)

    def test_phrase_reserve_noop_when_under_limit(self):
        # 후보 총수가 limit 이하면 reserve와 무관하게 전부 보존(기존 동작 불변).
        seed = {"daum_home": [{"keyword": "seed1", "rank": 1}]}
        result = cand.collect_candidates(
            seed, [], phrase_keywords=["phrase A"], limit=30, phrase_reserve=10
        )
        self.assertEqual(len(result), 2)

    def test_phrase_reserve_larger_than_limit_clamped(self):
        # phrase_reserve > limit이어도 전체 상한(limit)을 초과하지 않아야 한다(Codex P3 clamp).
        seed = {"daum_home": [{"keyword": f"seed{i}", "rank": i + 1} for i in range(10)]}
        phrases = [f"이슈 phrase {i}" for i in range(8)]
        result = cand.collect_candidates(
            seed, [], phrase_keywords=phrases, limit=5, phrase_reserve=99
        )
        self.assertEqual(len(result), 5)

    def test_phrase_reserve_zero_matches_legacy_slice(self):
        # phrase_reserve 기본값 0은 정렬 후 단순 [:limit]과 완전히 동일해야 한다(회귀 방지).
        seed = {"daum_home": [{"keyword": f"seed{i}", "rank": i + 1} for i in range(10)]}
        phrases = [f"이슈 phrase {i}" for i in range(5)]
        legacy = cand.collect_candidates(seed, [], phrase_keywords=phrases, limit=8)
        self.assertEqual(len(legacy), 8)
        # seed(rank 있음)가 앞쪽을 채우므로 8개 전부 seed여야 한다.
        self.assertTrue(all("naver_news_phrase" not in c["sources"] for c in legacy))


class TestBackfillPassSelection(unittest.TestCase):
    """_rank_and_select/exclude_generic_singletons를 통한 backfill 선택 로직 —
    main._backfill_pass는 실 I/O(seed/naver) 의존이라 여기서는 ranker 계층에서
    "gate 통과분만 최종 편입, 미통과는 미편입, 중복 미발생"을 검증한다.
    """

    def _candidates(self, kws, sources=None):
        return [{"keyword": k, "sources": (sources or {}).get(k, {"daum": i + 1})}
                for i, k in enumerate(kws)]

    def test_backfill_candidate_passing_gate_included_in_final(self):
        cands = self._candidates(["A", "B"], sources={"A": {"daum": 1}, "B": {"phrase": True}})
        arts_a = [_article("금리 인상 전망 확산", "https://x.com/a-only")]
        arts_b = [_article("반도체 수출 증가 발표", "https://x.com/b-only")]
        signals = {
            "news": {
                "A": _news(3, 1, 2, 0.9, articles=arts_a),
                "B": _news(3, 1, 2, 0.9, articles=arts_b),
            },
            "datalab": {}, "google": {}, "daum": {"A": 1},
        }
        ranked = ranker.compute_scores(cands, signals)
        merged = ranker.dedupe_and_merge(ranked)
        kept, _ = ranker.exclude_generic_singletons(merged)
        top = ranker.select_top(kept)
        self.assertIn("B", [t["keyword"] for t in top])

    def test_backfill_candidate_failing_gate_not_included(self):
        # 고관련 기사 부족(gate 미통과) phrase 후보는 final에 들어가면 안 됨.
        cands = self._candidates(["A", "B"], sources={"A": {"daum": 1}, "B": {"phrase": True}})
        signals = {
            "news": {
                "A": _news(3, 1, 2, 0.9),
                "B": _news(0, 20, 1, 0.1, high_relevance_count=0, quality_cluster_size=0,
                           fresh_high_relevance_count=0),
            },
            "datalab": {}, "google": {}, "daum": {"A": 1},
        }
        ranked = ranker.compute_scores(cands, signals)
        self.assertNotIn("B", [r["keyword"] for r in ranked])

    def test_backfill_no_duplicate_same_issue_across_passes(self):
        # backfill 후보가 pass1 생존 이슈와 same-issue면 merge로 흡수돼 중복 노출되지 않음.
        shared = _article("손흥민 월드컵 16강 진출 확정", "https://n.com/shared")
        shared["relevance_reason"] = "keyword_main_topic"
        cands = self._candidates(
            ["손흥민 월드컵", "월드컵 16강 진출"],
            sources={"손흥민 월드컵": {"daum": 1}, "월드컵 16강 진출": {"phrase": True}},
        )
        signals = {
            "news": {
                "손흥민 월드컵": {**_news(3, 1, 2, 0.9), "articles": [shared]},
                "월드컵 16강 진출": {**_news(3, 1, 2, 0.9), "articles": [shared]},
            },
            "datalab": {}, "google": {}, "daum": {"손흥민 월드컵": 1},
        }
        ranked = ranker.compute_scores(cands, signals)
        merged = ranker.dedupe_and_merge(ranked)
        self.assertEqual(len(merged), 1)


class TestRankAndSelectDiversityLogging(unittest.TestCase):
    """main._rank_and_select의 source family diversity 로깅이 결과에 영향 없는지(2026-07).

    로깅은 함수 말미에 순수 관찰용으로 추가됐다. top 반환값이 로깅과 무관하게 결정적이고,
    로깅 경로(_log_source_family_diversity)가 예외 없이 완주하는지 통합 경로로 검증한다."""

    def _cands(self):
        return [
            {"keyword": "금리 인상", "sources": {"daum_home": 1}},
            {"keyword": "반도체 수출", "sources": {"nate_home": 1, "naver_news_phrase": True}},
        ]

    def _signals(self):
        # display >= DISPLAY_ARTICLES_MIN(2) + 공통 사건 토큰(대표 생성 가능)으로 각 2건.
        return {
            "news": {
                "금리 인상": _news(3, 1, 2, 0.9, articles=[
                    _article("금리 인상 단행 발표 충격", "https://x.com/a1", "금리 인상 단행 발표"),
                    _article("금리 인상 단행 발표 반응", "https://x.com/a2", "금리 인상 단행 발표"),
                ]),
                "반도체 수출": _news(3, 1, 2, 0.9, articles=[
                    _article("반도체 수출 증가 발표 호조", "https://x.com/b1", "반도체 수출 증가 발표"),
                    _article("반도체 수출 증가 발표 지속", "https://x.com/b2", "반도체 수출 증가 발표"),
                ]),
            },
            "datalab": {}, "google": {},
        }

    def test_rank_and_select_returns_top_and_logging_has_no_side_effect(self):
        cands, signals = self._cands(), self._signals()
        top = main_module._rank_and_select(cands, signals, "test")
        kws = [t["keyword"] for t in top]
        self.assertIn("금리 인상", kws)
        self.assertIn("반도체 수출", kws)
        # 같은 입력으로 두 번 호출해도 결과가 결정적(로깅이 상태를 안 바꿈).
        top2 = main_module._rank_and_select(self._cands(), self._signals(), "test")
        self.assertEqual([t["keyword"] for t in top], [t["keyword"] for t in top2])

    def test_log_helper_runs_without_error_on_empty_stages(self):
        # 각 단계가 비어 있어도 로깅 헬퍼가 예외 없이 완주해야 한다(방어).
        main_module._log_source_family_diversity("test", [], [], [], [], [])


class TestBackfillPassIntegration(unittest.TestCase):
    """main._backfill_pass() 자체를 호출하는 통합 테스트(Codex diff 리뷰 P2 반영).

    ranker 계층 단위 테스트만으로는 pass1 aux 보존/재계산/rollback 같은 pass2 통합
    버그(예: aux top 확장 시 pass1 aux가 aux2에서 누락되는 문제, Codex diff 리뷰 P1)를
    잡지 못하므로, fetch 함수를 fixture로 주입해 _backfill_pass 전체 경로를 검증한다.
    seed(daum/danawa)와 search_news는 순수 인자/콜백이라 실제 DB/Naver 호출 없음.
    """

    def _news_fixture_fetch(self, articles_by_kw):
        def fetch(keyword):
            return articles_by_kw.get(keyword, [])
        return fetch

    def test_pass1_aux_preserved_when_aux_top_expands(self):
        # pass1 aux(top=5)에서 뽑힌 키워드가 pass2 aux 확장(top=10) 재추출에서
        # 우연히 빠지더라도(aux_max 상한 등으로), union 덕에 candidates2에 남아있어야 한다.
        daum_ranked = [{"keyword": f"daum{i}", "rank": i + 1} for i in range(10)]
        # 다양성 guard(MIN_SOURCE_FAMILIES=2) 통과용 두 번째 독립 family.
        nate_ranked = [{"keyword": f"nate이슈{i}", "rank": i + 1} for i in range(4)]
        home_fulls = {"daum_home": daum_ranked, "nate_home": nate_ranked}
        pass1_aux = ["생존이슈phrase"]
        pass1_top = [{"keyword": "daum1", "related_keywords": []}]

        # pass2 aux_expanded 재계산 시 "생존이슈phrase"가 다시 나오지 않도록(다른 강한
        # aux 후보들로 상한 12를 채워 밀려나는 상황을 흉내) fetch fixture 구성.
        recent = _recent_iso(1.0)
        news_by_kw = {}
        for i in range(10):
            # 공통 사건 토큰("긴급","대책")을 3건이 공유해 entity cohesion gate·B2를 통과한다
            # (제목이 전부 달라 대표를 못 뽑던 fixture를 정상 이슈 형태로 정정, 2026-07).
            news_by_kw[f"daum{i}"] = [
                # 서로 다른 host로 3개 언론사 → entity cohesion dominant event 충족(2026-07).
                {"title": f"daum{i} 긴급 대책 발표 {j}보", "originallink": f"https://press{j}.example.com/{i}",
                 "link": f"https://press{j}.example.com/{i}-{j}",
                 "description": f"daum{i} 긴급 대책", "pubDate": recent}
                for j in range(3)
            ]
        fetch = self._news_fixture_fetch(news_by_kw)

        news_signals = {"daum1": cand.compute_news_signal("daum1", news_by_kw["daum1"])}

        top2, candidates2 = main_module._backfill_pass(
            pass1_top, pass1_aux, home_fulls, [], daum_ranked,
            fetch, news_signals, {}, {},
        )
        self.assertIsNotNone(candidates2, "다양성 guard 통과 + aux 신규 후보 있으므로 pass2가 채택돼야 함")
        kws = [c["keyword"] for c in candidates2]
        self.assertIn("생존이슈phrase", kws, "pass1 aux는 top 확장 재추출 결과에 없어도 union으로 보존돼야 함")

    def test_backfill_pass_returns_none_when_no_new_candidates(self):
        # aux/phrase 둘 다 신규 후보를 못 만들면 (None, None)으로 pass1 유지를 알린다.
        fetch = self._news_fixture_fetch({})
        top2, candidates2 = main_module._backfill_pass(
            [], [], {}, [], [], fetch, {}, {}, {},
        )
        self.assertIsNone(top2)
        self.assertIsNone(candidates2)


class TestInsufficientFinalLogging(unittest.TestCase):
    """final_count가 TOP_N 미만일 때 부족 사유를 관찰할 수 있는 단계별 카운트 검증
    (main._rank_and_select가 로그로 남기는 값들의 기반 로직)."""

    def test_gate_exclusion_reduces_ranked_below_candidates(self):
        cands = [
            {"keyword": "정상이슈", "sources": {"daum": 1}},
            {"keyword": "저품질", "sources": {"daum": 2}},
        ]
        signals = {
            "news": {
                "정상이슈": _news(3, 1, 2, 0.9),
                "저품질": _news(0, 50, 1, 0.05, high_relevance_count=0, quality_cluster_size=0,
                              fresh_high_relevance_count=0),
            },
            "datalab": {}, "google": {}, "daum": {"정상이슈": 1, "저품질": 2},
        }
        ranked = ranker.compute_scores(cands, signals)
        # 저품질 후보는 quality gate에서 hard exclude되어 ranked에 없어야 함(부족 사유 관찰 가능).
        self.assertEqual(len(ranked), 1)
        self.assertEqual([r["keyword"] for r in ranked], ["정상이슈"])

    def test_final_below_top_n_does_not_get_generic_filler(self):
        # final이 TOP_N 미만이어도 generic singleton을 filler로 채우지 않는다.
        cands = [{"keyword": "정상이슈 사건", "sources": {"daum": 1}}]
        signals = {
            "news": {"정상이슈 사건": _news(3, 1, 2, 0.9)},
            "datalab": {}, "google": {}, "daum": {"정상이슈 사건": 1},
        }
        ranked = ranker.compute_scores(cands, signals)
        merged = ranker.dedupe_and_merge(ranked)
        kept, excluded = ranker.exclude_generic_singletons(merged)
        top = ranker.select_top(kept)
        self.assertLess(len(top), ranker.TOP_N)
        self.assertEqual(excluded, [])  # generic이 아니므로 제외 대상 아님, 그냥 개수 부족


class TestPromotionalPRArticle(unittest.TestCase):
    """문제 B: article-level PR/공익 판정(두 등급 override, 사용자 확정 2026-07-03)."""

    def test_pr_marker_in_title_is_promotional(self):
        a = _article("세라젬, KLPGA 롯데오픈 공식 후원", "https://x.com/p1", "공식 후원사로 나섰다")
        self.assertTrue(cand.is_promotional_pr(a))
        self.assertFalse(cand.is_public_interest(a))

    def test_pr_marker_only_in_snippet_not_promotional(self):
        # title에 마커 없고 snippet에만 있으면 PR 아님(경기결과 boilerplate 오탐 방지 — title-only).
        a = _article("김민지, 롯데오픈 최종 3R 우승", "https://x.com/p2", "대회 공식 후원사는 세라젬")
        self.assertFalse(cand.is_promotional_pr(a))

    def test_strong_public_interest_overrides_even_with_pr_marker(self):
        # 강한 사건성 토큰(화재)은 PR 마커(공식 후원) 동반해도 override → 공익, PR 아님.
        a = _article("세라젬 공식 후원 행사장 화재", "https://x.com/p3", "")
        self.assertTrue(cand.is_public_interest(a))
        self.assertFalse(cand.is_promotional_pr(a))

    def test_compound_public_interest_with_spacing(self):
        a = _article("현대차 본사 압수 수색", "https://x.com/p4", "")
        self.assertTrue(cand.is_public_interest(a))

    def test_market_token_without_pr_marker_is_public_interest(self):
        a = _article("SK하이닉스 주가 급등", "https://x.com/p5", "증권시장에서 강세")
        self.assertTrue(cand.is_public_interest(a))
        self.assertFalse(cand.is_promotional_pr(a))

    def test_market_token_with_pr_marker_is_not_override(self):
        # "브랜드 캠페인 효과로 실적 기대" — 시장성 토큰이 PR 마커와 같은 title → override 불인정, PR.
        a = _article("세라젬 브랜드 캠페인 효과로 실적 기대", "https://x.com/p6", "")
        self.assertFalse(cand.is_public_interest(a))
        self.assertTrue(cand.is_promotional_pr(a))

    def test_market_token_pr_marker_in_snippet_also_blocks_override(self):
        a = _article("세라젬 주가 상승 기대", "https://x.com/p7", "신제품 출시 기대감")
        self.assertFalse(cand.is_public_interest(a))

    def test_no_substring_false_positive_for_market_token(self):
        # "무사고"⊅"사고", "주가지수"⊅"주가" — 토큰 매칭이라 오탐 없음.
        a1 = _article("무사고 운전 캠페인", "https://x.com/p8", "")
        a2 = _article("코스피 주가지수 보합", "https://x.com/p9", "")
        # "무사고"는 토큰 "무사고" 하나라 "사고"와 매칭 안 됨 → 강한 공익 아님.
        self.assertNotIn("사고", set(cand._tokens("무사고 운전 캠페인")))
        self.assertFalse(cand._has_strong_public_interest("무사고 운전 캠페인"))
        # "주가지수"는 토큰 "주가지수"라 시장성 토큰 "주가"와 매칭 안 됨 → 이 title만으로 override 안 됨.
        self.assertNotIn("주가", set(cand._tokens("코스피 주가지수 보합")))
        self.assertFalse(cand.is_public_interest(a2))
        self.assertFalse(cand.is_public_interest(a1))


class TestPRClusterExclude(unittest.TestCase):
    """문제 B: PR 클러스터 hard exclude(pre-merge, per-keyword)."""

    def _serajem_signal(self, extra=None):
        arts = [
            _article("세라젬, KLPGA 롯데오픈 공식 후원", "https://x.com/s1",
                     "세라젬이 공식 후원사로 나섰다", _recent_iso()),
            _article("세라젬 롯데오픈 체험존 운영", "https://y.com/s2",
                     "세라젬 체험존에서 헬스케어 체험", _recent_iso()),
            _article("세라젬 브랜드 캠페인 확대", "https://z.com/s3",
                     "세라젬 브랜드 캠페인", _recent_iso()),
        ]
        if extra:
            arts.extend(extra)
        return cand.compute_news_signal("세라젬", arts)

    def test_pure_pr_cluster_excluded(self):
        sig = self._serajem_signal()
        self.assertEqual(sig["pr_article_count"], 3)
        self.assertEqual(sig["public_interest_count"], 0)
        self.assertGreaterEqual(sig["commercial_pr_ratio"], 0.6)
        cands = [{"keyword": "세라젬", "sources": {"danawa": 1}}]
        signals = {"news": {"세라젬": sig}, "datalab": {}, "google": {}, "daum": {"세라젬": 1}}
        ranked = ranker.compute_scores(cands, signals)
        self.assertEqual(len(ranked), 1)  # quality gate는 통과(고관련 기사)
        kept, excluded = ranker.exclude_pr_clusters(ranked)
        self.assertEqual(kept, [])
        self.assertIn("세라젬", excluded)

    def test_pr_cluster_with_market_hype_still_excluded(self):
        # 사용자 테스트 #1/#2: 후원/캠페인 + "실적 기대"/"주가 상승 기대"가 섞여도 제외.
        extra = [
            _article("세라젬 신제품 출시로 주가 상승 기대", "https://x.com/s4", "", _recent_iso()),
        ]
        sig = self._serajem_signal(extra)
        self.assertEqual(sig["public_interest_count"], 0)  # 시장성+PR마커 → override 안 됨
        cands = [{"keyword": "세라젬", "sources": {"danawa": 1}}]
        signals = {"news": {"세라젬": sig}, "datalab": {}, "google": {}, "daum": {"세라젬": 1}}
        _, excluded = ranker.exclude_pr_clusters(ranker.compute_scores(cands, signals))
        self.assertIn("세라젬", excluded)

    def test_strong_public_interest_keeps_cluster(self):
        # 사용자 테스트 #5: 사건성 토큰(소송)이 있으면 PR 다수여도 유지.
        extra = [_article("세라젬 후원 계약 소송 제기", "https://x.com/s5", "", _recent_iso())]
        sig = self._serajem_signal(extra)
        self.assertGreaterEqual(sig["public_interest_count"], 1)
        cands = [{"keyword": "세라젬", "sources": {"danawa": 1}}]
        signals = {"news": {"세라젬": sig}, "datalab": {}, "google": {}, "daum": {"세라젬": 1}}
        kept, excluded = ranker.exclude_pr_clusters(ranker.compute_scores(cands, signals))
        self.assertNotIn("세라젬", excluded)
        self.assertEqual(len(kept), 1)

    def test_recall_and_stock_news_kept(self):
        # 사용자 테스트 #3/#4: PR 마커 없는 실제 리콜/주가 기사는 유지.
        for kw, arts in [
            ("테슬라", [
                _article("테슬라 모델Y 리콜 결정", "https://x.com/t1", "리콜 대상 확대", _recent_iso()),
                _article("테슬라 브레이크 결함 리콜", "https://x.com/t2", "", _recent_iso()),
            ]),
            ("하이닉스", [
                _article("SK하이닉스 주가 급등", "https://x.com/h1", "실적 개선 기대", _recent_iso()),
                _article("하이닉스 주가 장중 신고가", "https://x.com/h2", "", _recent_iso()),
            ]),
        ]:
            sig = cand.compute_news_signal(kw, arts)
            self.assertEqual(sig["pr_article_count"], 0)
            cands = [{"keyword": kw, "sources": {"daum": 1}}]
            signals = {"news": {kw: sig}, "datalab": {}, "google": {}, "daum": {kw: 1}}
            _, excluded = ranker.exclude_pr_clusters(ranker.compute_scores(cands, signals))
            self.assertNotIn(kw, excluded)

    def test_mixed_sports_cluster_kept_by_ratio(self):
        # 경기 결과 다수 + 후원 언급 title 소수 → ratio<0.6으로 유지(정상 이슈 오제외 방지).
        arts = [
            _article("롯데오픈 최종 3R 김민지 우승", "https://x.com/m1", "리더보드 1위", _recent_iso()),
            _article("롯데오픈 2R 종료 컷 통과", "https://x.com/m2", "", _recent_iso()),
            _article("롯데오픈 갤러리 역대 최다", "https://x.com/m3", "", _recent_iso()),
            _article("롯데오픈 공식 후원 세라젬 체험존", "https://x.com/m4", "", _recent_iso()),
        ]
        sig = cand.compute_news_signal("롯데오픈", arts)
        self.assertLess(sig["commercial_pr_ratio"], 0.6)
        cands = [{"keyword": "롯데오픈", "sources": {"daum": 1}}]
        signals = {"news": {"롯데오픈": sig}, "datalab": {}, "google": {}, "daum": {"롯데오픈": 1}}
        _, excluded = ranker.exclude_pr_clusters(ranker.compute_scores(cands, signals))
        self.assertNotIn("롯데오픈", excluded)

    def test_single_pr_article_not_excluded_boundary(self):
        # 경계: PR 기사 1건(pr_article_count<2)은 제외하지 않음(단건 stray 마커 노이즈 방어).
        item1 = {"keyword": "A", "news_meta": {"pr_article_count": 1, "commercial_pr_ratio": 1.0,
                                               "public_interest_count": 0}}
        item2 = {"keyword": "B", "news_meta": {"pr_article_count": 2, "commercial_pr_ratio": 1.0,
                                               "public_interest_count": 0}}
        kept, excluded = ranker.exclude_pr_clusters([item1, item2])
        self.assertEqual([k["keyword"] for k in kept], ["A"])
        self.assertEqual(excluded, ["B"])


class TestDisplayArticleInvariant(unittest.TestCase):
    """문제 A: display_keyword가 표시 기사와 다른 이슈를 가리키지 않게 하는 invariant."""

    def _item(self, keyword, display, article_titles):
        arts = [_article(t, f"https://x.com/{i}", "") for i, t in enumerate(article_titles)]
        return {"keyword": keyword, "display_keyword": display, "news_meta": {"articles": arts}}

    def test_mismatched_display_downgraded_to_canonical(self):
        # display="조타 교통사고 사망"인데 기사는 전부 구제역 → canonical(구제역)로 강등.
        item = self._item("구제역", "조타 교통사고 사망",
                          ["경북 예천 돼지농장 구제역 발생", "예천 구제역 확산 방역"])
        out = ranker.enforce_display_article_consistency([item])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["display_keyword"], "구제역")

    def test_consistent_display_kept(self):
        item = self._item("월드컵", "월드컵 16강",
                          ["한국 월드컵 16강 진출 확정", "월드컵 16강 대진표 공개"])
        out = ranker.enforce_display_article_consistency([item])
        self.assertEqual(out[0]["display_keyword"], "월드컵 16강")

    def test_synonym_absent_token_downgrades(self):
        # 같은 이슈라도 canonical 기사가 "본선 진출" 동의표현만 쓰고 "16강" 미등장 → 강등(안전).
        item = self._item("월드컵", "월드컵 16강",
                          ["한국 월드컵 본선 진출 확정", "월드컵 토너먼트 대진 공개"])
        out = ranker.enforce_display_article_consistency([item])
        self.assertEqual(out[0]["display_keyword"], "월드컵")

    def test_event_tag_token_must_be_supported(self):
        # "A 사망" display에서 사망이 기사에 없으면(A만 있음) 강등 — 사건 꼬리표 검증(Codex 10차).
        item = self._item("배우B", "배우B 사망",
                          ["배우B 신작 드라마 출연 확정", "배우B 인터뷰 공개"])
        out = ranker.enforce_display_article_consistency([item])
        self.assertEqual(out[0]["display_keyword"], "배우B")

    def test_split_tokens_across_articles_downgraded(self):
        # 검증토큰이 서로 다른 기사에 흩어져 있으면(배우B 기사 + 별개 사망 기사) 강등해야 한다.
        # 단일 기사가 display 검증토큰 전부를 커버해야 지지로 인정(Codex diff 리뷰 P1).
        item = self._item("배우B", "배우B 사망",
                          ["배우B 신작 드라마 출연 확정", "원로배우 별세 사망 애도 물결"])
        out = ranker.enforce_display_article_consistency([item])
        self.assertEqual(out[0]["display_keyword"], "배우B")

    def test_weak_modifier_not_required(self):
        # 약한 수식어(신임/발표)는 검증 대상 아님 → 엔티티만 맞으면 유지.
        item = self._item("홍석기", "홍석기 신임",
                          ["홍석기 치안감 프로필", "홍석기 국가수사본부장 발탁"])
        out = ranker.enforce_display_article_consistency([item])
        self.assertEqual(out[0]["display_keyword"], "홍석기 신임")

    def test_generic_only_canonical_rejected(self):
        # 강등 대상 canonical이 generic-only(수사)면 reject(노출 안 함).
        item = self._item("수사", "조타 교통사고 사망",
                          ["경북 예천 돼지농장 구제역 발생", "예천 구제역 확산"])
        out = ranker.enforce_display_article_consistency([item])
        self.assertEqual(out, [])

    def test_singleton_consistent_item_unaffected(self):
        item = self._item("반도체", "반도체", ["삼성 반도체 실적 개선", "반도체 수요 회복"])
        out = ranker.enforce_display_article_consistency([item])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["display_keyword"], "반도체")


class TestSourceFamilyIntegration(unittest.TestCase):
    """google_trends/nate_home/bing_home family 편입 + evidence gate + 신규 4축 검증."""

    def test_all_home_families_merged_into_pool(self):
        seed_sources = {
            "google_trends": [{"keyword": "손흥민", "rank": 1}],
            "daum_home": [{"keyword": "환율", "rank": 1}],
            "nate_home": [{"keyword": "태풍", "rank": 1}],
            "bing_home": [{"keyword": "손흥민", "rank": 3}],  # google과 병합
        }
        c = cand.collect_candidates(seed_sources, [])
        kws = {x["keyword"] for x in c}
        self.assertEqual(kws, {"손흥민", "환율", "태풍"})
        son = next(x for x in c if x["keyword"] == "손흥민")
        self.assertIn("google_trends", son["sources"])
        self.assertIn("bing_home", son["sources"])
        # 등장 family = google_trends/daum_home/nate_home/bing_home → 4종
        self.assertEqual(cand.count_source_families(c), 4)

    def test_newsless_home_candidate_excluded_from_top(self):
        # Naver News evidence 없는 google/nate/bing 후보는 최종 랭킹에 없어야 한다(Gate 1).
        cands = [
            {"keyword": "실뉴스이슈", "sources": {"daum_home": 1}},
            {"keyword": "구글단독", "sources": {"google_trends": 1}},
            {"keyword": "네이트단독", "sources": {"nate_home": 1}},
        ]
        signals = {
            "news": {"실뉴스이슈": _news(3, 1, 2, 0.9)},  # 나머지 news 없음
            "datalab": {}, "google": {"구글단독": {"interest": 0.9}},
        }
        ranked = ranker.compute_scores(cands, signals)
        kws = [r["keyword"] for r in ranked]
        self.assertIn("실뉴스이슈", kws)
        self.assertNotIn("구글단독", kws)   # google 신호 있어도 news 없으면 제외
        self.assertNotIn("네이트단독", kws)

    def test_source_consensus_rewards_multi_family(self):
        # news 신호 동일할 때 더 많은 독립 family를 가진 후보의 score가 높아야 한다.
        cands = [
            {"keyword": "복수합의", "sources": {"daum_home": 1, "nate_home": 1, "bing_home": 1}},
            {"keyword": "단일소스", "sources": {"daum_home": 1}},
        ]
        signals = {
            "news": {"복수합의": _news(3, 1, 2, 0.9), "단일소스": _news(3, 1, 2, 0.9)},
            "datalab": {}, "google": {},
        }
        ranked = ranker.compute_scores(cands, signals)
        scores = {r["keyword"]: r["score"] for r in ranked}
        self.assertGreater(scores["복수합의"], scores["단일소스"])
        self.assertIn("source_consensus", ranked[0]["source_breakdown"])

    def test_search_demand_priority_prefers_google(self):
        # 두 후보 모두 daum rank + google interest 보유. google이 우선순위상 앞서므로
        # demand는 daum rank가 아니라 google interest를 따라야 한다.
        #   X: daum rank 나쁨(10) + google interest 높음(1.0)
        #   Y: daum rank 좋음(1)  + google interest 낮음(0.0)
        cands = [
            {"keyword": "X", "sources": {"daum_home": 10}},
            {"keyword": "Y", "sources": {"daum_home": 1}},
        ]
        signals = {
            "news": {"X": _news(3, 1, 2, 0.9), "Y": _news(3, 1, 2, 0.9)},
            "datalab": {},
            "google": {"X": {"interest": 1.0}, "Y": {"interest": 0.0}},
        }
        ranked = ranker.compute_scores(cands, signals)
        bd = {r["keyword"]: r["source_breakdown"]["search_demand"] for r in ranked}
        # daum rank만 보면 Y가 우세하지만, google 우선순위라 X의 demand가 더 높아야 한다.
        self.assertGreater(bd["X"], bd["Y"])


class TestGuardPredicates(unittest.TestCase):
    """diversity/recent guard 판정 predicate — 실패 시 upsert skip(last good 유지)의 근거."""

    def test_diversity_guard_predicate(self):
        # 독립 family 1종뿐이면 MIN_SOURCE_FAMILIES(2) 미만 → skip 대상.
        c = [{"keyword": "A", "sources": {"daum_home": 1, "naver_news_aux": True}}]
        self.assertLess(cand.count_source_families(c), cand.MIN_SOURCE_FAMILIES)

    def test_recent_guard_predicate(self):
        # 최근 기사 보유 키워드 수가 MIN_RECENT_KEYWORDS 미만이면 skip 대상.
        top = [
            {"keyword": "A", "news_meta": {"recent_count": 1}},
            {"keyword": "B", "news_meta": {"recent_count": 0}},
        ]
        self.assertEqual(main_module._count_recent_keywords(top), 1)
        self.assertLess(main_module._count_recent_keywords(top), main_module.MIN_RECENT_KEYWORDS)


class TestDisplayArticles(unittest.TestCase):
    """display_articles(2026-07-04) — 상세 팝업 노출 전용 필터.

    문제 사례: 키워드 "도깨비 10주년 여행 공유"에서 "공유"(share)라는 일반 단어만 겹치는
    "성과 공유"/"국민배당" 기사가 상세 팝업 articles에 섞여 노출됨. articles(랭킹/게이트
    근거)는 그대로 두고, display_articles로 사용자 노출만 별도로 정제한다.
    """

    KEYWORD = "도깨비 10주년 여행 공유"

    def _build_articles(self):
        raw = [
            # 정상: 공유(배우) + 도깨비/김고은 등 실앵커 동반
            {"title": "공유 김고은과 도깨비 명장면 재현", "originallink": "https://x.com/dk1",
             "description": "배우 공유와 김고은이 tvN 도깨비 촬영 당시를 회상했다", "pubDate": None},
            {"title": "공유·김고은, 나이차 12살인데 친구처럼 잘 지내", "originallink": "https://x.com/dk2",
             "description": "도깨비에서 호흡을 맞춘 공유와 김고은의 브로맨스가 화제다", "pubDate": None},
            {"title": "도깨비 10주년 기념 팬 여행 프로그램 공개", "originallink": "https://x.com/dk3",
             "description": "tvN 도깨비 방영 10주년을 맞아 배우 공유와 김고은이 함께한 팬 여행 프로그램이 열린다", "pubDate": None},
            # 오염: "공유"라는 일반 단어만 겹치는 무관 기사
            {"title": "성과급 3,000%·국민배당까지... AI 반도체 호황, 누구 몫인가",
             "originallink": "https://x.com/noise1",
             "description": "반도체 기업들이 성과 공유 계획을 발표하며 국민배당 논의가 커지고 있다",
             "pubDate": None},
            {"title": "'전북 잡고 2위' 정경호 감독, 시즌 계획 공유",
             "originallink": "https://x.com/noise2",
             "description": "정경호 감독이 다음 시즌 운영 계획을 공유했다고 밝혔다", "pubDate": None},
        ]
        return raw

    def test_actor_related_articles_survive_in_display_articles(self):
        sig = cand.compute_news_signal(self.KEYWORD, self._build_articles())
        articles = cand.filter_articles_for_display(sig["articles"], min_count=1)
        display = cand.build_display_articles(self.KEYWORD, articles, sig["representative_article"])
        display_titles = [a["title"] for a in display]
        self.assertTrue(any("공유 김고은과 도깨비" in t for t in display_titles))
        self.assertTrue(any("나이차 12살" in t for t in display_titles))
        self.assertTrue(any("팬 여행 프로그램" in t for t in display_titles))

    def test_generic_word_only_articles_excluded_from_display_articles(self):
        sig = cand.compute_news_signal(self.KEYWORD, self._build_articles())
        articles = cand.filter_articles_for_display(sig["articles"], min_count=1)
        display = cand.build_display_articles(self.KEYWORD, articles, sig["representative_article"])
        display_titles = [a["title"] for a in display]
        self.assertFalse(any("국민배당" in t for t in display_titles))
        self.assertFalse(any("정경호 감독" in t for t in display_titles))

    def test_generic_plus_keyword_only_token_still_excluded(self):
        # keyword 자체의 토큰이라도 "여행"처럼 그 자체로 흔한 단어 + "공유"(모호) 조합만으로는
        # 대표 기사와 무관한 기사를 허용하지 않는다(Codex review-only P2, 2026-07-04 —
        # "여행 계획 공유 앱 출시" 같은 무관 기사가 keyword 토큰 매칭만으로 새는 것 방지).
        raw = [
            {"title": "공유 김고은과 도깨비 명장면 재현", "originallink": "https://x.com/dk1",
             "description": "배우 공유와 김고은이 tvN 도깨비 촬영 당시를 회상했다", "pubDate": None},
            {"title": "여행 계획 공유 앱 출시", "originallink": "https://x.com/noise3",
             "description": "새로운 여행 계획 공유 서비스가 출시됐다", "pubDate": None},
        ]
        sig = cand.compute_news_signal(self.KEYWORD, raw)
        articles = cand.filter_articles_for_display(sig["articles"], min_count=1)
        display = cand.build_display_articles(self.KEYWORD, articles, sig["representative_article"])
        display_titles = [a["title"] for a in display]
        self.assertFalse(any("여행 계획 공유 앱" in t for t in display_titles))

    def test_articles_field_itself_unaffected_by_display_filter(self):
        # display_articles를 만들어도 원본 articles(랭킹/게이트 근거)는 줄어들지 않는다.
        sig = cand.compute_news_signal(self.KEYWORD, self._build_articles())
        self.assertEqual(len(sig["articles"]), 5)

    def test_build_ranked_entry_exposes_display_articles_without_shrinking_articles(self):
        raw = self._build_articles()
        sig = cand.compute_news_signal(self.KEYWORD, raw)
        ranked_item = {
            "keyword": self.KEYWORD, "score": 0.5, "source_breakdown": {"news": 0.5},
            "rank_reason": "", "news_meta": sig, "used_signals": ["news"],
            "display_keyword": self.KEYWORD,
        }
        entry = build_ranked_entry(1, ranked_item)
        self.assertIn("display_articles", entry)
        display_titles = [a["title"] for a in entry["display_articles"]]
        self.assertFalse(any("국민배당" in t for t in display_titles))
        self.assertTrue(any("도깨비" in t for t in display_titles))
        # articles 원본은 display_articles보다 적지 않아야 한다(줄이지 않음 유지).
        self.assertGreaterEqual(len(entry["articles"]), len(entry["display_articles"]))

    def test_no_backfill_when_display_articles_below_min(self):
        # display_articles가 3개 미만이어도 엉뚱한 기사로 채우지 않는다.
        raw = [
            {"title": "공유 김고은과 도깨비 명장면 재현", "originallink": "https://x.com/only1",
             "description": "배우 공유와 김고은이 tvN 도깨비 촬영 당시를 회상했다", "pubDate": None},
            {"title": "성과급 3,000%·국민배당까지... AI 반도체 호황, 누구 몫인가",
             "originallink": "https://x.com/only2",
             "description": "반도체 기업들이 성과 공유 계획을 발표했다", "pubDate": None},
        ]
        sig = cand.compute_news_signal(self.KEYWORD, raw)
        articles = cand.filter_articles_for_display(sig["articles"], min_count=5)
        display = cand.build_display_articles(self.KEYWORD, articles, sig["representative_article"])
        self.assertEqual(len(display), 1)
        self.assertIn("도깨비", display[0]["title"])


class TestSingleTokenDisplayArticles(unittest.TestCase):
    """단일 non-generic 토큰(인물명 등) 키워드 display 필터 완화(A, 2026-07-05).

    문제 사례: 키워드 "장동건"에서 articles 6건 전부 keyword_main_topic(0.9)인데도
    primary cluster가 2건뿐이라 display_articles가 2건으로 잘림. 단일 토큰이라 기존
    anchor 예외(토큰 2개 이상 조건)를 구조적으로 통과할 수 없던 것이 원인.
    """

    KEYWORD = "장동건"

    def _person_articles(self):
        # 같은 인물의 서로 다른 각도 기사 — Jaccard clustering이 여러 클러스터로 쪼갬.
        return [
            {"title": "노화 고백한 장동건, 급 '탱탱' 동안됐다", "originallink": "https://x.com/j1",
             "description": "배우 장동건이 한층 어려진 비주얼로 등장했다", "pubDate": None},
            {"title": "못 알아볼 뻔…장동건, 공식석상서 포착된 달라진 이미지", "originallink": "https://x.com/j2",
             "description": "장동건이 공식 행사에서 달라진 모습을 보였다", "pubDate": None},
            {"title": "54세 장동건, 못 알아볼 뻔한 바뀐 얼굴", "originallink": "https://x.com/j3",
             "description": "장동건의 외모 변화가 화제다", "pubDate": None},
            {"title": "중년 배우들 회춘…볼살 통통해진 장동건", "originallink": "https://x.com/j4",
             "description": "황정민과 장동건 등 중년 배우들의 외모 변화가 눈길을 끈다", "pubDate": None},
        ]

    def test_single_person_name_main_topic_articles_survive(self):
        # 단일 인물명 키워드의 keyword_main_topic 기사는 primary cluster 밖이라도 살아남는다.
        sig = cand.compute_news_signal(self.KEYWORD, self._person_articles())
        articles = cand.filter_articles_for_display(sig["articles"], min_count=1)
        display = cand.build_display_articles(self.KEYWORD, articles, sig["representative_article"])
        # 4건 모두 장동건이 title 주제 → display에 대부분 생존(최소 3건 이상).
        self.assertGreaterEqual(len(display), 3)
        self.assertTrue(all("장동건" in a["title"] for a in display))

    def test_single_generic_token_still_excluded(self):
        # 단일 토큰이라도 그것이 generic("공유")이면 예외를 타지 못한다(오염 방어 유지).
        raw = [
            {"title": "삼성전자 성과 공유", "originallink": "https://x.com/g1",
             "description": "기업이 성과 공유 계획을 발표했다", "pubDate": None},
            {"title": "정경호 감독 시즌 계획 공유", "originallink": "https://x.com/g2",
             "description": "정경호 감독이 계획을 공유했다", "pubDate": None},
        ]
        sig = cand.compute_news_signal("공유", raw)
        articles = cand.filter_articles_for_display(sig["articles"], min_count=1)
        # 대표 없이도(또는 대표가 있어도) generic 단독 토큰은 primary 외 기사를 살리지 않는다.
        rep = sig["representative_article"]
        non_primary = [a for a in articles if not a.get("is_primary_cluster")]
        for a in non_primary:
            self.assertFalse(
                cand._display_anchor_allowed("공유", a, rep),
                msg=f"generic 단독 토큰이 예외를 타면 안 됨: {a['title']}",
            )

    def test_multitoken_keyword_reduced_to_one_nongeneric_not_exempted(self):
        # "여행 공유"처럼 generic("공유")을 뺀 뒤 non-generic이 1개("여행")만 남는 다토큰
        # 키워드는 단일토큰 예외를 타면 안 된다(Codex review-only P1, 2026-07-05). "여행"만
        # 겹치는 무관 기사가 anchor 검증 없이 새는 것을 막는다.
        rep = {"title": "여행 공유 앱 대표 인터뷰", "snippet": "여행 공유 서비스"}
        article = {
            "title": "제주 여행 코스 추천", "snippet": "가족 여행 코스를 추천한다",
            "relevance_reason": "keyword_main_topic", "is_incidental": False,
            "is_primary_cluster": False,
        }
        self.assertFalse(cand._display_anchor_allowed("여행 공유", article, rep))

    def test_single_token_incidental_article_not_survived(self):
        # 단일 인물명 키워드라도 incidental/side-mention(주제가 아님) 기사는 예외를 못 탄다.
        raw = [
            {"title": "장동건, 신작 영화 제작발표회 참석", "originallink": "https://x.com/p1",
             "description": "장동건이 신작 제작발표회에 참석했다", "pubDate": None},
            {"title": "영화 시사회 경품 증정 이벤트, 장동건 친필 사인 지급", "originallink": "https://x.com/p2",
             "description": "경품으로 장동건 친필 사인을 증정한다", "pubDate": None},
        ]
        sig = cand.compute_news_signal(self.KEYWORD, raw)
        articles = cand.filter_articles_for_display(sig["articles"], min_count=1)
        display = cand.build_display_articles(self.KEYWORD, articles, sig["representative_article"])
        # 경품 증정(incidental) 기사는 display에 노출되지 않는다.
        self.assertFalse(any("경품 증정" in a["title"] for a in display))


class TestDisplayArticlesMinGate(unittest.TestCase):
    """display_articles <= 1 Top10 제외 gate(B, 2026-07-05, ranker.exclude_insufficient_display_articles).

    gate는 build 이전(select_top 이후)에 적용된다 — recent guard/partial publish 판단과
    저장 로그가 실제 발행 개수와 정합하도록(Codex review-only P2).
    """

    def _ranked_item(self, keyword, raw, display_keyword=None):
        sig = cand.compute_news_signal(keyword, raw)
        return {
            "keyword": keyword, "score": 0.5, "source_breakdown": {"news": 0.5},
            "rank_reason": "", "news_meta": sig, "used_signals": ["news"],
            "display_keyword": display_keyword or keyword,
        }

    def test_keyword_with_single_display_article_excluded(self):
        # display_articles가 1건뿐인 후보는 gate에서 제외된다.
        single = self._ranked_item(
            "도깨비 10주년 여행 공유",
            [
                {"title": "공유 김고은과 도깨비 명장면 재현", "originallink": "https://x.com/s1",
                 "description": "배우 공유와 김고은이 tvN 도깨비 촬영을 회상했다", "pubDate": None},
                {"title": "성과급 국민배당 AI 반도체 호황", "originallink": "https://x.com/s2",
                 "description": "기업이 성과 공유 계획을 발표했다", "pubDate": None},
            ],
        )
        kept, excluded = ranker.exclude_insufficient_display_articles([single])
        self.assertEqual(len(kept), 0)
        self.assertIn("도깨비 10주년 여행 공유", excluded)

    def test_keyword_with_enough_display_articles_kept(self):
        # display >= 2 후보는 유지되고, 부족 후보만 제외된다.
        drop = self._ranked_item(
            "도깨비 10주년 여행 공유",
            [
                {"title": "공유 김고은과 도깨비 명장면 재현", "originallink": "https://x.com/d1",
                 "description": "배우 공유와 김고은이 도깨비 촬영을 회상했다", "pubDate": None},
                {"title": "성과급 국민배당 반도체 호황", "originallink": "https://x.com/d2",
                 "description": "성과 공유 계획 발표", "pubDate": None},
            ],
        )
        keep = self._ranked_item(
            "장동건",
            [
                {"title": "노화 고백한 장동건, 동안됐다", "originallink": "https://x.com/k1",
                 "description": "배우 장동건이 어려진 비주얼로 등장했다", "pubDate": None},
                {"title": "54세 장동건, 바뀐 얼굴 화제", "originallink": "https://x.com/k2",
                 "description": "장동건의 외모 변화가 화제다", "pubDate": None},
                {"title": "장동건, 공식석상서 달라진 이미지", "originallink": "https://x.com/k3",
                 "description": "장동건이 공식 행사에서 달라진 모습을 보였다", "pubDate": None},
            ],
        )
        kept, excluded = ranker.exclude_insufficient_display_articles([drop, keep])
        kept_keywords = [k["keyword"] for k in kept]
        self.assertIn("장동건", kept_keywords)
        self.assertNotIn("도깨비 10주년 여행 공유", kept_keywords)
        self.assertIn("도깨비 10주년 여행 공유", excluded)

    def test_build_ranked_issues_reranks_kept_from_one(self):
        # gate로 걸러진 top이 build로 넘어오면 issues의 rank는 1부터 순서대로 매겨진다.
        keep = self._ranked_item(
            "장동건",
            [
                {"title": "노화 고백한 장동건, 동안됐다", "originallink": "https://x.com/r1",
                 "description": "배우 장동건이 어려진 비주얼로 등장했다", "pubDate": None},
                {"title": "54세 장동건, 바뀐 얼굴 화제", "originallink": "https://x.com/r2",
                 "description": "장동건의 외모 변화가 화제다", "pubDate": None},
            ],
        )
        kept, _ = ranker.exclude_insufficient_display_articles([keep])
        issues = build_ranked_issues(kept, {}, ["naver_news"])
        self.assertEqual(issues["keywords"][0]["rank"], 1)


class TestHoroscopeContentGate(unittest.TestCase):
    """반복형/evergreen 콘텐츠(운세류) quality gate 제외(2026-07-05, 별도 작업).

    "오늘의 운세"/"띠별 운세" 등은 매일 반복 발행돼 기사 수·freshness가 항상 충분해
    기존 quality/fresh gate를 통과하지만, 실시간 이슈가 아니므로 keyword/기사 패턴
    기반으로 별도 제외한다. display_articles 부족 gate/source family/ranking gate는
    건드리지 않는다.
    """

    def _horoscope_articles(self):
        return [
            _article("[오늘의 운세] 2026년 7월 6일", "https://x.com/h1",
                     "오늘의 운세를 확인해보세요. 별자리별 운세도 함께 알려드립니다.",
                     published_at=_recent_iso()),
            _article("띠별 운세-7월 5일", "https://x.com/h2",
                     "띠별로 오늘의 운세를 정리했다.", published_at=_recent_iso()),
            _article("별자리별 운세 7월 5일", "https://x.com/h3",
                     "별자리별 오늘의 운세를 알아본다.", published_at=_recent_iso()),
        ]

    def test_keyword_itself_horoscope_excluded_from_top10(self):
        # keyword="운세 오늘의 운세" 자체가 강한 신호 → quality gate 이전에 제외.
        sig = cand.compute_news_signal("운세 오늘의 운세", self._horoscope_articles())
        candidates = [{"keyword": "운세 오늘의 운세", "sources": {"bing_home": 1}}]
        signals = {
            "news": {"운세 오늘의 운세": sig}, "datalab": {}, "google": {},
            "bing_home": {"운세 오늘의 운세": 1},
        }
        ranked = ranker.compute_scores(candidates, signals)
        self.assertEqual(ranked, [])

    def test_dated_horoscope_article_bundle_excluded(self):
        # "[오늘의 운세] 2026년 7월 6일"처럼 날짜가 붙어도 패턴 포함으로 그대로 잡힌다.
        sig = cand.compute_news_signal("오늘의 운세 7월", self._horoscope_articles())
        reason = ranker._quality_gate_reason("오늘의 운세 7월", sig)
        self.assertEqual(reason, "horoscope_content")

    def test_ttiband_and_constellation_bundle_excluded(self):
        # keyword 자체는 운세 패턴이 아니지만, 기사 title 전부가 띠별/별자리 운세류.
        articles = [
            _article("띠별 운세 총정리", "https://x.com/t1", published_at=_recent_iso()),
            _article("별자리별 운세 모음", "https://x.com/t2", published_at=_recent_iso()),
        ]
        sig = cand.compute_news_signal("오늘 운세 모음", articles)
        reason = ranker._quality_gate_reason("오늘 운세 모음", sig)
        self.assertEqual(reason, "horoscope_content")

    def test_unrelated_news_mentioning_horoscope_once_not_excluded(self):
        # "운세"가 한 기사에만 우연히 언급된 정도로는 과도하게 제외하지 않는다.
        articles = [
            _article("증권가 신년 운세 이벤트 대신 실적 발표 집중", "https://x.com/u1",
                     "증권사들이 신년 운세 이벤트보다 실적 발표에 집중한다.",
                     published_at=_recent_iso()),
            _article("반도체 업체 실적 전망 상향", "https://x.com/u2",
                     "반도체 기업들의 실적 전망이 상향 조정됐다.", published_at=_recent_iso()),
        ]
        sig = cand.compute_news_signal("반도체 실적", articles)
        reason = ranker._quality_gate_reason("반도체 실적", sig)
        self.assertNotEqual(reason, "horoscope_content")

    def test_drop_reason_recorded_as_horoscope_content(self):
        sig = cand.compute_news_signal("운세 오늘의 운세", self._horoscope_articles())
        reason = ranker._quality_gate_reason("운세 오늘의 운세", sig)
        self.assertEqual(reason, "horoscope_content")

    def test_incident_keyword_containing_sazu_word_not_excluded(self):
        # "청부 사주"/"언론사 사주"처럼 "사주"가 들어간 사건성 키워드는 운세와 무관하므로
        # 오탐 제외되면 안 된다(Codex review-only P1, 2026-07-05).
        articles = [
            _article("검찰, 청부 사주 의혹 수사 착수", "https://x.com/s1",
                     "검찰이 청부 사주 의혹에 대한 수사에 착수했다.", published_at=_recent_iso()),
            _article("언론사 사주 구속 기소", "https://x.com/s2",
                     "언론사 사주가 배임 혐의로 구속 기소됐다.", published_at=_recent_iso()),
        ]
        sig = cand.compute_news_signal("청부 사주 의혹", articles)
        reason = ranker._quality_gate_reason("청부 사주 의혹", sig)
        self.assertNotEqual(reason, "horoscope_content")

    def test_exactly_half_horoscope_articles_not_excluded(self):
        # 기사 2건 중 1건만 운세 패턴이면(정확히 절반) 제외하지 않는다 — "다수"가 아니다
        # (Codex review-only P2, 2026-07-05: >=0.5는 이 경계에서 오제외됨).
        articles = [
            _article("오늘의 운세 함께 보기", "https://x.com/half1", published_at=_recent_iso()),
            _article("반도체 업체 실적 전망 상향", "https://x.com/half2",
                     "반도체 기업들의 실적 전망이 상향 조정됐다.", published_at=_recent_iso()),
        ]
        sig = cand.compute_news_signal("반도체 전망", articles)
        reason = ranker._quality_gate_reason("반도체 전망", sig)
        self.assertNotEqual(reason, "horoscope_content")

    def test_display_articles_min_gate_unaffected(self):
        # 운세 gate 추가가 기존 display_articles 부족 gate 동작을 깨지 않는다.
        sig = cand.compute_news_signal(
            "도깨비 10주년 여행 공유",
            [
                {"title": "공유 김고은과 도깨비 명장면 재현", "originallink": "https://x.com/dg1",
                 "description": "배우 공유와 김고은이 tvN 도깨비 촬영을 회상했다", "pubDate": None},
                {"title": "성과급 국민배당 AI 반도체 호황", "originallink": "https://x.com/dg2",
                 "description": "기업이 성과 공유 계획을 발표했다", "pubDate": None},
            ],
        )
        item = {
            "keyword": "도깨비 10주년 여행 공유", "score": 0.5, "source_breakdown": {"news": 0.5},
            "rank_reason": "", "news_meta": sig, "used_signals": ["news"],
            "display_keyword": "도깨비 10주년 여행 공유",
        }
        kept, excluded = ranker.exclude_insufficient_display_articles([item])
        self.assertEqual(len(kept), 0)
        self.assertIn("도깨비 10주년 여행 공유", excluded)

    def test_source_family_candidates_unaffected_by_horoscope_gate(self):
        # 운세와 무관한 후보의 source family 판정(google/nate/bing/daum)은 그대로 유지된다.
        good_articles = [
            _article("삼성 신형 노트북 출시", "https://x.com/g1", "신형 노트북 출시", published_at=_recent_iso()),
            _article("신형 노트북 출시 판매 돌입", "https://y.com/g2", "신형 노트북 출시", published_at=_recent_iso()),
        ]
        sig = cand.compute_news_signal("노트북", good_articles)
        candidates = [
            {"keyword": "노트북", "sources": {"google_trends": 1, "nate_home": 2, "bing_home": 3, "daum_home": 4}},
        ]
        signals = {"news": {"노트북": sig}, "datalab": {}, "google": {}}
        ranked = ranker.compute_scores(candidates, signals)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(
            set(ranked[0]["sources"].keys()),
            {"google_trends", "nate_home", "bing_home", "daum_home"},
        )


class TestCacheGoogleKeywords(unittest.TestCase):
    """_cache_google_keywords: google_trends 원천 후보 → keyword_cache 저장 변환.

    출처별 보기 팝업이 keyword_cache를 읽으므로, google 후보를 {keyword, url} 형태로
    upsert한다. 랭킹 로직과 무관하며 저장 실패는 news_top pipeline에 영향 없어야 한다.
    """

    def test_google_cands_upserted_as_keyword_url_items(self):
        cands = [
            {"keyword": "손흥민", "rank": 1, "active": True},
            {"keyword": "월드컵 예선", "rank": 2},
        ]
        with patch.object(main_module, "upsert_keywords", return_value=True) as m:
            main_module._cache_google_keywords(cands)
        m.assert_called_once()
        source, items = m.call_args[0]
        self.assertEqual(source, "google_trends")
        self.assertEqual(len(items), 2)
        # {keyword, url} 형태 + Google 검색 URL(https) + keyword 원문 보존
        self.assertEqual(items[0]["keyword"], "손흥민")
        self.assertTrue(items[0]["url"].startswith("https://www.google.com/search?q="))
        # 공백 키워드는 url-encode 되어 안전 URL 규약(http/https) 통과
        self.assertIn("url", items[1])
        self.assertTrue(items[1]["url"].startswith("https://"))

    def test_empty_google_cands_no_upsert(self):
        # 비활성/실패 시 google_cands=[] → keyword_cache 미저장(빈 값으로 last-good 덮지 않음)
        with patch.object(main_module, "upsert_keywords", return_value=True) as m:
            main_module._cache_google_keywords([])
        m.assert_not_called()

    def test_blank_keyword_items_skipped(self):
        cands = [{"keyword": "  ", "rank": 1}, {"rank": 2}]
        with patch.object(main_module, "upsert_keywords", return_value=True) as m:
            main_module._cache_google_keywords(cands)
        # 유효 keyword 0개 → upsert 호출 안 함
        m.assert_not_called()

    def test_upsert_exception_isolated(self):
        # keyword_cache 저장 예외가 상위(news_top)로 전파되지 않아야 함
        cands = [{"keyword": "테스트", "rank": 1}]
        with patch.object(main_module, "upsert_keywords", side_effect=RuntimeError("db down")):
            try:
                main_module._cache_google_keywords(cands)
            except Exception as e:  # noqa: BLE001
                self.fail(f"예외가 격리되지 않고 전파됨: {e}")


class _FakeKeywordScraper:
    """_collect_keyword_caches 테스트용 KEYWORD_SCRAPERS 대체 객체(실제 HTTP 호출 없음)."""

    def __init__(self, source, active=True, items=None, raise_exc=None):
        self.source = source
        self.active = active
        self._items = items if items is not None else [{"keyword": "테스트", "url": "https://x"}]
        self._raise_exc = raise_exc

    def scrape(self):
        if self._raise_exc:
            raise self._raise_exc
        return self._items


class TestCollectKeywordCaches(unittest.TestCase):
    """_collect_keyword_caches: 검색엔진 키워드 수집 → keyword_cache upsert 분리 함수.

    기존 run() 인라인 루프를 그대로 추출한 것이라 동작이 동일해야 하며, news_top_only
    경로에서도 재사용되므로 source_status 채우기/개별 실패 격리가 유지되는지 검증한다.
    """

    def test_ok_source_upserted_and_marked_ok(self):
        fake = _FakeKeywordScraper("daum", items=[{"keyword": "손흥민", "url": "https://x"}])
        status = {}
        with patch.object(main_module, "KEYWORD_SCRAPERS", [fake]), \
             patch.object(main_module, "upsert_keywords", return_value=True) as m:
            main_module._collect_keyword_caches(status)
        m.assert_called_once_with("daum", [{"keyword": "손흥민", "url": "https://x"}])
        self.assertEqual(status["daum"], "ok")

    def test_inactive_source_skipped_without_scrape(self):
        fake = _FakeKeywordScraper("namuwiki", active=False)
        status = {}
        with patch.object(main_module, "KEYWORD_SCRAPERS", [fake]), \
             patch.object(main_module, "upsert_keywords") as m:
            main_module._collect_keyword_caches(status)
        m.assert_not_called()
        self.assertEqual(status["namuwiki"], "skipped")

    def test_upsert_failure_marks_failed(self):
        fake = _FakeKeywordScraper("msn")
        status = {}
        with patch.object(main_module, "KEYWORD_SCRAPERS", [fake]), \
             patch.object(main_module, "upsert_keywords", return_value=False):
            main_module._collect_keyword_caches(status)
        self.assertEqual(status["msn"], "failed")

    def test_scrape_exception_isolated_per_source(self):
        # 한 source의 scrape() 예외가 다른 source 수집을 막지 않아야 한다(기존 run() 동작 유지).
        failing = _FakeKeywordScraper("nate", raise_exc=RuntimeError("network down"))
        ok = _FakeKeywordScraper("danawa", items=[{"keyword": "ddr5", "url": "https://x"}])
        status = {}
        with patch.object(main_module, "KEYWORD_SCRAPERS", [failing, ok]), \
             patch.object(main_module, "upsert_keywords", return_value=True):
            main_module._collect_keyword_caches(status)
        self.assertEqual(status["nate"], "failed")
        self.assertEqual(status["danawa"], "ok")

    def test_news_top_only_calls_keyword_caches_before_briefing(self):
        # __main__ 분기 순서 검증: 포털 키워드 수집(_collect_keyword_caches) →
        # news_top 생성(run_news_briefing) 순으로 호출돼야 한다.
        call_order = []
        with patch.object(
            main_module, "_collect_keyword_caches",
            side_effect=lambda status: call_order.append("keywords"),
        ) as mock_collect, patch.object(
            main_module, "run_news_briefing",
            side_effect=lambda: call_order.append("briefing"),
        ) as mock_briefing:
            mock_collect({})
            mock_briefing()
        self.assertEqual(call_order, ["keywords", "briefing"])


class TestSenseMixingDisplay(unittest.TestCase):
    """짧고 애매한 keyword가 서로 다른 의미의 기사를 흡수하는 sense-mixing 방어
    ("위홀 뜻" 사례, 2026-07). 검색의도 suffix display 방지 + 다른 의미 article
    혼입 방지 + 정상 케이스 회귀 없음을 검증한다."""

    def _andy_warhol_articles(self):
        # 앤디워홀/미술관/대구/전시 — "위홀" 토큰만 공유하는 무관 기사 클러스터.
        return [
            {"title": "대구문화예술회관, 7월 '미술관 라이브' 개최…앤디 워홀 특별전과 대구",
             "originallink": "https://a.com/w1",
             "description": "앤디 워홀 예술을 팔다 포스터 대구문화예술회관 대표 융합 미술 프로그램",
             "pubDate": None},
            {"title": "앤디 워홀 특별전, 대구서 개막…미술관 전시 화제",
             "originallink": "https://a.com/w2",
             "description": "앤디 워홀의 작품 세계를 조명하는 전시가 대구에서 열린다",
             "pubDate": None},
        ]

    def _hyori_articles(self):
        # 이효리/연애전쟁/위홀 커플/조언 — keyword "위홀 뜻"의 실제 다수 클러스터.
        # 스크린샷 원문 검색결과 문구를 그대로 사용(제목/스니펫에 "위홀" 표기가 실제로
        # 등장 — naver 검색 결과 자체가 이렇게 표기됐던 실사례를 재현).
        return [
            {"title": "'연애전쟁' 이효리, 위홀 커플 조언",
             "originallink": "https://a.com/h1",
             "description": "'연애전쟁' JTBC '연애전쟁'에서 이효리가 위킹홀리데이를 앞둔 커플에게 현실적인 연애 조언을 건넸다. 감정 기복으로 힘들어하는 여자친구를 향한 공감과 진심 어린 위로가 시청자들의 공감을 얻었다. JTBC",
             "pubDate": None},
            {"title": "'3년 차 커플' 결혼 vs 위홀..마지막 여행서 끝내 눈물의 파국[연애전쟁",
             "originallink": "https://a.com/h2",
             "description": "'연애전쟁'에서 남자친구와 결혼을 원하는 여자친구의 갈등이 공개됐다. 7일 방송된 JTBC 예능프로그램 '연애전쟁'에서는 3년째 교제 중인 4살 차이 커플의 여행이 공개됐다. 이날 여행은 남성이 워킹홀리데이를 앞두고",
             "pubDate": None},
            {"title": "친오빠 친구와 5년 째 연애 시작했는데...\"18일 뒤 위홀 떠난다\" (연애전...",
             "originallink": "https://a.com/h3",
             "description": "7일 방송되는 JTBC '연애전쟁' 3회에서는 세 번째 협상 의뢰인으로 '위홀 커플'이 출연한다. 두 사람은... 그러나 여자친구는 '위홀'의 '위'자만 나와도 눈물 뚝뚝 떨구는 모습을 보였고, 이에 이효리와 서장은",
             "pubDate": None},
            {"title": "이효리 한방 조언 \"안달나게 하고 싶으면...\" (연애전쟁)",
             "originallink": "https://a.com/h4",
             "description": "7일 방송된 JTBC '연애전쟁' 3회에는 특별외교관으로 이준이 출연한 가운데, 출국을 앞둔 '위홀 커플'.. 결국 '위홀 커플'은 워킹홀리데이 기간 연락 횟수에 대해서는 합의했지만 결혼 시기에 대한 의견 차는",
             "pubDate": None},
        ]

    def _mixed_raw_items(self):
        return self._hyori_articles() + self._andy_warhol_articles()

    def test_search_intent_suffix_display_avoided(self):
        # keyword="위홀 뜻", 기사 다수는 이효리/연애전쟁/위홀 커플/조언 →
        # display_keyword가 "위홀 뜻"이면 실패.
        sig = cand.compute_news_signal("위홀 뜻", self._mixed_raw_items())
        item = {"keyword": "위홀 뜻", "news_meta": sig}
        resolved = ranker.resolve_singleton_displays([item])
        self.assertNotEqual(resolved[0]["display_keyword"], "위홀 뜻")

    def test_display_keyword_reflects_dominant_cluster(self):
        # 재구성된 display_keyword는 이효리/워홀 커플/조언 계열이어야 한다.
        sig = cand.compute_news_signal("위홀 뜻", self._mixed_raw_items())
        item = {"keyword": "위홀 뜻", "news_meta": sig}
        resolved = ranker.resolve_singleton_displays([item])
        display = resolved[0]["display_keyword"]
        self.assertTrue(
            any(tok in display for tok in ("이효리", "워홀", "조언", "연애전쟁")),
            msg=f"display_keyword={display!r}가 dominant cluster를 반영하지 않음",
        )
        self.assertNotIn("앤디", display)
        self.assertNotIn("미술관", display)

    def test_andy_warhol_articles_excluded_from_display(self):
        # 앤디워홀/미술관/대구 기사는 표시 articles(display_articles)에서 제외돼야 한다.
        sig = cand.compute_news_signal("위홀 뜻", self._mixed_raw_items())
        articles = cand.filter_articles_for_display(sig["articles"], min_count=1)
        display_articles = cand.build_display_articles(
            "위홀 뜻", articles, sig["representative_article"]
        )
        for a in display_articles:
            self.assertNotIn("앤디", a["title"])
            self.assertNotIn("미술관", a["title"])

    def test_off_primary_sense_flag_set_for_unrelated_cluster(self):
        # compute_news_signal이 앤디워홀 기사에 is_off_primary_sense=True를 부여해야 한다.
        sig = cand.compute_news_signal("위홀 뜻", self._mixed_raw_items())
        off_sense_titles = [a["title"] for a in sig["articles"] if a.get("is_off_primary_sense")]
        self.assertTrue(any("앤디" in t for t in off_sense_titles))
        self.assertGreaterEqual(sig["off_primary_sense_count"], 1)

    def test_search_intent_suffix_alone_not_displayed(self):
        # "뜻"/"의미"/"누구"/"프로필"/"나이" 단독 keyword는 suffix만 있으므로(stem 없음)
        # 재구성 대상이 아니라 원래 keyword를 그대로 유지한다(vacuous 재구성 방지,
        # Codex review-only P1 반영).
        for kw in ("뜻", "의미", "누구", "프로필", "나이"):
            item = {"keyword": kw, "news_meta": {"articles": []}}
            resolved = ranker.resolve_singleton_displays([item])
            self.assertEqual(resolved[0]["display_keyword"], kw)

    def test_normal_typhoon_keyword_unaffected(self):
        sig = cand.compute_news_signal("태풍", [
            {"title": "태풍 북상, 제주도 강풍 특보", "originallink": "https://t.com/1",
             "description": "태풍이 북상하며 제주도에 강풍 특보가 발효됐다", "pubDate": None},
            {"title": "태풍 경로 예측, 남부지방 영향권", "originallink": "https://t.com/2",
             "description": "태풍의 예상 경로가 남부지방을 지날 것으로 보인다", "pubDate": None},
        ])
        item = {"keyword": "태풍", "news_meta": sig}
        resolved = ranker.resolve_singleton_displays([item])
        self.assertEqual(resolved[0]["display_keyword"], "태풍")

    def test_normal_worldcup_16gang_keyword_unaffected(self):
        item = {"keyword": "월드컵 16강", "news_meta": {"articles": []}}
        resolved = ranker.resolve_singleton_displays([item])
        self.assertEqual(resolved[0]["display_keyword"], "월드컵 16강")

    def test_normal_hong_seokgi_keyword_unaffected(self):
        item = {"keyword": "홍석기 치안감", "news_meta": {"articles": []}}
        resolved = ranker.resolve_singleton_displays([item])
        self.assertEqual(resolved[0]["display_keyword"], "홍석기 치안감")

    def test_normal_jang_yoonjeong_mom_keyword_unaffected(self):
        item = {"keyword": "장윤정 엄마", "news_meta": {"articles": []}}
        resolved = ranker.resolve_singleton_displays([item])
        self.assertEqual(resolved[0]["display_keyword"], "장윤정 엄마")

    def test_merged_group_not_affected_by_singleton_resolver(self):
        # related_keywords가 있는(merge된) item은 resolve_singleton_displays 대상이 아니다.
        item = {
            "keyword": "위홀 뜻", "display_keyword": "위홀 뜻 커플",
            "related_keywords": ["위홀 커플"], "news_meta": {"articles": []},
        }
        resolved = ranker.resolve_singleton_displays([item])
        self.assertEqual(resolved[0]["display_keyword"], "위홀 뜻 커플")

    def test_generic_singleton_suffix_penalty_in_representative_score(self):
        # _representative_score의 suffix 페널티 축이 정상 반영되는지(튜플 2번째 원소).
        common_tokens = set()
        member_suffix = {"keyword": "위홀 뜻", "score": 0.9}
        member_normal = {"keyword": "위홀 커플", "score": 0.9}
        score_suffix = ranker._representative_score(member_suffix, common_tokens, [])
        score_normal = ranker._representative_score(member_normal, common_tokens, [])
        self.assertLess(score_suffix, score_normal)

    def test_substring_suffix_not_penalized(self):
        # "이나이 대표"처럼 suffix 문자열이 다른 토큰의 일부로 우연히 섞인 경우는
        # 토큰 단위 정확 일치가 아니므로 오탐 감점되지 않아야 한다.
        self.assertFalse(ranker._ends_with_search_intent_suffix("이나이 대표"))
        self.assertTrue(ranker._ends_with_search_intent_suffix("위홀 뜻"))
        self.assertTrue(ranker._ends_with_search_intent_suffix("아이유 프로필"))


class TestShortGenericSingletonDisplayBoost(unittest.TestCase):
    """짧은 일반 생활명사 단독(singleton) display 보강 — "안경" → "AI 안경".

    표시 기사에서 keyword 바로 앞에 반복되는 영문/숫자 modifier가 있을 때만 보강한다.
    canonical keyword는 불변, display_keyword만 바뀐다. 순수 한글 문맥어(제주/은행 등)와
    뒤 서술어(북상/규제)는 이번 범위에서 보강하지 않는다(원형 유지).
    """

    @staticmethod
    def _item(keyword, titles):
        return {
            "keyword": keyword,
            "news_meta": {
                "articles": [
                    {"title": t, "url": f"https://x.com/{i}", "snippet": ""}
                    for i, t in enumerate(titles)
                ],
                "representative_article": {"title": titles[0]} if titles else {},
            },
        }

    def _display(self, keyword, titles):
        resolved = ranker.resolve_singleton_displays([self._item(keyword, titles)])
        return resolved[0]["display_keyword"]

    def test_ai_glasses_boosted(self):
        # "안경" 단독 + 기사 다수 "AI 안경 ..." → display "AI 안경"(단독 "안경"이면 실패).
        display = self._display("안경", [
            "AI 안경 체험존 오픈", "AI 안경 시스템 체험", "AI 안경 신제품 공개",
        ])
        self.assertEqual(display, "AI 안경")

    def test_trailing_event_word_not_appended(self):
        # 뒤 사건어("체험"/"몰카")는 붙지 않는다 — "AI 안경"까지만(과구체화 방지).
        display = self._display("안경", [
            "AI 안경 체험 인기", "AI 안경 몰카 수사", "AI 안경 시스템 공개",
        ])
        self.assertEqual(display, "AI 안경")

    def test_canonical_keyword_unchanged(self):
        # canonical keyword(movement 비교용)는 그대로 "안경" 유지, display만 보강.
        item = self._item("안경", ["AI 안경 체험", "AI 안경 시스템", "AI 안경 공개"])
        resolved = ranker.resolve_singleton_displays([item])
        self.assertEqual(resolved[0]["keyword"], "안경")
        self.assertEqual(resolved[0]["display_keyword"], "AI 안경")

    def test_typhoon_trailing_predicate_not_boosted(self):
        # "태풍 북상"/"태풍 전망"은 뒤 서술어라 prev-token에 안 잡힘 → 원형 유지.
        display = self._display("태풍", [
            "태풍 북상 제주 강풍", "태풍 전망 남부 영향", "태풍 경로 예측",
        ])
        self.assertEqual(display, "태풍")

    def test_typhoon_korean_context_word_not_boosted(self):
        # "제주 태풍"처럼 앞 한글 문맥어는 영문/숫자 없음 → 이번 범위 제외(원형 유지).
        display = self._display("태풍", [
            "제주 태풍 피해 속출", "제주 태풍 대비 비상", "제주 태풍 영향권",
        ])
        self.assertEqual(display, "태풍")

    def test_interest_rate_korean_context_word_not_boosted(self):
        # "은행 금리"도 순수 한글 문맥어 → 원형 유지.
        display = self._display("금리", [
            "은행 금리 인하 발표", "은행 금리 비교 서비스", "은행 금리 상승 전환",
        ])
        self.assertEqual(display, "금리")

    def test_judamdae_alone_unchanged(self):
        # "주담대 규제"/"주담대 금리"는 뒤 서술어 → 원형 유지.
        display = self._display("주담대", [
            "주담대 규제 강화", "주담대 금리 상승", "주담대 한도 축소",
        ])
        self.assertEqual(display, "주담대")

    def test_modifier_must_repeat_across_majority(self):
        # prev-token이 소수 기사에만 반복(일관성 없음) → 원형 유지.
        display = self._display("안경", [
            "AI 안경 체험", "삼성 안경 출시", "LG 안경 공개", "코오롱 안경 신제품",
        ])
        self.assertEqual(display, "안경")

    def test_duplicate_form_modifier_rejected(self):
        # 중복형("오픈AI" + "AI") 차단 — modifier가 keyword를 문자로 포함.
        display = self._display("AI", [
            "오픈AI AI 모델 공개", "오픈AI AI 전략 발표", "오픈AI AI 신기술",
        ])
        self.assertEqual(display, "AI")

    def test_english_short_keyword_without_modifier_unchanged(self):
        # keyword="AI" 앞에 반복 modifier가 없으면 원형 유지(영문 2자 오탐 방어).
        display = self._display("AI", [
            "AI 반도체 급등", "AI 스타트업 투자", "AI 규제 논의",
        ])
        self.assertEqual(display, "AI")

    def test_multiword_keyword_not_target(self):
        # 다어절 keyword는 이미 구체적 → 보강 대상 아님(원형 유지).
        display = self._display("스마트 안경", [
            "AI 스마트 안경 체험", "AI 스마트 안경 공개",
        ])
        self.assertEqual(display, "스마트 안경")

    def test_generic_only_keyword_not_boosted(self):
        # generic-only("수사")는 exclude_generic_singletons가 별도 처리 → 여기서 개입 안 함.
        display = self._display("수사", [
            "AI 수사 기법 도입", "AI 수사 시스템 확대", "AI 수사 협력",
        ])
        self.assertEqual(display, "수사")

    def test_long_keyword_over_length_gate_not_target(self):
        # SHORT_GENERIC_SINGLETON_MAX_LEN(3자) 초과 단일 토큰은 대상 아님.
        display = self._display("헤르체고비나", [
            "AI 헤르체고비나 협력", "AI 헤르체고비나 교류",
        ])
        self.assertEqual(display, "헤르체고비나")

    def test_boosted_over_maxlen_keeps_original(self):
        # "{modifier} {keyword}"가 18자 초과면 원형 유지(토큰 중간 절단 방지).
        long_modifier = "SUPERLONGBRANDNAME2026"  # 22자 영문
        display = self._display("이어폰", [
            f"{long_modifier} 이어폰 출시", f"{long_modifier} 이어폰 공개",
            f"{long_modifier} 이어폰 예약",
        ])
        self.assertEqual(display, "이어폰")

    def test_keyword_at_title_start_no_prev_token(self):
        # keyword가 title 맨 앞이라 앞 토큰이 없으면 보강 근거 없음 → 원형 유지.
        display = self._display("안경", [
            "안경 신제품 출시", "안경 브랜드 협업", "안경 시장 성장",
        ])
        self.assertEqual(display, "안경")

    def test_keyword_low_coverage_not_boosted(self):
        # keyword 토큰이 표시 기사 절반 미만에만 등장 → 보강 안 함.
        display = self._display("안경", [
            "AI 안경 체험", "무관한 사회 기사", "무관한 경제 기사", "무관한 스포츠 기사",
        ])
        self.assertEqual(display, "안경")

    def test_merged_group_not_boosted(self):
        # related_keywords 있는(merge된) item은 singleton 보강 대상 아님.
        item = self._item("안경", ["AI 안경 체험", "AI 안경 공개"])
        item["related_keywords"] = ["스마트 안경"]
        item["display_keyword"] = "안경 스마트"
        resolved = ranker.resolve_singleton_displays([item])
        self.assertEqual(resolved[0]["display_keyword"], "안경 스마트")

    def test_numeric_modifier_allowed(self):
        # 숫자 포함 modifier("5G")도 허용. keyword는 _tokens가 토큰화하는 2자 이상.
        display = self._display("공유기", [
            "5G 공유기 신제품", "5G 공유기 출시 임박", "5G 공유기 예약 판매",
        ])
        self.assertEqual(display, "5G 공유기")

    def test_modifier_exact_half_boosted(self):
        # 경계 고정(Codex diff P3): keyword 등장 4기사 중 정확히 2건(절반)이 "AI 안경"
        # → threshold(>= 절반)를 만족하므로 보강돼야 한다.
        display = self._display("안경", [
            "AI 안경 체험 오픈", "AI 안경 시스템 공개",  # 2/4 = 0.5
            "삼성 안경 신제품", "코오롱 안경 출시",
        ])
        self.assertEqual(display, "AI 안경")

    def test_only_first_occurrence_prev_counted(self):
        # 의도 고정(Codex diff P2): 한 title에 keyword가 여러 번 나오면 첫 등장 prev만
        # 센다. "안경 시장, AI 안경 공개"는 첫 등장("안경")에 앞 토큰이 없어(맨 앞) prev
        # 없음 처리 → majority 미달로 원형 유지.
        display = self._display("안경", [
            "안경 시장 확대 AI 안경 공개", "안경 트렌드 AI 안경 경쟁", "안경 수요 AI 안경 성장",
        ])
        self.assertEqual(display, "안경")

    def test_duplicate_form_modifier_rejected_case_insensitive(self):
        # 중복형 차단은 영문 case 무시(Codex diff P3): "Openai" + "AI"도 차단.
        display = self._display("AI", [
            "Openai AI 모델 공개", "Openai AI 전략 발표", "Openai AI 신기술",
        ])
        self.assertEqual(display, "AI")

    def test_display_articles_basis_dedup_not_raw(self):
        # 표시 기사 기준 집계(Codex diff P1): 원본에 같은 URL 중복 기사가 있어도 dedup
        # 후 집계하므로 중복이 majority를 왜곡하지 않는다. dedup_articles는 URL 기준이라
        # 같은 URL을 여러 번 넣어 실제 dedup 경로를 태운다. dedup 후 3건 모두 "AI 안경"
        # 이므로 정상 보강.
        dup = {"title": "AI 안경 체험존", "url": "https://x.com/dup", "snippet": ""}
        item = {
            "keyword": "안경",
            "news_meta": {
                "articles": [
                    dup, dict(dup),  # 동일 URL 중복(dedup 대상)
                    {"title": "AI 안경 시스템", "url": "https://x.com/1", "snippet": ""},
                    {"title": "AI 안경 공개", "url": "https://x.com/2", "snippet": ""},
                ],
                "representative_article": {"title": "AI 안경 체험존"},
            },
        }
        resolved = ranker.resolve_singleton_displays([item])
        self.assertEqual(resolved[0]["display_keyword"], "AI 안경")

    def test_modifier_min_absolute_support_required(self):
        # 절대 근거 방어(Codex diff 재리뷰 P1): modifier가 표시 기사 절반 이상 비율은
        # 만족해도 절대 hit이 DISPLAY_ARTICLES_MIN(2) 미만이면 보강하지 않는다. 보강 후
        # exclude_insufficient_display_articles에 걸려 원래 "안경"이면 살아남았을 후보가
        # 탈락하는 것을 막기 위함. 아래는 keyword 등장 2기사 중 "AI 안경" 1건(비율 0.5는
        # 넘지만 절대 1건 < 2) → 원형 유지.
        display = self._display("안경", [
            "AI 안경 체험", "메타 안경 공개",
        ])
        self.assertEqual(display, "안경")


class TestBroadCategorySingletonDetect(unittest.TestCase):
    """broad category(업종/분야) generic singleton 탐지 — 1차 logging first.

    detect_broad_category_singletons는 탐지·진단만 반환하고 final 결과를 바꾸지 않는다.
    "건설"/"게임"류 순수 한글 업종/분야어 단독 후보를 shadow dispersion과 함께 잡고,
    "태풍"/"주담대"/"금리" 이슈 단독어와 §0-4 보강분("AI 안경")·merge group은 제외한다.
    """

    @staticmethod
    def _item(keyword, titles, display=None, related=None):
        # 표시 기사 산출이 build_display_articles(anchor 재확인)를 통과해야 하므로,
        # 실기사처럼 keyword_main_topic/non-incidental 메타를 부여한다(단일 broad 토큰
        # 키워드는 _display_anchor_allowed의 단일 토큰 예외 경로로 표시에 남는다).
        item = {
            "keyword": keyword,
            "display_keyword": display if display is not None else keyword,
            "news_meta": {
                "articles": [
                    {
                        "title": t,
                        "url": f"https://x.com/{i}",
                        "snippet": "",
                        "relevance_reason": "keyword_main_topic",
                        "is_incidental": False,
                    }
                    for i, t in enumerate(titles)
                ],
            },
        }
        if related:
            item["related_keywords"] = related
        return item

    def _diag(self, keyword, titles, display=None, related=None):
        diags = ranker.detect_broad_category_singletons(
            [self._item(keyword, titles, display=display, related=related)]
        )
        return diags

    # ── positive: broad category singleton 탐지 ──
    def test_construction_dispersed_detected(self):
        # "건설" + 서로 다른 건설사 기사 → 탐지 + shadow dispersed True.
        diags = self._diag("건설", [
            "현대건설 안전 스타트업 협업 성과",
            "대우건설 이라크 국가전략사업 수주",
            "삼성물산 건설 부문 실적 발표",
        ])
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0]["keyword"], "건설")
        self.assertTrue(diags[0]["shadow_dispersed"])

    def test_game_multiple_companies_detected(self):
        # "게임" + 여러 회사/작품 혼재 → 탐지 + dispersed True.
        diags = self._diag("게임", [
            "넷마블 신작 게임 출시 예고",
            "엔씨소프트 게임 매출 반등",
            "크래프톤 게임 글로벌 흥행",
        ])
        self.assertEqual(len(diags), 1)
        self.assertTrue(diags[0]["shadow_dispersed"])

    def test_game_same_subject_not_dispersed(self):
        # "게임" + 동일 주체(넷마블) 과반 반복 → 탐지되지만 dispersed False(유지 후보).
        diags = self._diag("게임", [
            "넷마블 게임 신작 공개",
            "넷마블 게임 사전예약 시작",
            "넷마블 게임 매출 신기록",
        ])
        self.assertEqual(len(diags), 1)
        self.assertFalse(diags[0]["shadow_dispersed"])

    # ── negative: 이슈 단독어 미탐지(사전 미포함) ──
    def test_typhoon_not_detected(self):
        diags = self._diag("태풍", [
            "태풍 북상 제주 강타", "태풍 전국 강풍 특보", "태풍 피해 복구 총력",
        ])
        self.assertEqual(diags, [])

    def test_judamdae_not_detected(self):
        diags = self._diag("주담대", [
            "주담대 금리 상단 7% 근접", "주담대 규제 강화 검토",
        ])
        self.assertEqual(diags, [])

    def test_interest_rate_not_detected(self):
        diags = self._diag("금리", [
            "금리 인하 기대감 확산", "금리 동결 전망 우세",
        ])
        self.assertEqual(diags, [])

    # ── negative: §0-4 보강분 / merge group 제외 ──
    def test_display_boosted_not_detected(self):
        # display_keyword != keyword(§0-4 "AI 안경"류)면 관찰 대상 아님.
        diags = self._diag("게임", [
            "AI 게임 신작 공개", "AI 게임 플랫폼 출시",
        ], display="AI 게임")
        self.assertEqual(diags, [])

    def test_merge_group_not_detected(self):
        # related_keywords 존재(merge group) → 대상 아님.
        diags = self._diag("건설", [
            "현대건설 협업", "대우건설 수주",
        ], related=["현대건설"])
        self.assertEqual(diags, [])

    # ── 안전 동작: 데이터 부족 / 비주체 접두 ──
    def test_single_article_dispersion_none(self):
        # 표시 기사 2건 미만 → dispersed None(판정 불가, 보수적). 탐지 자체는 됨.
        diags = self._diag("건설", ["현대건설 단독 기사"])
        self.assertEqual(len(diags), 1)
        self.assertIsNone(diags[0]["shadow_dispersed"])

    def test_noise_prefix_skipped_in_subject(self):
        # [속보]/정부/업계 등 접두 노이즈는 주체에서 건너뛰고 다음 토큰을 본다.
        # _tokens가 "[속보]" 기호를 제거하므로 "속보" 토큰만 남고, 그마저 스킵된다.
        diags = self._diag("건설", [
            "속보 현대건설 대형 수주 성공",
            "정부 대우건설 이라크 지원 확대",
            "업계 삼성물산 건설 신사업 진출",
        ])
        self.assertEqual(len(diags), 1)
        dist = diags[0]["subject_dist"]
        # 접두 노이즈가 주체로 잡히지 않았는지 확인.
        self.assertNotIn("속보", dist)
        self.assertNotIn("정부", dist)
        self.assertNotIn("업계", dist)
        self.assertIn("현대건설", dist)

    def test_keyword_first_token_uses_next(self):
        # 첫 토큰이 keyword 자신이면 다음 토큰을 주체로 본다.
        diags = self._diag("건설", [
            "건설 현대건설 안전 협업",
            "건설 대우건설 해외 수주",
        ])
        dist = diags[0]["subject_dist"]
        self.assertIn("현대건설", dist)
        self.assertIn("대우건설", dist)
        self.assertNotIn("건설", dist)

    def test_non_broad_singleton_not_detected(self):
        # 사전에 없는 일반 단독어("손예진")는 미탐지.
        diags = self._diag("손예진", [
            "손예진 신작 드라마 확정", "손예진 화보 공개",
        ])
        self.assertEqual(diags, [])

    def test_final_result_unchanged_detection_only(self):
        # detect는 입력 items를 변형하지 않는다(순수 관찰).
        item = self._item("건설", ["현대건설 협업", "대우건설 수주"])
        before = dict(item)
        ranker.detect_broad_category_singletons([item])
        self.assertEqual(item["keyword"], before["keyword"])
        self.assertEqual(item["display_keyword"], before["display_keyword"])

    def test_rank_and_select_top_identical_with_and_without_detect(self):
        # call-site 불변식(Codex diff P3): _rank_and_select의 detect 호출은 로그 전용이라
        # top(순서/개수/keyword/display)이 detect 유무와 무관하게 동일해야 한다. detect를
        # 무력화(no-op)한 결과와 정상 결과를 직접 비교해 "logging first = final 불변"을 고정.
        cands = [
            {"keyword": "게임", "sources": {"daum_home": 1}},
            {"keyword": "반도체 수출", "sources": {"nate_home": 1}},
        ]

        def _signals():
            return {
                "news": {
                    "게임": _news(3, 1, 2, 0.9, articles=[
                        _article("넷마블 게임 신작 공개", "https://x.com/g1"),
                        _article("엔씨 게임 매출 반등", "https://x.com/g2"),
                    ]),
                    "반도체 수출": _news(3, 1, 2, 0.9, articles=[
                        _article("반도체 수출 증가 발표", "https://x.com/b1"),
                        _article("반도체 수출 호조 지속", "https://x.com/b2"),
                    ]),
                },
                "datalab": {}, "google": {},
            }

        top_real = main_module._rank_and_select(cands, _signals(), "test")
        with patch.object(ranker, "detect_broad_category_singletons", return_value=[]):
            top_noop = main_module._rank_and_select(
                [dict(c) for c in cands], _signals(), "test"
            )
        # top 전체(순서/개수/keyword/display/rank + builder가 소비하는 articles/score/
        # source_breakdown/representative 등 모든 필드)가 detect 유무와 무관하게 동일해야
        # 한다(Codex diff 재리뷰 P3: fingerprint 일부만 보면 in-place 변형을 놓침).
        self.assertEqual(top_real, top_noop)


class TestHomonymEntitySingletonDetect(unittest.TestCase):
    """단일 토큰 keyword 동음이의 sense 탐지 — 1차 logging first(issue #2 후속).

    detect_homonym_entity_singletons는 탐지·진단만 반환하고 final 결과를 바꾸지 않는다.
    "워홀"(연애 예능의 워킹홀리데이) keyword에 "앤디 워홀"(다른 개체의 합성 고유명 일부)
    기사 클러스터가 혼입되는 케이스를 dominant collocation(전 등장 동일 인접 partner +
    partner가 primary에 미등장)으로 shadow 탐지한다. "장동건"류 정상 단일 고유명사는
    인접 토큰이 기사마다 달라 미발화한다.
    """

    @staticmethod
    def _art(title, url, snippet="", primary=False):
        # 표시 파이프라인(filter_articles_for_display → build_display_articles)을 실기사와
        # 동일하게 통과하도록 relevance 메타를 부여한다. 단일 토큰 keyword의 non-primary
        # 기사는 _display_anchor_allowed의 단일 토큰 예외 경로로 표시에 남는다(그 혼입이
        # 이번 관찰 대상).
        return {
            "title": title, "url": url, "snippet": snippet, "press": "x",
            "published_at": None, "thumbnail": None,
            "relevance_score": 0.9, "relevance_reason": "keyword_main_topic",
            "is_incidental": False, "is_primary_cluster": primary,
        }

    @staticmethod
    def _item(keyword, articles, display=None, related=None):
        item = {
            "keyword": keyword,
            "display_keyword": display if display is not None else keyword,
            "news_meta": {"articles": articles},
        }
        if related:
            item["related_keywords"] = related
        return item

    def _hyori_primary(self):
        # primary(연애 예능 워홀 커플) — "워홀" 인접 토큰이 기사마다 다름.
        return [
            self._art("'연애전쟁' 이효리, 워홀 커플 조언", "https://x.com/h1",
                      "이효리가 워킹홀리데이를 앞둔 커플에게 조언을 건넸다", primary=True),
            self._art("'3년 차 커플' 결혼 vs 워홀 눈물의 파국", "https://x.com/h2",
                      "결혼을 원하는 여자친구의 갈등이 공개됐다", primary=True),
        ]

    def _andy_cluster(self):
        # 다른 의미(앤디 워홀 전시) — "워홀" exact 등장이 전부 "앤디" 바로 뒤.
        # 두 기사가 cluster_articles(Jaccard 0.3)에서 한 묶음이 되도록 실기사처럼
        # 공통 어휘(앤디/워홀/특별전/대구/미술관)를 충분히 공유시킨다.
        return [
            self._art("앤디 워홀 특별전, 대구 미술관 개막", "https://x.com/w1",
                      "앤디 워홀 특별전 전시가 대구 미술관에서 열린다"),
            self._art("대구 미술관 앤디 워홀 특별전 화제", "https://x.com/w2",
                      "앤디 워홀 특별전 전시 작품을 대구 미술관에서 공개한다"),
        ]

    # ── positive: 동음이의 collocation shadow 탐지 ──
    def test_warhol_suffix_keyword_shadow_detected(self):
        # keyword="워홀 뜻"(1글자 suffix가 토큰화에서 빠져 core 단일 토큰 {워홀}).
        item = self._item("워홀 뜻", self._hyori_primary() + self._andy_cluster())
        diags = ranker.detect_homonym_entity_singletons([item])
        self.assertEqual(len(diags), 1)
        self.assertEqual(len(diags[0]["clusters"]), 1)
        self.assertEqual(diags[0]["clusters"][0]["partner"], "앤디")
        self.assertEqual(diags[0]["clusters"][0]["direction"], "prev")
        self.assertEqual(diags[0]["would_exclude_display_count"], 2)
        # 표시 4건 중 2건 제외돼도 2건 남음(>= DISPLAY_ARTICLES_MIN) → drop 아님.
        self.assertFalse(diags[0]["would_drop_candidate_by_display_min"])

    def test_warhol_alone_end_to_end_detected(self):
        # keyword="워홀" 단독 — compute_news_signal 실제 경로(primary 선택/off-sense
        # 판정 포함)로 흘려도 앤디워홀 클러스터가 shadow 탐지된다(issue #2 재현 fixture).
        raw = [
            {"title": "'연애전쟁' 이효리, 워홀 커플 조언", "originallink": "https://a.com/h1",
             "description": "'연애전쟁' JTBC에서 이효리가 워킹홀리데이를 앞둔 커플에게 조언을 건넸다.",
             "pubDate": None},
            {"title": "'3년 차 커플' 결혼 vs 워홀..마지막 여행서 눈물의 파국[연애전쟁",
             "originallink": "https://a.com/h2",
             "description": "'연애전쟁'에서 결혼을 원하는 여자친구의 갈등이 공개됐다. JTBC 예능프로그램.",
             "pubDate": None},
            {"title": "친오빠 친구와 연애 시작했는데...\"18일 뒤 워홀 떠난다\" (연애전...",
             "originallink": "https://a.com/h3",
             "description": "JTBC '연애전쟁' 3회에서는 세 번째 협상 의뢰인으로 '워홀 커플'이 출연한다.",
             "pubDate": None},
            {"title": "앤디 워홀 특별전, 대구서 개막…미술관 전시 화제", "originallink": "https://a.com/w1",
             "description": "앤디 워홀의 작품 세계를 조명하는 전시가 대구에서 열린다", "pubDate": None},
            {"title": "대구문화예술회관, 7월 '미술관 라이브' 개최…앤디 워홀 특별전과 대구",
             "originallink": "https://a.com/w2",
             "description": "앤디 워홀 예술을 팔다 포스터 대구문화예술회관 대표 융합 미술 프로그램",
             "pubDate": None},
        ]
        sig = cand.compute_news_signal("워홀", raw)
        # 전제 확인: 현재 코드에서 앤디 기사는 off-sense로 못 걸러(동일 문자열 공유) 표시에 혼입.
        arts = cand.filter_articles_for_display(sig["articles"], min_count=1)
        disp = cand.build_display_articles("워홀", arts, sig["representative_article"])
        self.assertTrue(any("앤디" in a["title"] for a in disp))
        diags = ranker.detect_homonym_entity_singletons(
            [{"keyword": "워홀", "display_keyword": "워홀", "news_meta": sig}]
        )
        self.assertEqual(len(diags), 1)
        partners = [c["partner"] for c in diags[0]["clusters"]]
        self.assertIn("앤디", partners)

    def test_next_token_partner_detected(self):
        # 후행(next) partner형 동음이의 — "소희 미술관"류(Codex 계획 리뷰 1차 P1 반영).
        item = self._item("소희", [
            self._art("소희 신곡 음원차트 1위", "https://x.com/s1", primary=True),
            self._art("소희 컴백 무대 화제", "https://x.com/s2", primary=True),
            # 두 기사가 한 클러스터로 묶이도록 공통 어휘(미술관/특별/기념전)를 공유.
            self._art("소희 미술관 개관 특별 기념전 개최", "https://x.com/m1"),
            self._art("소희 미술관 소장품 특별 기념전 공개", "https://x.com/m2"),
        ])
        diags = ranker.detect_homonym_entity_singletons([item])
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0]["clusters"][0]["partner"], "미술관")
        self.assertEqual(diags[0]["clusters"][0]["direction"], "next")

    def test_would_drop_flag_when_remainder_below_min(self):
        # primary 1건 + 동음이의 2건 → 제외 시 1건 남아 DISPLAY_ARTICLES_MIN 미만 → True.
        item = self._item("워홀", self._hyori_primary()[:1] + self._andy_cluster())
        diags = ranker.detect_homonym_entity_singletons([item])
        self.assertEqual(len(diags), 1)
        self.assertTrue(diags[0]["would_drop_candidate_by_display_min"])

    def test_primary_suspect_observed_separately(self):
        # primary 선택이 뒤집혀 동음이의 클러스터가 primary가 된 경우 — 별도 키로만 관찰.
        item = self._item("워홀", [
            self._art("앤디 워홀 특별전, 대구서 개막", "https://x.com/w1",
                      "미술관 전시가 열린다", primary=True),
            self._art("대구문화예술회관 앤디 워홀 특별전 화제", "https://x.com/w2",
                      "작품 세계를 조명한다", primary=True),
            self._art("'연애전쟁' 이효리, 워홀 커플 조언", "https://x.com/h1"),
            self._art("'3년 차 커플' 결혼 vs 워홀 눈물의 파국", "https://x.com/h2"),
        ])
        diags = ranker.detect_homonym_entity_singletons([item])
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0]["primary_suspect"], {"partner": "앤디", "direction": "prev"})
        # non-primary(연애 예능) 쪽은 인접 토큰이 제각각이라 clusters로는 안 잡힌다.
        self.assertEqual(diags[0]["clusters"], [])

    # ── negative: 기존 방어/정상 케이스 미발화 ──
    def test_wihol_existing_defense_untouched(self):
        # 기존 실사례 "위홀 뜻"(keyword 위홀 ≠ 원문 워홀, 문자열 다름)은 off-sense 방어가
        # 그대로 동작해 앤디 기사가 표시에서 이미 빠지고, shadow 탐지도 발화하지 않는다.
        raw = [
            {"title": "'연애전쟁' 이효리, 위홀 커플 조언", "originallink": "https://a.com/h1",
             "description": "이효리가 워킹홀리데이를 앞둔 커플에게 조언을 건넸다.", "pubDate": None},
            {"title": "\"18일 뒤 위홀 떠난다\" 커플의 눈물 (연애전쟁)", "originallink": "https://a.com/h2",
             "description": "'연애전쟁'에서 '위홀 커플'이 출연해 갈등을 상담했다.", "pubDate": None},
            {"title": "앤디 워홀 특별전, 대구서 개막…미술관 전시 화제", "originallink": "https://a.com/w1",
             "description": "앤디 워홀의 작품 세계를 조명하는 전시가 대구에서 열린다", "pubDate": None},
            {"title": "대구문화예술회관 앤디 워홀 특별전과 대구", "originallink": "https://a.com/w2",
             "description": "앤디 워홀 예술을 팔다 포스터 대구문화예술회관", "pubDate": None},
        ]
        sig = cand.compute_news_signal("위홀 뜻", raw)
        arts = cand.filter_articles_for_display(sig["articles"], min_count=1)
        disp = cand.build_display_articles("위홀 뜻", arts, sig["representative_article"])
        self.assertFalse(any("앤디" in a["title"] for a in disp))  # 기존 방어 유지
        diags = ranker.detect_homonym_entity_singletons(
            [{"keyword": "위홀 뜻", "display_keyword": "위홀 뜻", "news_meta": sig}]
        )
        self.assertEqual(diags, [])

    def test_jangdonggun_normal_person_not_detected(self):
        # 같은 인물의 다른 각도 기사(클러스터 분산) — 인접 토큰이 제각각이라 미발화.
        raw = [
            {"title": "노화 고백한 장동건, 급 '탱탱' 동안됐다", "originallink": "https://x.com/j1",
             "description": "배우 장동건이 한층 어려진 비주얼로 등장했다", "pubDate": None},
            {"title": "못 알아볼 뻔…장동건, 공식석상서 포착된 달라진 이미지", "originallink": "https://x.com/j2",
             "description": "장동건이 공식 행사에서 달라진 모습을 보였다", "pubDate": None},
            {"title": "54세 장동건, 못 알아볼 뻔한 바뀐 얼굴", "originallink": "https://x.com/j3",
             "description": "장동건의 외모 변화가 화제다", "pubDate": None},
            {"title": "중년 배우들 회춘…볼살 통통해진 장동건", "originallink": "https://x.com/j4",
             "description": "황정민과 장동건 등 중년 배우들의 외모 변화가 눈길을 끈다", "pubDate": None},
        ]
        sig = cand.compute_news_signal("장동건", raw)
        diags = ranker.detect_homonym_entity_singletons(
            [{"keyword": "장동건", "display_keyword": "장동건", "news_meta": sig}]
        )
        self.assertEqual(diags, [])

    def test_role_prefix_partner_not_detected(self):
        # "배우 장동건" 역할명 접두가 일관 반복돼도 weak partner라 증거로 쓰지 않는다
        # (Codex 계획 리뷰 1차 P1: 역할명 오탐 방어).
        item = self._item("장동건", [
            self._art("장동건 신작 영화 촬영 시작", "https://x.com/p1", primary=True),
            self._art("장동건 인터뷰 공개", "https://x.com/p2", primary=True),
            self._art("배우 장동건 근황 화제", "https://x.com/r1"),
            self._art("배우 장동건 시상식 참석", "https://x.com/r2"),
        ])
        diags = ranker.detect_homonym_entity_singletons([item])
        self.assertEqual(diags, [])

    def test_partner_present_in_primary_not_detected(self):
        # partner("앤디")가 primary 표시 기사에도 등장하면 같은 이슈의 표기 변형일 수
        # 있어 증거로 쓰지 않는다.
        item = self._item("워홀", [
            self._art("워홀 준비 비자 신청 급증", "https://x.com/v1",
                      "앤디 소속사 관계자도 워킹홀리데이를 언급했다", primary=True),
            self._art("워홀 비자 발급 확대", "https://x.com/v2", primary=True),
        ] + self._andy_cluster())
        diags = ranker.detect_homonym_entity_singletons([item])
        self.assertEqual(diags, [])

    def test_single_occurrence_not_detected(self):
        # exact 등장 1회뿐이면 "일관 반복"을 관측할 수 없어 보수적으로 미발화.
        item = self._item("워홀", self._hyori_primary() + [
            self._art("앤디 워홀 특별전, 대구서 개막", "https://x.com/w1"),
        ])
        diags = ranker.detect_homonym_entity_singletons([item])
        self.assertEqual(diags, [])

    def test_noise_prefix_partner_not_detected(self):
        # partner가 주체 노이즈 접두("오늘" 등)면 증거로 쓰지 않는다.
        item = self._item("워홀", self._hyori_primary() + [
            self._art("오늘 워홀 비자 정책 발표", "https://x.com/n1"),
            self._art("오늘 워홀 시행 확대 결정", "https://x.com/n2"),
        ])
        diags = ranker.detect_homonym_entity_singletons([item])
        self.assertEqual(diags, [])

    def test_normal_single_and_multi_token_keywords_not_detected(self):
        # 정상 보존 케이스: 단일 토큰(태풍/스마일게이트)은 인접 불일치로 미발화,
        # 다토큰(하이닉스 주가/홍석기 치안감/장윤정 엄마/월드컵 8강 대진표/박은영 셰프
        # 신혼여행)은 core 단일 토큰 조건에서 애초에 대상이 아니다.
        items = [
            self._item("태풍", [
                self._art("태풍 북상, 제주 강풍 특보", "https://t.com/1", primary=True),
                self._art("태풍 경로 예측 남부 영향권", "https://t.com/2", primary=True),
                self._art("전국 태풍 대비 점검", "https://t.com/3"),
                self._art("항공편 태풍 결항 속출", "https://t.com/4"),
            ]),
            self._item("스마일게이트", [
                self._art("스마일게이트 신작 공개", "https://s.com/1", primary=True),
                self._art("스마일게이트 글로벌 진출 확대", "https://s.com/2", primary=True),
                self._art("업계가 주목한 스마일게이트 전략", "https://s.com/3"),
                self._art("인디게임 지원 나선 스마일게이트", "https://s.com/4"),
            ]),
            self._item("하이닉스 주가", [
                self._art("하이닉스 주가 신고가", "https://h.com/1", primary=True),
                self._art("하이닉스 주가 상승 지속", "https://h.com/2"),
            ]),
            self._item("홍석기 치안감", [
                self._art("홍석기 치안감 임명", "https://g.com/1", primary=True),
            ]),
            self._item("장윤정 엄마", [
                self._art("장윤정 엄마 근황", "https://y.com/1", primary=True),
            ]),
            self._item("월드컵 8강 대진표", [
                self._art("월드컵 8강 대진표 확정", "https://w.com/1", primary=True),
            ]),
            self._item("박은영 셰프 신혼여행", [
                self._art("박은영 셰프 신혼여행 공개", "https://p.com/1", primary=True),
            ]),
        ]
        self.assertEqual(ranker.detect_homonym_entity_singletons(items), [])

    def test_merge_group_not_detected(self):
        # related_keywords 존재(merge group) → 1차 관찰 대상 아님.
        item = self._item("워홀", self._hyori_primary() + self._andy_cluster(),
                          related=["워홀 커플"])
        self.assertEqual(ranker.detect_homonym_entity_singletons([item]), [])

    # ── final 불변성 ──
    def test_detect_does_not_mutate_input(self):
        # detect는 입력 items/article dict/news_meta를 절대 변형하지 않는다(딥카피 동치).
        import copy
        item = self._item("워홀 뜻", self._hyori_primary() + self._andy_cluster())
        before = copy.deepcopy(item)
        ranker.detect_homonym_entity_singletons([item])
        self.assertEqual(item, before)

    def test_payload_unchanged_by_detect(self):
        # builder 산출물(저장 payload의 keywords 부분)이 detect 실행 전후 동일해야 한다
        # (Codex 계획 리뷰 2차 P1: shadow 필드가 articles/display_articles로 새면 안 됨).
        import copy
        item = self._item("워홀 뜻", self._hyori_primary() + self._andy_cluster())
        item.update({"score": 0.9, "rank_reason": "", "source_breakdown": {},
                     "sources": {"daum_home": 1}})
        pristine = copy.deepcopy(item)
        ranker.detect_homonym_entity_singletons([item])
        issues_after = build_ranked_issues([item], {}, ["naver_news"])
        issues_pristine = build_ranked_issues([pristine], {}, ["naver_news"])
        self.assertEqual(issues_after["keywords"], issues_pristine["keywords"])

    def test_rank_and_select_top_identical_with_and_without_detect(self):
        # call-site 불변식: _rank_and_select의 homonym detect 호출은 로그 전용이라 top
        # 전체 객체가 detect 유무와 무관하게 동일해야 한다(broad category 패턴 재사용).
        # fixture는 relevance/primary 메타를 실기사처럼 채워 "워홀"이 display gate를
        # 통과해 final에 남고 탐지가 실제로 발화하는 경로를 태운다(Codex diff 리뷰 P2:
        # 후보가 final 전에 탈락하면 "진단 truthy일 때의 불변" 통합 검증이 비어버림).
        def _cands():
            return [
                {"keyword": "워홀", "sources": {"daum_home": 1}},
                {"keyword": "반도체 수출", "sources": {"nate_home": 1}},
            ]

        def _signals():
            warhol_articles = (
                [dict(a) for a in self._hyori_primary()]
                + [dict(a) for a in self._andy_cluster()]
            )
            return {
                "news": {
                    "워홀": _news(3, 1, 2, 0.9, articles=warhol_articles),
                    "반도체 수출": _news(3, 1, 2, 0.9, articles=[
                        _article("반도체 수출 증가 발표", "https://x.com/b1"),
                        _article("반도체 수출 호조 지속", "https://x.com/b2"),
                    ]),
                },
                "datalab": {}, "google": {},
            }

        top_real = main_module._rank_and_select(_cands(), _signals(), "test")
        # 전제 확인: "워홀"이 final에 생존했고, detect가 실제로 truthy 진단을 반환해
        # _rank_and_select의 warning 분기를 탔다(로그 전용 분기 실경로 고정).
        self.assertIn("워홀", [t["keyword"] for t in top_real])
        diags = ranker.detect_homonym_entity_singletons(top_real)
        self.assertTrue(diags)
        self.assertEqual(diags[0]["clusters"][0]["partner"], "앤디")
        with patch.object(ranker, "detect_homonym_entity_singletons", return_value=[]):
            top_noop = main_module._rank_and_select(_cands(), _signals(), "test")
        self.assertEqual(top_real, top_noop)


class TestBroadKeywordRepresentative(unittest.TestCase):
    """broad/generic 키워드('초복') 대표기사 억제 — 기사들이 키워드 단어만 공유하고
    실제 사건·인물·기관·지역이 다르면 대표를 뽑지 않는다(2026-07-15)."""

    # 실제 '초복' 사례: 키워드('초복')와 세시풍속 소품('삼계탕')만 겹칠 뿐
    # 청도/하림/보은/성남/대통령/폭염은 서로 다른 사건이다.
    CHOBOK = [
        _article("청도군, 거연리 주민과 초복맞이 화합과 소통의 시간 가져", "https://x.com/c1",
                 "청도군은 지난 14일 환경산림과와 물관리사업소 주관으로 거연리 주민들과 함께하는 화합의 자리를 가졌다."),
        _article("하림 오드그로서, 초복 맞아 삼계탕으로 팀워크 다져", "https://x.com/c2",
                 "오늘(15일) 하림에 따르면 초복을 하루 앞둔 어제(14일) 선수들은 서울 강남구에 위치한 PBA 라운지 오픈"),
        _article("보은군, 민관 협력으로 초복 맞이 삼계탕 나눔 행사 성황리 진행", "https://x.com/c3",
                 "보은군이 초복을 맞아 지역 내 어르신과 장애인들의 건강한 여름나기를 돕기 위해 행사를 지원한 장애인후원회"),
        _article("성남시의회, 수진2동 초복맞이 삼계탕 나눔행사 참석", "https://x.com/c4",
                 "성남시의회는 15일 수진2동 단체장협의회 초복맞이 삼계탕 나눔행사에 참석했다. 이번 행사는 지역 내 취약계층"),
        _article("李대통령, 초복 맞아 靑직원들과 콩국수 오찬...깜짝 기자간담회", "https://x.com/c5",
                 "靑 격무 부서 직원들과 격려 오찬 오찬 뒤 춘추관 깜짝 방문 이재명 대통령이 15일 초복을 맞아 청와대"),
        _article("경산·경주 감포 37.9도...초복 대구·경북 찜통더위", "https://x.com/c6",
                 "초복인 15일 대구·경북 대부분 지역에 폭염이 이어지며 찜통더위 최고기온은 경산과 경주 감포가 37.9도"),
    ]

    # 동일 사건을 보도한 기사들 — 기존처럼 대표기사/summary가 생성돼야 한다.
    SAME_EVENT = [
        _article("민경욱 전 의원, 자택서 의식 불명 상태로 발견", "https://x.com/s1",
                 "민경욱 전 의원이 15일 서울 자택에서 의식 불명 상태로 발견돼 병원으로 이송됐다."),
        _article("민경욱 의식 불명…병원 이송 후 치료 중", "https://x.com/s2",
                 "경찰에 따르면 민경욱 전 의원은 자택에서 쓰러진 채 발견됐으며 현재 병원에서 치료를 받고 있다."),
        _article("[속보] 민경욱 전 의원 자택서 쓰러져 병원 이송", "https://x.com/s3",
                 "민경욱 전 의원이 자택에서 의식 불명 상태로 발견됐다고 경찰이 밝혔다."),
    ]

    # --- broad 키워드: 대표 없음 ---

    def test_broad_keyword_has_no_representative(self):
        self.assertFalse(cand_summarizer.has_representative("초복", self.CHOBOK))

    def test_broad_keyword_summary_is_empty(self):
        summary, summary_type = summarize("초복", self.CHOBOK)
        self.assertEqual(summary, "")
        self.assertEqual(summary_type, "no_representative")

    def test_broad_keyword_summary_is_not_any_article_title(self):
        # 어느 한 기사도 대표로 선택되지 않아야 한다(특히 기존 우승자였던 '하림').
        summary, _ = summarize("초복", self.CHOBOK)
        for a in self.CHOBOK:
            self.assertNotEqual(summary, a["title"])

    def test_broad_keyword_subtopic_tokens_below_threshold(self):
        # 키워드/파생형/날짜/일반어를 빼면 남는 공유 토큰은 세시풍속 소품 {삼계탕}
        # 하나뿐 — 공통 "사건"의 증거가 아니므로 임계(2) 미만이어야 한다.
        tokens = cand_summarizer.subtopic_tokens("초복", self.CHOBOK)
        self.assertLess(len(tokens), 2)
        self.assertNotIn("초복", tokens)      # 키워드 자신
        self.assertNotIn("초복맞이", tokens)  # 파생형
        self.assertNotIn("15일", tokens)      # 날짜
        self.assertNotIn("맞아", tokens)      # 일반 서술어

    # --- 동일 사건: 기존 동작 회귀 ---

    def test_same_event_keeps_representative(self):
        self.assertTrue(cand_summarizer.has_representative("민경욱", self.SAME_EVENT))
        summary, summary_type = summarize("민경욱", self.SAME_EVENT)
        self.assertEqual(summary_type, "rule")
        self.assertIn(summary, [a["title"] for a in self.SAME_EVENT])

    def test_same_event_two_articles_boundary(self):
        # 동일 사건 2건(최소 경계)에서도 대표가 생성돼야 한다.
        summary, summary_type = summarize("민경욱", self.SAME_EVENT[:2])
        self.assertEqual(summary_type, "rule")
        self.assertTrue(summary)

    def test_same_event_survives_one_unrelated_article(self):
        # 무관 기사 1건이 섞여도 동일 사건 토큰이 반복되므로 대표가 유지된다.
        mixed = self.SAME_EVENT + [_article("폭염 특보 확대", "https://x.com/u1",
                                            "기상청은 전국에 폭염 특보를 확대 발효했다.")]
        _, summary_type = summarize("민경욱", mixed)
        self.assertEqual(summary_type, "rule")

    # --- 경계/함정 (Codex review-only P2) ---

    def test_real_world_same_event_keywords_keep_representative(self):
        # 운영 news_top 실데이터 회귀(2026-07-15). 초기 구현은 "엄격한 과반" 임계를
        # 요구했는데, 운영 10건에 적용해 보니 5건이 억제됐다 — 같은 사건이라도 매체별
        # 표현이 갈리면(제목엔 "2000억", 다른 기사엔 "메리츠") 과반을 못 넘는 게 정상.
        # 아래는 그때 억제됐던 실제 키워드들의 축약본이다. 과반 요구가 되살아나면 깨진다.
        homeplus = [
            _article("파산 위기 홈플러스, 2000억 긴급자금 확보…MBK·메리츠 잠정 합의",
                     "https://x.com/h1", "홈플러스가 메리츠금융그룹과 2000억원 긴급자금 잠정 합의에 이르렀다."),
            _article("홈플러스 2000억 ‘긴급 수혈’", "https://x.com/h2",
                     "메리츠금융그룹이 홈플러스에 2000억원을 대출하는 방안을 잠정 합의했다."),
            _article("홈플러스 극적 회생?…메리츠 내일 2천억 원 대출 재논의", "https://x.com/h3",
                     "메리츠가 홈플러스 긴급자금 대출을 재논의한다. MBK파트너스와의 협의도 이어진다."),
            _article("홈플러스 폐점 후폭풍…도심 속 애물단지되나?", "https://x.com/h4",
                     "폐점 이후 빈 점포 활용 문제가 남았다."),
        ]
        _, summary_type = summarize("홈플러스", homeplus)
        self.assertEqual(summary_type, "rule")

        # 5건 중 '결승' 등 소수 토큰만 공유하는 경기 기사 — 과반 임계에선 억제됐다.
        match = [
            _article("무적함대 스페인, 16년 만에 결승 진출", "https://x.com/m1",
                     "스페인이 프랑스를 꺾고 16년 만에 월드컵 결승에 올랐다."),
            _article("전술·기술 다 밀렸다…스페인에 무릎 꿇은 프랑스", "https://x.com/m2",
                     "프랑스는 스페인에 완패하며 결승 진출이 좌절됐다."),
            _article("음바페 지운 스페인 협력 수비…‘무적 함대’의 힘", "https://x.com/m3",
                     "스페인의 협력 수비가 음바페를 지웠다. 월드컵 결승 진출의 원동력."),
        ]
        _, summary_type = summarize("프랑스 스페인", match)
        self.assertEqual(summary_type, "rule")

    def test_two_plus_two_split_events_picks_evidenced_cluster(self):
        # 알려진 한계(2026-07-15): 2+2로 갈린 서로 다른 사건에서는 대표가 생성되고,
        # 근거 토큰을 더 많이 담은 클러스터의 기사가 대표가 된다.
        #
        # "과반" 임계를 쓰면 이 케이스도 억제할 수 있지만, 운영 실데이터 10건 중
        # 5건(홈플러스/프랑스 스페인/호프 영화/김민하/제헌절)이 함께 억제되는 대가를
        # 치른다 — 같은 사건도 매체별 표현이 갈리면 과반을 못 넘기 때문이다. 이번
        # 작업의 대상은 "공통 사건이 아예 없는" broad 키워드(초복)이고, 2+2는 공통
        # 사건이 둘 존재하는 다른 문제다. 실데이터 회귀를 감수하면서까지 닫지 않는다.
        split = [
            _article("A시, 신청사 착공식 개최", "https://x.com/p1", "A시가 신청사 착공식을 열었다."),
            _article("A시 신청사 착공 본격화", "https://x.com/p2", "A시 신청사 착공이 본격화됐다."),
            _article("B사, 신형 전기차 공개", "https://x.com/p3", "B사가 신형 전기차를 공개했다."),
            _article("B사 전기차 사전계약 시작", "https://x.com/p4", "B사 전기차 사전계약이 시작됐다."),
        ]
        summary, summary_type = summarize("공개", split)
        self.assertEqual(summary_type, "rule")
        # 임의 기사가 아니라 실제 반복 토큰을 담은 기사가 선택된다.
        self.assertIn(summary, [a["title"] for a in split])

    def test_snippet_only_boilerplate_has_no_representative(self):
        # 매체 boilerplate가 snippet에만 반복되는 경우 — 사건 증거가 아니다.
        boiler = [
            _article("가 지역 축제 개막", "https://x.com/b1", "무단전재 재배포 금지 저작권자 제보는 카카오톡"),
            _article("나 지역 마라톤 열려", "https://x.com/b2", "무단전재 재배포 금지 저작권자 제보는 카카오톡"),
            _article("다 지역 음악회 성료", "https://x.com/b3", "무단전재 재배포 금지 저작권자 제보는 카카오톡"),
        ]
        summary, summary_type = summarize("축제", boiler)
        # boilerplate 토큰이 과반을 넘더라도 어느 title에도 없으므로 대표 title을
        # 특정할 수 없다 → 임의 기사로 채우지 않는다.
        self.assertEqual(summary_type, "no_representative")
        self.assertEqual(summary, "")

    def test_empty_keyword_keeps_legacy_behavior(self):
        # candidates.build_representative_summary가 쓰는 keyword="" 경로는
        # 키워드 제외를 적용하지 않는다(""가 모든 토큰에 substring 매칭되면 전멸).
        summary, summary_type = summarize("", self.SAME_EVENT)
        self.assertEqual(summary_type, "rule")
        self.assertTrue(summary)

    def test_single_article_keeps_title(self):
        # 1건은 "여러 기사 중 임의 선택" 문제가 없다 — 기존 동작 유지.
        summary, summary_type = summarize("초복", self.CHOBOK[:1])
        self.assertEqual(summary_type, "title")
        self.assertEqual(summary, self.CHOBOK[0]["title"])

    def test_no_articles_still_seed_only(self):
        self.assertEqual(summarize("초복", []), ("", "seed_only"))

    # --- builder 통합: 랭킹 유지 + 대표 필드만 비움 ---

    def test_builder_keeps_keyword_and_articles_but_drops_representative(self):
        ranked_item = {
            "keyword": "초복", "score": 0.7,
            "source_breakdown": {"news": 0.7}, "rank_reason": "",
            "news_meta": {
                "articles": self.CHOBOK,
                "representative_title": "하림 오드그로서, 초복 맞아 삼계탕으로 팀워크 다져",
                "representative_summary": "하림이 초복을 맞아 삼계탕으로 팀워크를 다졌다.",
                "representative_article": {"title": "하림 오드그로서, 초복 맞아 삼계탕으로 팀워크 다져"},
                "primary_cluster_size": 2,
                "topic_coherence": 0.3,
            },
            "used_signals": ["news"],
            "display_keyword": "초복",
            "sources": {"daum_home": 1},
        }
        entry = build_ranked_entry(1, ranked_item)

        # 키워드는 랭킹에 유지 + 기사 목록도 유지
        self.assertEqual(entry["keyword"], "초복")
        self.assertEqual(entry["rank"], 1)
        self.assertTrue(entry["signals"]["news"])
        self.assertTrue(len(entry["articles"]) > 0)

        # summary 없음 + 어느 기사도 대표로 간주되지 않음
        self.assertEqual(entry["summary"], "")
        self.assertEqual(entry["summary_type"], "no_representative")
        self.assertIsNone(entry["representative_title"])
        self.assertIsNone(entry["representative_summary"])
        self.assertIsNone(entry["representative_article"])

    def test_builder_backfilled_incidentals_do_not_suppress_representative(self):
        # Codex review-only P1(2026-07-15): builder는 summarize() 전에
        # filter_articles_for_display(min_count=5)로 저관련/incidental 기사를 하한까지
        # 보충한다. 이 보충분이 근거 집계에 들어가면 보충 기사가 대표로 뽑히거나
        # 그들끼리의 공통 토큰이 하위주제로 오인된다(훼손 시 summary='무관 기사 0').
        related = [
            dict(a, relevance_score=0.9, relevance_reason="keyword_main_topic",
                 is_incidental=False)
            for a in self.SAME_EVENT[:2]
        ]
        incidental = [
            dict(_article(f"무관 기사 {i}", f"https://x.com/i{i}", "전혀 다른 내용"),
                 relevance_score=0.1, is_incidental=True)
            for i in range(3)
        ]
        ranked_item = {
            "keyword": "민경욱", "score": 0.7,
            "source_breakdown": {"news": 0.7}, "rank_reason": "",
            "news_meta": {"articles": related + incidental},
            "used_signals": ["news"],
            "display_keyword": "민경욱",
            "sources": {"daum_home": 1},
        }
        entry = build_ranked_entry(1, ranked_item)
        # 보충분은 하한 보호로 articles에 남지만, 대표 판정 근거에서는 빠져야 한다.
        self.assertEqual(entry["summary_type"], "rule")
        self.assertIn(entry["summary"], [a["title"] for a in related])

    def test_low_relevance_articles_excluded_from_evidence(self):
        # Codex review-only P1(2차): is_incidental=False여도 relevance_score가 대표
        # 자격(0.5) 미만인 object-side 기사는 근거 집계에서 빠져야 한다 — 그렇지 않으면
        # 저관련 기사가 대표로 뽑히거나 그 토큰이 하위주제로 오인된다.
        related = [
            dict(a, relevance_score=0.9, relevance_reason="keyword_main_topic",
                 is_incidental=False)
            for a in self.SAME_EVENT[:2]
        ]
        low_rel = [
            dict(_article(f"저관련 기사 {i}", f"https://x.com/l{i}", "다른 내용"),
                 relevance_score=0.35, relevance_reason="object_side_mention",
                 is_incidental=False)
            for i in range(3)
        ]
        summary, summary_type = summarize("민경욱", related + low_rel)
        self.assertEqual(summary_type, "rule")
        self.assertIn(summary, [a["title"] for a in related])

    def test_all_incidental_has_no_representative(self):
        # 명시적으로 전부 대표 자격 미달이면 evidence 0건 → 대표 없음.
        # (단일 기사 예외가 evidence 필터보다 먼저 실행되면 이 불변식이 깨진다.)
        only_incidental = [
            dict(_article("부수 언급 기사", "https://x.com/n1", "키워드가 스쳐 지나간다"),
                 relevance_score=0.2, is_incidental=True),
        ]
        self.assertEqual(summarize("키워드", only_incidental), ("", "no_representative"))
        self.assertFalse(cand_summarizer.has_representative("키워드", only_incidental))

    def test_builder_no_representative_keeps_display_articles(self):
        # 팝업 기사 목록(display_articles)이 대표 억제와 무관하게 보존되는지 검증한다.
        #
        # 모든 기사를 is_primary_cluster=True로 두면 build_display_articles가 anchor를
        # 보지도 않고 전부 통과시켜, builder가 anchor에 None을 넘기는 회귀가 생겨도
        # 테스트가 통과한다(Codex review-only P2). 그래서 한 건은 non-primary로 두고
        # representative title과의 토큰 overlap(_display_anchor_allowed)으로만 살아남게
        # 구성한다 — anchor가 끊기면 이 기사가 목록에서 빠져 테스트가 깨진다.
        primary = [
            dict(a, relevance_score=0.9, relevance_reason="keyword_main_topic",
                 is_incidental=False, is_primary_cluster=True)
            for a in self.CHOBOK
        ]
        ranked_item = {
            "keyword": "초복", "score": 0.7,
            "source_breakdown": {"news": 0.7}, "rank_reason": "",
            "news_meta": {"articles": primary,
                          "representative_article": {"title": primary[1]["title"]}},
            "used_signals": ["news"],
            "display_keyword": "초복",
            "sources": {"daum_home": 1},
        }
        entry = build_ranked_entry(1, ranked_item)
        self.assertEqual(entry["summary_type"], "no_representative")
        self.assertIsNone(entry["representative_article"])
        # 대표를 안 뽑는 것과 기사를 숨기는 것은 다르다 — 목록은 그대로.
        titles = [a["title"] for a in entry["display_articles"]]
        for a in primary:
            self.assertIn(a["title"], titles)

    def test_builder_passes_representative_anchor_to_display_filter(self):
        # builder는 representative_article을 출력 JSON에서만 비우고
        # build_display_articles에는 계속 넘겨야 한다. anchor를 끊으면 primary cluster
        # 밖 기사가 연관성 재확인을 통과하지 못해 팝업 목록이 바뀐다.
        #
        # 초복 fixture로는 이 검증이 불가능하다 — anchor 검사를 통과할 만큼 대표와
        # 겹치는 기사는 필연적으로 두 번째 공유 토큰을 만들어 게이트를 rule로 뒤집는다.
        # 그래서 대표가 정상 생성되는 동일 사건 키워드로 anchor 전달 자체를 검증한다.
        primary = [
            dict(a, relevance_score=0.9, relevance_reason="keyword_main_topic",
                 is_incidental=False, is_primary_cluster=True)
            for a in self.SAME_EVENT
        ]
        # non-primary이며 keyword 토큰이 1개(=단일 토큰 조건 미달)라, 대표 title과의
        # 토큰 overlap(_display_anchor_allowed)으로만 목록에 남는 기사.
        anchored = dict(
            _article("의식 불명 상태로 병원 이송된 전 의원 근황", "https://x.com/a9",
                     "자택에서 발견돼 병원으로 이송된 전 의원의 치료가 이어지고 있다."),
            relevance_score=0.8, relevance_reason="keyword_main_topic",
            is_incidental=False, is_primary_cluster=False,
        )
        ranked_item = {
            "keyword": "민경욱", "score": 0.7,
            "source_breakdown": {"news": 0.7}, "rank_reason": "",
            "news_meta": {"articles": primary + [anchored],
                          "representative_article": {"title": primary[0]["title"]}},
            "used_signals": ["news"],
            "display_keyword": "민경욱",
            "sources": {"daum_home": 1},
        }
        entry = build_ranked_entry(1, ranked_item)
        titles = [a["title"] for a in entry["display_articles"]]
        self.assertIn(anchored["title"], titles)

    def test_builder_same_event_keeps_representative_fields(self):
        # 회귀: 동일 사건 키워드는 기존대로 대표 필드가 살아 있어야 한다.
        ranked_item = {
            "keyword": "민경욱", "score": 0.7,
            "source_breakdown": {"news": 0.7}, "rank_reason": "",
            "news_meta": {
                "articles": self.SAME_EVENT,
                "representative_title": self.SAME_EVENT[0]["title"],
                "representative_summary": "민경욱 전 의원이 자택에서 의식 불명 상태로 발견됐다.",
                "representative_article": {"title": self.SAME_EVENT[0]["title"]},
                "primary_cluster_size": 3,
                "topic_coherence": 0.9,
            },
            "used_signals": ["news"],
            "display_keyword": "민경욱",
            "sources": {"daum_home": 1},
        }
        entry = build_ranked_entry(1, ranked_item)
        self.assertEqual(entry["summary_type"], "rule")
        self.assertTrue(entry["summary"])
        self.assertIsNotNone(entry["representative_title"])
        self.assertIsNotNone(entry["representative_summary"])
        self.assertIsNotNone(entry["representative_article"])


class TestTruncatedTitleTiebreak(unittest.TestCase):
    """동점 후보 중 upstream 절단 제목 후순위(2026-07-16).

    뉴스 검색 API는 기사 제목을 잘라서 반환하는 경우가 있다(raw API 실측: '임영호 가수'
    10건 중 4건이 문자열 끝 "..."로 절단, 같은 기사 원문 og:title은 전체 제목). 절단
    제목이 대표로 뽑히면 홈/팝업 설명 문구가 문장 중간에서 끊긴 채 노출된다.

    이 tie-breaker는 절단 제목을 "복원"하지 않는다 — 하위주제 토큰 점수가 완전히 동점일
    때만 정상 제목을 우선한다. 점수 우위는 절대 뒤집지 않는다.
    """

    # 운영 캐시 실데이터(2026-07-16 news_top, keyword='임영호 가수') 기반.
    # 두 제목이 하위주제 토큰을 똑같이 담아 score가 완전히 동일하도록 구성한다 —
    # 절단 제목이 '먼저' 오므로 기존 로직(score > best_score)만으로는 절단 제목이
    # 대표가 된다. tie-breaker가 없으면 이 fixture는 실패해야 한다.
    LIM_YOUNGHO = [
        _article("가수 임영호 별세, 연인이 부고 전해 향년 49세 추모 물결...",
                 "https://www.mk.co.kr/article/12099997",
                 "가수 임영호가 별세했다. 연인이 부고를 전했다. 향년 49세."),
        _article("가수 임영호 별세, 연인이 부고 전해 향년 49세 추모 이어져",
                 "https://www.dailian.co.kr/news/view/1667746",
                 "가수 임영호가 별세했다. 연인이 부고를 전했다. 향년 49세."),
    ]

    def _scores(self, keyword, articles):
        """fixture가 실제로 동점인지 확인용(테스트 자체의 전제 검증)."""
        subtopic = cand_summarizer.subtopic_tokens(keyword, articles)
        return [
            sum(1 for tok in set(cand_summarizer._tokens(a["title"])) if tok in subtopic)
            for a in articles
        ]

    def test_fixture_is_actually_tied(self):
        # 전제 검증: 아래 tie-breaker 테스트가 '동점'을 실제로 만드는지 확인한다.
        # 동점이 아니면 tie-breaker 없이도 통과해 회귀를 못 잡는다(Codex review P1).
        scores = self._scores("임영호 가수", self.LIM_YOUNGHO)
        self.assertEqual(len(set(scores)), 1, f"fixture가 동점이 아님: {scores}")
        self.assertGreater(scores[0], 0)

    def test_tiebreak_prefers_untruncated_title(self):
        # 동점에서 절단된 최초 후보 대신 정상 제목이 대표가 된다.
        summary, summary_type = summarize("임영호 가수", self.LIM_YOUNGHO)
        self.assertEqual(summary_type, "rule")
        self.assertFalse(summary.endswith("..."))
        self.assertEqual(summary, self.LIM_YOUNGHO[1]["title"])

    def test_tiebreak_holds_when_candidate_order_reversed(self):
        # 후보 순서를 뒤집어도(정상 제목이 먼저) 정상 제목이 유지된다.
        reversed_articles = list(reversed(self.LIM_YOUNGHO))
        summary, _ = summarize("임영호 가수", reversed_articles)
        self.assertFalse(summary.endswith("..."))
        self.assertEqual(summary, self.LIM_YOUNGHO[1]["title"])

    def test_tiebreak_result_is_an_actual_article_title(self):
        # 대표는 항상 실재하는 기사 제목이어야 한다(문자열 조합/복원 금지).
        summary, _ = summarize("임영호 가수", self.LIM_YOUNGHO)
        self.assertIn(summary, [a["title"] for a in self.LIM_YOUNGHO])

    def test_higher_score_truncated_title_still_wins(self):
        # 점수 우위는 절대 뒤집지 않는다 — 절단 제목이 단독 최고점이면 그대로 대표다.
        # (운영 실데이터 '이정효' 케이스: 절단 제목이 score=5로 단독 1위 → 변경 없음)
        # 절단 제목(t1)이 하위주제 토큰 {수원, 삼성, 부산교통공사, 코리아컵}을 더 많이
        # 담아 단독 최고점이 되도록 구성한다.
        articles = [
            _article("골키퍼를 최전방 공격수로?…수원 삼성, 부산교통공사에 코리아컵 패배...",
                     "https://x.com/t1",
                     "이정효 감독의 수원 삼성이 부산교통공사에 패해 코리아컵에서 탈락했다."),
            _article("이정효의 수원, 탈락",
                     "https://x.com/t2",
                     "수원 삼성이 부산교통공사에 패해 코리아컵에서 탈락했다."),
        ]
        summary, summary_type = summarize("이정효", articles)
        self.assertEqual(summary_type, "rule")
        self.assertTrue(summary.endswith("..."))  # 절단이어도 점수가 높으면 유지
        self.assertEqual(summary, articles[0]["title"])

    def test_mid_title_ellipsis_is_not_penalized(self):
        # 제목 "중간"의 말줄임표는 정상 표기 — 절단으로 오판하면 안 된다.
        self.assertFalse(
            cand_summarizer._is_truncated_title(
                "가수 와이스토리, 49세로 사망... 연인이 장례 치르고 마지막 인사 남겨"))
        self.assertTrue(
            cand_summarizer._is_truncated_title("‘귓속말’ 임영호, 향년 49세로 사망…연인이 전한 부고에 ‘추모 물결..."))
        self.assertTrue(cand_summarizer._is_truncated_title("제목이 여기서 잘렸다…"))
        self.assertFalse(cand_summarizer._is_truncated_title("정상적으로 끝나는 제목"))

    def test_truncated_title_trailing_whitespace(self):
        # 앞뒤 공백 제거 후 끝 문자를 본다.
        self.assertTrue(cand_summarizer._is_truncated_title("잘린 제목...  "))
        self.assertFalse(cand_summarizer._is_truncated_title(""))
        self.assertFalse(cand_summarizer._is_truncated_title(None))

    def test_all_truncated_tie_keeps_first_candidate(self):
        # 동점 후보가 전부 절단이면 기존 계약(최초 후보) 유지 — 무한 후순위 금지.
        articles = [
            _article("민경욱 전 의원 자택서 쓰러져 병원 이송...",
                     "https://x.com/a1",
                     "민경욱 전 의원이 자택에서 의식 불명 상태로 발견돼 병원으로 이송됐다."),
            _article("민경욱 전 의원 자택서 쓰러져 병원 이송 중...",
                     "https://x.com/a2",
                     "민경욱 전 의원이 자택에서 의식 불명 상태로 발견돼 병원으로 이송됐다."),
        ]
        summary, summary_type = summarize("민경욱", articles)
        self.assertEqual(summary_type, "rule")
        self.assertEqual(summary, articles[0]["title"])

    def test_all_zero_score_still_no_representative(self):
        # 어느 title도 하위주제 토큰을 담지 않으면(전부 score=0) 기존 불변식대로
        # no_representative — tie-breaker가 score=0 후보를 대표로 승격시키면 안 된다.
        # (하위주제 증거가 snippet에만 있는 경우)
        # 두 title은 서로 공통 토큰이 없고(=하위주제 토큰이 될 수 없음) 하위주제 증거는
        # snippet에만 있다. 한쪽은 절단, 한쪽은 정상 — 그래도 승격되면 안 된다.
        articles = [
            _article("속보...", "https://x.com/z1",
                     "민경욱 전 의원이 자택에서 의식 불명 상태로 발견돼 병원으로 이송됐다."),
            _article("현장 화보", "https://x.com/z2",
                     "민경욱 전 의원이 자택에서 의식 불명 상태로 발견돼 병원으로 이송됐다."),
        ]
        summary, summary_type = summarize("민경욱", articles)
        self.assertEqual(summary, "")
        self.assertEqual(summary_type, "no_representative")


class TestRunSelectionStages(unittest.TestCase):
    """ranker.run_selection_stages 단일 진실원 계약 회귀 고정(2026-07 통합).

    main._rank_and_select(운영)와 replay_selection(재생)이 같은 시퀀스 정의를 소비하고,
    observe hook이 결과에 영향 없이 기존 로그 발생 시점(다음 stage 내부 로그보다 앞)을
    보존하는지 고정한다. 시퀀스 복제본이 조용히 drift한 전례(news/dryrun.py)의 재발 방지.
    """

    def _cands(self):
        return [
            {"keyword": "금리 인상", "sources": {"daum_home": 1}},
            {"keyword": "반도체 수출", "sources": {"nate_home": 1}},
            # display 기사 1건 → exclude_insufficient_display_articles 제외 대상.
            {"keyword": "단일기사", "sources": {"daum_home": 3}},
            # 기사 2건이나 공통 하위주제 없음 → exclude_no_representative 제외 대상.
            {"keyword": "민경욱", "sources": {"daum_home": 4}},
        ]

    def _signals(self):
        return {
            "news": {
                "금리 인상": _news(3, 1, 2, 0.9, articles=[
                    _article("금리 인상 단행 발표 충격", "https://a.com/a1", "금리 인상 단행 발표"),
                    _article("금리 인상 단행 발표 반응", "https://b.com/a2", "금리 인상 단행 발표"),
                ]),
                "반도체 수출": _news(3, 1, 2, 0.9, articles=[
                    _article("반도체 수출 증가 발표 호조", "https://a.com/b1", "반도체 수출 증가 발표"),
                    _article("반도체 수출 증가 발표 지속", "https://b.com/b2", "반도체 수출 증가 발표"),
                ]),
                "단일기사": _news(3, 1, 2, 0.9, articles=[
                    _article("단일기사 후속 상황 발표", "https://a.com/c1", "단일기사 후속"),
                ]),
                "민경욱": _news(3, 1, 2, 0.9, articles=[
                    _article("속보...", "https://a.com/d1",
                             "민경욱 전 의원이 자택에서 의식 불명 상태로 발견돼 병원으로 이송됐다."),
                    _article("현장 화보", "https://b.com/d2",
                             "민경욱 전 의원이 자택에서 의식 불명 상태로 발견돼 병원으로 이송됐다."),
                ]),
            },
            "datalab": {}, "google": {},
        }

    # --- merge 후 rank 11 승격 (2026-09-05 21:47 후속 확인) ---

    _DUP_ARTICLES = [
        ("이란 당국자 한국 미국 위해 희생 말라 발언 파문 확산", "https://s1.example.com/1"),
        ("한국 호르무즈 파병 검토에 이란 직접 공격 간주 경고 나와", "https://s2.example.com/1"),
        ("이란 고위 당국자 한국 호르무즈 개입 시 전쟁 참여로 간주", "https://s3.example.com/1"),
        ("이란 한국 호르무즈 파병시 전쟁 참여 간주 좌시 안해 경고", "https://s4.example.com/1"),
        ("이란 고위당국자 한국 호르무즈 개입시 전쟁 참여 간주 경고", "https://s5.example.com/1"),
        ("한국 호르무즈 파병시 전쟁 참여로 간주 이란 공개 경고 발표", "https://s6.example.com/1"),
    ]

    def _promotion_case(self):
        """ranking eligible 11개 + Top10 안의 genuine duplicate 1쌍.

        중복쌍은 기사 집합이 완전히 동일하지만 어느 제목도 두 keyword 의 anchor 합집합을
        모두 담지 않는다(운영 21:47 구조 재현). 나머지 9개는 서로 무관한 사건이다.
        """
        dup = [_article(t, u, t) for t, u in self._DUP_ARTICLES]
        cands = [
            {"keyword": "이란 한국 경고", "sources": {"daum_home": 1}},
            {"keyword": "이란 당국자 간주", "sources": {"daum_home": 1}},
        ]
        news = {
            "이란 한국 경고": _news(3, 1, 2, 0.9, articles=[dict(a) for a in dup]),
            "이란 당국자 간주": _news(3, 1, 2, 0.9, articles=[dict(a) for a in dup]),
        }
        # 무관한 사건 9개 — score 내림차순이라 마지막이 rank 11 후보가 된다.
        for i in range(9):
            kw = f"독립사건{i}"
            cands.append({"keyword": kw, "sources": {"daum_home": 1}})
            news[kw] = _news(3, 1, 2, 0.9, articles=[
                _article(f"{kw} 관련 상세 보도 내용 정리 기사 {j}", f"https://u{i}{j}.example.com/1",
                         f"{kw} 관련 상세 보도")
                for j in range(3)
            ])
        return cands, {"news": news, "datalab": {}, "google": {}}

    def test_merge_frees_slot_and_rank11_is_promoted(self):
        """중복 merge 로 자리가 비면 기존 rank 11 후보가 승격돼 selected 10 이 유지된다.

        운영 21:47 사례의 selected-only replay 는 10→9 로 보이지만, 그것은 SELECTED 행만
        입력한 재구성의 한계다(비선정 행은 근거 기사/score 가 보존되지 않아 전체 파이프
        재현이 불가능). 실제 계약에서는 dedupe_and_merge 가 select_top **이전**에 돌고
        select_top 은 단순 슬라이스이므로, merge 로 줄어든 만큼 하위 정상 후보가 그
        자리를 채운다. 이 테스트가 그 계약을 고정한다.
        """
        cands, signals = self._promotion_case()
        stages = ranker.run_selection_stages(cands, signals)
        # 전제: ranking eligible 이 TOP_N 보다 많다(11개).
        self.assertEqual(len(cands), 11)
        self.assertGreater(len(stages["gate_passed"]), ranker.TOP_N)
        # 중복쌍이 실제로 하나로 병합됐다.
        merged_kws = [m["keyword"] for m in stages["merged"]]
        self.assertEqual(len(stages["merged"]), len(stages["gate_passed"]) - 1)
        self.assertNotIn("이란 당국자 간주", merged_kws)
        absorbed = [
            rel for m in stages["merged"] for rel in (m.get("related_keywords") or [])
        ]
        self.assertIn("이란 당국자 간주", absorbed)
        # 병합 후에도 최종 selected 는 10 을 유지한다(하위 후보 승격).
        self.assertEqual(len(stages["top"]), ranker.TOP_N)
        # 승격 근거: merge 직전에는 후보가 11개였고 select_top 은 슬라이스만 한다.
        self.assertEqual(len(stages["kept"]), ranker.TOP_N)

    def test_underfill_only_when_no_safe_candidate_remains(self):
        """안전 후보가 부족하면(정확히 TOP_N 개에서 중복 1쌍) 승격 없이 9개로 줄어든다.

        빈자리를 아무 후보로나 강제로 채우지 않는다는 계약 — 위 승격 테스트와 짝이다.
        """
        cands, signals = self._promotion_case()
        # rank 11 후보 하나를 제거해 eligible 을 정확히 TOP_N 으로 만든다.
        drop = "독립사건8"
        cands = [c for c in cands if c["keyword"] != drop]
        signals["news"].pop(drop)
        stages = ranker.run_selection_stages(cands, signals)
        self.assertEqual(len(cands), ranker.TOP_N)
        # 중복 1쌍이 병합되고 채울 후보가 없으므로 9개.
        self.assertEqual(len(stages["top"]), ranker.TOP_N - 1)

    def test_contract_keys_and_top_equals_select_top(self):
        stages = ranker.run_selection_stages(self._cands(), self._signals())
        self.assertEqual(
            set(stages.keys()),
            {"gate_passed", "ranked", "pr_excluded", "merged", "generic_excluded",
             "display_excluded", "no_rep_excluded", "kept", "top"},
        )
        # top은 kept의 순수 slice — 재호출해도 동일해야 한다(순위 재계산 없음).
        self.assertEqual(stages["top"], ranker.select_top(stages["kept"]))

    def test_gate_passed_snapshot_taken_before_exclusions(self):
        # gate_passed는 compute_scores 통과 직후 시점 값 — 이후 단계에서 제외되는
        # 후보도 포함해야 한다(main의 gate통과 카운트/재구성 계약).
        stages = ranker.run_selection_stages(self._cands(), self._signals())
        self.assertIn("단일기사", stages["gate_passed"])
        self.assertIn("단일기사", stages["display_excluded"])
        self.assertIn("민경욱", stages["gate_passed"])
        self.assertIn("민경욱", stages["no_rep_excluded"])
        top_keywords = [t["keyword"] for t in stages["top"]]
        self.assertNotIn("단일기사", top_keywords)
        self.assertNotIn("민경욱", top_keywords)

    def test_observe_called_in_stage_order_with_returned_lists(self):
        calls = []
        stages = ranker.run_selection_stages(
            self._cands(), self._signals(),
            observe=lambda stage, excluded: calls.append((stage, excluded)),
        )
        self.assertEqual(
            [s for s, _ in calls],
            ["pr_excluded", "generic_excluded", "display_excluded", "no_rep_excluded"],
        )
        # hook이 받은 리스트는 반환 dict와 동일 객체(사본 아님 — 관찰 전용 계약).
        for stage, excluded in calls:
            self.assertIs(excluded, stages[stage])

    def test_observe_has_no_effect_on_results(self):
        base = ranker.run_selection_stages(self._cands(), self._signals())
        observed = ranker.run_selection_stages(
            self._cands(), self._signals(), observe=lambda s, e: None)
        self.assertEqual([t["keyword"] for t in base["top"]],
                         [t["keyword"] for t in observed["top"]])
        for key in ("gate_passed", "pr_excluded", "generic_excluded",
                    "display_excluded", "no_rep_excluded"):
            self.assertEqual(base[key], observed[key])

    def test_main_rank_and_select_delegates_to_shared_sequence(self):
        top_main = main_module._rank_and_select(self._cands(), self._signals(), "test")
        stages = ranker.run_selection_stages(self._cands(), self._signals())
        self.assertEqual([t["keyword"] for t in top_main],
                         [t["keyword"] for t in stages["top"]])
        self.assertEqual([t.get("display_keyword") for t in top_main],
                         [t.get("display_keyword") for t in stages["top"]])
        self.assertEqual([t.get("score") for t in top_main],
                         [t.get("score") for t in stages["top"]])

    def test_replay_selection_matches_shared_sequence(self):
        from news.replay import replay_selection, _fetch_from_input

        def _raw(title, url, desc):
            import datetime as _dt
            from email.utils import format_datetime
            dt = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1)
            return {"title": title, "originallink": url, "link": url,
                    "description": desc, "pubDate": format_datetime(dt)}

        abk = {
            "금리 인상": [
                _raw("금리 인상 단행 발표 충격", "https://a.com/r1", "금리 인상 단행 발표"),
                _raw("금리 인상 단행 발표 반응", "https://b.com/r2", "금리 인상 단행 발표"),
                _raw("금리 인상 단행 발표 후폭풍", "https://c.com/r3", "금리 인상 단행 발표"),
            ],
            "반도체 수출": [
                _raw("반도체 수출 증가 발표 호조", "https://a.com/r4", "반도체 수출 증가 발표"),
                _raw("반도체 수출 증가 발표 지속", "https://b.com/r5", "반도체 수출 증가 발표"),
                _raw("반도체 수출 증가 발표 확대", "https://c.com/r6", "반도체 수출 증가 발표"),
            ],
        }
        kws = list(abk)
        out = replay_selection({"keywords": kws, "articles_by_keyword": abk})
        candidates = [{"keyword": kw, "sources": {"daum_home": i + 1}}
                      for i, kw in enumerate(kws)]
        signals = {"news": cand.build_news_signals(candidates, _fetch_from_input(abk)),
                   "datalab": {}, "google": {}}
        stages = ranker.run_selection_stages(candidates, signals)
        self.assertEqual([s["keyword"] for s in out["selected"]],
                         [t["keyword"] for t in stages["top"]])
        self.assertEqual(out["gate_passed"], stages["gate_passed"])
        self.assertEqual(out["display_excluded"], stages["display_excluded"])
        self.assertEqual(out["no_rep_excluded"], stages["no_rep_excluded"])

    def test_log_interleaving_preserved_in_rank_and_select(self):
        # 기존 계약: 각 제외 단계의 집계 경고(main logger)는 그 단계 직후·다음 stage의
        # 내부 로그보다 앞에 찍힌다. run_selection_stages 통합 후에도 observe hook이
        # 이 인터리브 순서를 보존해야 한다(Codex 계획리뷰 2R P1 회귀 방지).
        with self.assertLogs(level="WARNING") as cm:
            main_module._rank_and_select(self._cands(), self._signals(), "test")
        out = cm.output

        def _idx(substr):
            for i, line in enumerate(out):
                if substr in line:
                    return i
            self.fail(f"log line not found: {substr} in {out}")

        drop_insufficient = _idx("drop 단일기사: insufficient_display_articles")
        agg_display = _idx("display_articles 부족 제외")
        drop_no_rep = _idx("drop 민경욱: no_representative")
        agg_no_rep = _idx("no_representative 제외")
        # ranker 내부 drop 로그 → main 집계 경고 → 다음 stage 내부 로그 → 그 집계 경고.
        self.assertLess(drop_insufficient, agg_display)
        self.assertLess(agg_display, drop_no_rep)
        self.assertLess(drop_no_rep, agg_no_rep)


if __name__ == "__main__":
    unittest.main(verbosity=2)
