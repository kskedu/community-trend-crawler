"""Generic section/category labels must not become standalone news issues."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news import candidates as cand
from news import ranker
from news.normalizer import title_evidence_text
from news.replay import replay_selection


def _article(title, description, slug, minute=20):
    return {
        "title": title,
        "description": description,
        "originallink": f"https://{slug}.example.com/article",
        "pubDate": f"Fri, 28 Aug 2026 08:{minute:02d}:00 +0900",
    }


WEATHER_CASE = [
    _article(
        "[날씨] 전국 비·소나기...무더위 이어져",
        "한낮 기온 대부분 32도, 부산 31도 등 무더위. 내일 전국으로 비 확대. "
        "일요일까지 이어질 예정.",
        "weather-one",
        30,
    ),
    _article(
        "[굿모닝 날씨] 저녁까지 최대 80mm 비... 월요일까지 이어져",
        "대구 등 무더위. 영덕·울진 등 비. 월요일까지 비가 이어질 전망.",
        "weather-two",
        20,
    ),
]


class TestGenericCategoryKeyword(unittest.TestCase):
    def _replay(self, keyword, articles):
        return replay_selection({
            "keywords": [keyword],
            "articles_by_keyword": {keyword: articles},
            "sources_by_keyword": {keyword: {"daum_home": 1}},
        })

    def test_operating_weather_case_does_not_publish_generic_keyword(self):
        result = self._replay("날씨", WEATHER_CASE)
        self.assertNotIn(
            "날씨", [item["display_keyword"] for item in result["selected"]]
        )
        # The existing pipeline has no safe canonical-replacement contract. Until a
        # grounded specific candidate exists independently, fail closed.
        self.assertEqual(result["selected"], [])

    def test_section_and_format_prefix_normalization_is_narrow(self):
        self.assertEqual(title_evidence_text("[날씨] 전국 비"), "전국 비")
        self.assertEqual(title_evidence_text("[굿모닝 날씨] 전국 비"), "전국 비")
        self.assertEqual(title_evidence_text("[오늘의 날씨] 전국 비"), "전국 비")
        self.assertEqual(title_evidence_text("[속보][단독] 삼성전자 실적"), "삼성전자 실적")
        self.assertEqual(
            title_evidence_text("[삼성전자] 2분기 실적"), "[삼성전자] 2분기 실적"
        )

    def test_small_category_vocabulary_is_generic_but_entities_are_not(self):
        categories = ("날씨", "뉴스", "경제", "정치", "사회", "스포츠", "연예")
        for category in categories:
            with self.subTest(category=category):
                self.assertTrue(ranker._is_generic_only_display(category))
                kept, excluded = ranker.exclude_generic_singletons([{
                    "keyword": category,
                    "display_keyword": category,
                    "related_keywords": [],
                }])
                self.assertEqual(kept, [])
                self.assertEqual(excluded, [category])
        self.assertFalse(ranker._is_generic_only_display("이정후"))
        self.assertFalse(ranker._is_generic_only_display("삼성전자"))

    def test_section_prefix_is_not_subject_or_grounding_evidence(self):
        meta = cand.compute_news_signal("날씨", WEATHER_CASE)
        self.assertIsNotNone(meta)
        self.assertTrue(all(
            article["relevance_reason"] == "keyword_not_found"
            for article in meta["articles"]
        ))
        grounded = ranker.enforce_display_source_grounding([{
            "keyword": "날씨",
            "display_keyword": "날씨",
            "news_meta": meta,
        }])
        self.assertEqual(grounded, [])

    def test_unrelated_articles_sharing_only_weather_prefix_do_not_form_issue(self):
        articles = [
            _article("[날씨] 제주 한파주의보 발효", "제주 산간 도로 결빙 우려.", "cold"),
            _article("[굿모닝 날씨] 부산 황사 농도 상승", "부산 미세먼지 나쁨.", "dust"),
        ]
        result = self._replay("날씨", articles)
        self.assertEqual(result["selected"], [])

    def test_generic_seed_with_common_specific_event_fails_closed(self):
        articles = [
            _article("[날씨] 태풍 카눈 북상", "태풍 카눈이 남해안으로 북상 중.", "typhoon-a"),
            _article("[굿모닝 날씨] 태풍 카눈 상륙", "태풍 카눈이 남해안에 상륙 전망.", "typhoon-b"),
        ]
        result = self._replay("날씨", articles)
        self.assertNotIn(
            "날씨", [item["display_keyword"] for item in result["selected"]]
        )
        self.assertEqual(result["selected"], [])

    def test_generic_seed_without_coherent_event_is_not_forced(self):
        articles = [
            _article("[날씨] 주말 기상 전망", "지역별 기온과 하늘 상태를 전합니다.", "roundup-a"),
            _article("[오늘의 날씨] 전국 기상 종합", "전국의 오늘 기상 정보를 전합니다.", "roundup-b"),
        ]
        self.assertEqual(self._replay("날씨", articles)["selected"], [])

    def test_normal_single_token_entity_survives(self):
        articles = [
            _article("이정후 시즌 20호 홈런", "이정후가 시즌 20호 홈런을 기록했다.", "entity-a"),
            _article("이정후 시즌 20호 홈런 폭발", "이정후의 시즌 20호 홈런 소식.", "entity-b"),
        ]
        result = self._replay("이정후", articles)
        self.assertEqual(result["selected"][0]["keyword"], "이정후")

    def test_breaking_and_exclusive_prefixes_preserve_real_subject(self):
        articles = [
            _article("[속보] 삼성전자 2분기 실적 영업이익 급증", "삼성전자 실적이 개선됐다.", "prefix-a"),
            _article("[단독] 삼성전자 2분기 실적 영업이익 발표", "삼성전자 영업이익이 늘었다.", "prefix-b"),
        ]
        result = self._replay("삼성전자", articles)
        self.assertEqual(result["selected"][0]["keyword"], "삼성전자")


if __name__ == "__main__":
    unittest.main()
