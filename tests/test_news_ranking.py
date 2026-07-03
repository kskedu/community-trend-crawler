"""통합 랭킹 단위 테스트 (unittest, 외부호출/DB write 없음).

검증 항목:
- ranker: score 결정성, News 지배, 재정규화, penalty, 동점 tiebreak, 0-division
- candidates: dedup/병합/상한, 다양성 카운트, News 신호 산출
- datalab: recent_delta 계산, 0-division 방어, fixture skip
- google: stub skip
- Daum 복제 방지(순서 탈동조)
- JSON backward compatibility(기존 프론트 필드 보존)
- 랭킹 품질 개선(docs/news-ranking-quality-plan.md): 유사 키워드 dedupe,
  same-issue merge, article relevance/incidental mention 필터, clustering/
  representative 선택, movement와의 순서 관계
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news import ranker, candidates as cand, datalab, google
from news.builder import build_ranked_issues, build_ranked_entry
from news.movement import apply_movement
import main as main_module


def _news(recent_count, age, diversity, relevance, articles=None,
          high_relevance_count=2, quality_cluster_size=2,
          fresh_high_relevance_count=1, fresh_quality_cluster_size=1,
          latest_relevant_age_hours=1.0):
    # high_relevance_count/quality_cluster_size/fresh_* 기본값은 keyword-level quality
    # gate(ranker._passes_keyword_quality_gate, fresh relevance gate 포함)를 통과하도록
    # 채운다 — 이 헬퍼를 쓰는 기존 테스트들은 quality gate 자체가 아니라 score/penalty/
    # 정규화 로직을 검증 대상으로 하므로, 신규 gate 때문에 무관하게 회귀하지 않아야 한다.
    return {
        "recent_count": recent_count,
        "latest_age_hours": age,
        "domain_diversity": diversity,
        "title_relevance": relevance,
        "articles": articles or [{"title": "t", "url": "https://x.com/a", "snippet": "s",
                                  "press": "x", "published_at": None, "thumbnail": None}],
        "high_relevance_count": high_relevance_count,
        "quality_cluster_size": quality_cluster_size,
        "fresh_high_relevance_count": fresh_high_relevance_count,
        "fresh_quality_cluster_size": fresh_quality_cluster_size,
        "latest_relevant_age_hours": latest_relevant_age_hours,
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
    return {"title": title, "url": url, "press": "x", "snippet": snippet,
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
        good_articles = [
            _article("AI 노트북 시장 성장", "https://x.com/good1", "노트북 수요가 늘고 있다.",
                     published_at=_recent_iso()),
            _article("신형 노트북 출시", "https://x.com/good2", "새로운 노트북 라인업이 공개됐다.",
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
            _article("AI 노트북 시장 성장", "https://x.com/n1", published_at=_recent_iso()),
            _article("신형 노트북 출시", "https://x.com/n2", published_at=_recent_iso()),
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
        stale_articles = [
            _article("노트북 신제품 리뷰", "https://x.com/b-old1", published_at=_stale_iso(days_ago=20)),
            _article("노트북 스펙 정리", "https://x.com/b-old2", published_at=_stale_iso(days_ago=25)),
        ]
        fresh_articles = [
            _article("모니터 신제품 출시 화제", "https://x.com/b-new1",
                     published_at=_recent_iso(hours_ago=2)),
            _article("모니터 할인 행사 시작", "https://x.com/b-new2",
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
    """phrase 소스가 다양성 hard guard를 우회하지 않는지(2차 리뷰 P1 반영)."""

    def test_phrase_source_excluded_from_non_daum_count(self):
        candidates_ = [{"keyword": "월드컵 일정", "sources": {"phrase": True}}]
        self.assertEqual(cand.count_non_daum(candidates_), 0)

    def test_phrase_mixed_with_independent_source_still_counted(self):
        candidates_ = [{"keyword": "월드컵", "sources": {"phrase": True, "danawa": 1}}]
        self.assertEqual(cand.count_non_daum(candidates_), 1)

    def test_collect_candidates_includes_phrase_keywords(self):
        result = cand.collect_candidates([], [], [], [], phrase_keywords=["신규 이슈 phrase"])
        kws = [c["keyword"] for c in result]
        self.assertIn("신규 이슈 phrase", kws)
        self.assertEqual(result[0]["sources"].get("phrase"), True)


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
        # 다양성 가드(MIN_NON_DAUM_CANDIDATES=4) 통과용 독립 소스 4개.
        danawa_ranked = [{"keyword": f"danawa상품{i}", "rank": i + 1} for i in range(4)]
        pass1_aux = ["생존이슈phrase"]
        pass1_top = [{"keyword": "daum1", "related_keywords": []}]

        # pass2 aux_expanded 재계산 시 "생존이슈phrase"가 다시 나오지 않도록(다른 강한
        # aux 후보들로 상한 12를 채워 밀려나는 상황을 흉내) fetch fixture 구성.
        recent = _recent_iso(1.0)
        news_by_kw = {}
        for i in range(10):
            news_by_kw[f"daum{i}"] = [
                {"title": f"daum{i} 강한아ux단어{j} 발생", "originallink": "", "link": f"https://x.com/{i}-{j}",
                 "description": "", "pubDate": recent}
                for j in range(3)
            ]
        fetch = self._news_fixture_fetch(news_by_kw)

        news_signals = {"daum1": cand.compute_news_signal("daum1", news_by_kw["daum1"])}

        top2, candidates2 = main_module._backfill_pass(
            pass1_top, pass1_aux, daum_ranked, danawa_ranked, [],
            fetch, news_signals, {}, {},
        )
        self.assertIsNotNone(candidates2, "다양성 가드 통과 + aux 신규 후보 있으므로 pass2가 채택돼야 함")
        kws = [c["keyword"] for c in candidates2]
        self.assertIn("생존이슈phrase", kws, "pass1 aux는 top 확장 재추출 결과에 없어도 union으로 보존돼야 함")

    def test_backfill_pass_returns_none_when_no_new_candidates(self):
        # aux/phrase 둘 다 신규 후보를 못 만들면 (None, None)으로 pass1 유지를 알린다.
        daum_ranked = []
        fetch = self._news_fixture_fetch({})
        top2, candidates2 = main_module._backfill_pass(
            [], [], daum_ranked, [], [], fetch, {}, {}, {},
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
            _article("세라젬 롯데오픈 체험존 운영", "https://x.com/s2",
                     "세라젬 체험존에서 헬스케어 체험", _recent_iso()),
            _article("세라젬 브랜드 캠페인 확대", "https://x.com/s3",
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
