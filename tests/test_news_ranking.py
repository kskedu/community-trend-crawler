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


if __name__ == "__main__":
    unittest.main(verbosity=2)
