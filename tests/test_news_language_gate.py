"""기사 단위 한국어 언어 gate 회귀 테스트."""
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news import candidates as cand
from news import ranker
from news.builder import build_ranked_entry
from news.normalizer import is_high_confidence_foreign_article, normalize_article


def _raw(title, description, url):
    return {
        "title": title,
        "description": description,
        "originallink": url,
        "pubDate": datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z"),
    }


def _healthy_meta(keyword, idx):
    articles = [
        {
            "title": f"{keyword} 핵심 사건 후속 보도 {n}",
            "snippet": f"{keyword} 관련 핵심 사건을 한국어로 자세히 전한 기사입니다.",
            "url": f"https://healthy{idx}-{n}.example.com/news",
            "published_at": datetime.now(timezone.utc).isoformat(),
            "relevance_score": 0.9,
            "relevance_reason": "keyword_main_topic",
            "is_incidental": False,
            "is_primary_cluster": True,
        }
        for n in range(2)
    ]
    return {
        "recent_count": 2,
        "latest_age_hours": 0.1,
        "domain_diversity": 2,
        "title_relevance": 0.9,
        "articles": articles,
        "representative_article": articles[0],
        "high_relevance_count": 2,
        "quality_cluster_size": 2,
        "fresh_high_relevance_count": 2,
        "fresh_quality_cluster_size": 2,
        "latest_relevant_age_hours": 0.1,
        "keyword_kind": "unknown",
        "has_dominant_event": True,
        "same_event_burst": True,
    }


class TestArticleLanguagePrimitive(unittest.TestCase):
    def test_vietnamese_article_is_high_confidence_foreign(self):
        self.assertTrue(is_high_confidence_foreign_article(
            "Vợ chồng bà Koo Yeon-kyung thuộc gia đình LG được tuyên trắng án",
            "Hàn Quốc siết phí dịch vụ thông tin chứng khoán và cấm tự động gia hạn.",
        ))

    def test_english_only_article_is_high_confidence_foreign(self):
        self.assertTrue(is_high_confidence_foreign_article(
            "Seoul records first snowfall earlier than last year",
            "The city issued its first heavy snow advisory of the winter season.",
        ))

    def test_japanese_and_chinese_articles_are_high_confidence_foreign(self):
        self.assertTrue(is_high_confidence_foreign_article(
            "日本経済の回復見通しと政府の新しい支援政策",
            "企業投資と個人消費の動向について詳しく報じた記事です",
        ))
        self.assertTrue(is_high_confidence_foreign_article(
            "中国经济复苏前景与政府公布的新支持政策",
            "报道详细分析企业投资和居民消费最新变化",
        ))

    def test_korean_report_about_overseas_event_is_kept(self):
        self.assertFalse(is_high_confidence_foreign_article(
            "미국 중간선거 판세 분석…상원과 하원 모두 접전",
            "뉴욕 현지 투표소 상황과 주요 쟁점을 한국어로 전했다.",
        ))

    def test_english_proper_names_in_korean_article_are_kept(self):
        self.assertFalse(is_high_confidence_foreign_article(
            "BTS·AI·NVIDIA 협력 확대…한국 스타트업도 참여",
            "엔비디아와 국내 기업이 인공지능 생태계 투자 계획을 발표했다.",
        ))

    def test_short_english_brand_or_acronym_is_uncertain_and_kept(self):
        for title in ("BTS", "AI", "NVIDIA"):
            with self.subTest(title=title):
                self.assertFalse(is_high_confidence_foreign_article(title, ""))

    def test_domain_does_not_override_article_language(self):
        foreign_on_korean_domain = _raw(
            "Taste of Winter: Korea issues first heavy snow advisory of season",
            "Seoul recorded the first snowfall earlier than last year.",
            "https://english.example.co.kr/article/1",
        )
        korean_on_foreign_domain = _raw(
            "일본 경제 회복세…한국 기업 수출에도 긍정적 영향",
            "도쿄 현지 지표를 바탕으로 한국어로 분석한 기사다.",
            "https://foreign.example.com/korean/1",
        )
        self.assertIsNone(normalize_article(foreign_on_korean_domain))
        self.assertIsNotNone(normalize_article(korean_on_foreign_domain))


class TestLanguageGatePipeline(unittest.TestCase):
    def setUp(self):
        self.vietnamese = [
            _raw(
                f"Thời tiết và kinh tế Hàn Quốc được phân tích trong bản tin số {i}",
                "Bài viết bằng tiếng Việt cung cấp thông tin chi tiết cho độc giả.",
                f"https://vn.example.com/{i}",
            )
            for i in range(6)
        ]
        self.korean = [
            _raw(
                f"미국 날씨 악화로 항공편 결항 잇따라 {i}",
                "현지 폭설 상황과 항공편 운항 정보를 한국어로 전한 기사입니다.",
                f"https://ko.example.com/{i}",
            )
            for i in range(2)
        ]

    def test_foreign_only_keyword_has_no_news_signal(self):
        self.assertIsNone(cand.compute_news_signal("thời tiết", self.vietnamese))

    def test_mixed_bundle_counts_only_korean_evidence(self):
        sig = cand.compute_news_signal("미국 날씨", self.vietnamese + self.korean)
        self.assertIsNotNone(sig)
        self.assertEqual(len(sig["articles"]), 2)
        self.assertEqual(sig["recent_count"], 2)
        self.assertTrue(all("미국 날씨" in a["title"] for a in sig["articles"]))

    def test_filtered_article_cannot_reappear_in_display_or_summary(self):
        sig = cand.compute_news_signal("미국 날씨", self.vietnamese + self.korean)
        ranked = {
            "keyword": "미국 날씨",
            "score": 0.5,
            "source_breakdown": {},
            "rank_reason": "최근 뉴스 다수",
            "news_meta": sig,
            "used_signals": ["news"],
            "sources": {"google_trends": 1},
        }
        entry = build_ranked_entry(1, ranked)
        exposed = entry["articles"] + entry["display_articles"]
        self.assertTrue(exposed)
        self.assertTrue(all("vn.example.com" not in a["url"] for a in exposed))
        self.assertNotIn("Bài viết", entry["summary"])

    def test_rank11_safe_candidate_is_promoted_after_foreign_drop(self):
        healthy = [f"정상후보{i}" for i in range(1, 11)]
        candidates = ([{"keyword": "thời tiết", "sources": {"google_trends": 1}}]
                      + [{"keyword": kw, "sources": {"daum_home": i + 1}}
                         for i, kw in enumerate(healthy)])
        before_signals = {
            "news": {
                "thời tiết": _healthy_meta("thời tiết", 99),
                **{kw: _healthy_meta(kw, i) for i, kw in enumerate(healthy)},
            },
            "datalab": {},
            "google": {},
        }
        before = [item["keyword"] for item in ranker.select_top(
            ranker.compute_scores(candidates, before_signals)
        )]
        self.assertIn("thời tiết", before)
        self.assertNotIn("정상후보10", before, "fixture 전제: 필터 전 rank11 후보")

        after_signals = {
            "news": {kw: _healthy_meta(kw, i) for i, kw in enumerate(healthy)},
            "datalab": {},
            "google": {},
        }
        ranked = ranker.compute_scores(candidates, after_signals)
        top = ranker.select_top(ranked)
        keys = [item["keyword"] for item in top]
        self.assertEqual(len(keys), ranker.TOP_N)
        self.assertNotIn("thời tiết", keys)
        self.assertIn("정상후보10", keys)

    def test_underfill_remains_allowed_when_safe_candidates_run_out(self):
        healthy = [f"정상후보{i}" for i in range(1, 9)]
        candidates = ([{"keyword": "thời tiết", "sources": {"google_trends": 1}}]
                      + [{"keyword": kw, "sources": {"daum_home": i + 1}}
                         for i, kw in enumerate(healthy)])
        signals = {
            "news": {kw: _healthy_meta(kw, i) for i, kw in enumerate(healthy)},
            "datalab": {},
            "google": {},
        }
        top = ranker.select_top(ranker.compute_scores(candidates, signals))
        self.assertEqual(len(top), 8)


if __name__ == "__main__":
    unittest.main()
