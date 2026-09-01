"""Generic section/category labels must not become standalone news issues."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news import candidates as cand
from news import ranker
from news.normalizer import title_evidence_text
from news.replay import replay_selection

# fixture 기사 시각은 **실행 시각 기준 상대값**으로 만든다(2026-09-01).
# 과거엔 "Fri, 28 Aug 2026 08:MM"로 고정돼 있었는데, 이 파일이 검증하려는 것은
# generic section/category 계약이지 freshness 가 아니다. 고정 날짜는 실행일이
# FRESH_RELEVANCE_HOURS(72h) 창을 벗어나는 순간 stale_only 게이트가 **먼저** 걸려
# 정작 검증 대상 계약에 도달하지 못한 채 통과/실패가 뒤집힌다(실제로 2026-09-01
# 기준 2건이 그렇게 깨져 있었다).
# 날짜를 더 미래로 미루는 건 같은 폭탄을 다시 심는 것이라, 상대 시각으로 바꿔
# 실행일과 무관하게 항상 같은 의미를 검증하게 한다.
# stale 계약 자체는 약화하지 않는다 — 아래 TestStaleContractStillEnforced 가
# 동일 fixture 를 72h 밖으로 보내면 여전히 stale_only 로 떨어짐을 고정한다.
_FIXTURE_BASE_AGE_HOURS = 1.0

# stale 검증용 기사 나이(절대 시간). FRESH_RELEVANCE_HOURS(72h)에서 파생시키지 않는다 —
# 파생시키면 그 상수를 키우는 회귀에서 fixture 도 같이 밀려나 테스트가 통과해버린다.
_STALE_AGE_HOURS = 96.0


def _pub_date(dt):
    """datetime → 네이버 pubDate(RFC822) 문자열.

    strftime("%a"/"%b")는 LC_TIME 로케일에 따라 요일·월 이름이 한국어 등으로 나올 수
    있어(그러면 normalizer.parse_pubdate 가 파싱에 실패한다) 영문 약어를 직접 만든다.
    실행 환경 로케일과 무관하게 항상 같은 문자열 형식을 낸다.
    """
    days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return (
        f"{days[dt.weekday()]}, {dt.day:02d} {months[dt.month - 1]} {dt.year} "
        f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d} +0000"
    )


def _article(title, description, slug, minute=20):
    """minute 은 fresh 창 **안에서의 상대 배치**만 뜻한다(클수록 더 과거).

    호출부 인자 이름·기본값은 그대로 두고 해석만 절대시각 → "몇 분 전"으로 바꾼다.
    이 파일의 어떤 테스트도 기사 간 선후 자체를 단언하지 않는다(검증 대상은
    generic-category 계약이다) — 필요한 건 "서로 다른 시각"과 "fresh 창 안"뿐이다.
    """
    dt = datetime.now(timezone.utc) - timedelta(
        hours=_FIXTURE_BASE_AGE_HOURS, minutes=minute
    )
    return {
        "title": title,
        "description": description,
        "originallink": f"https://{slug}.example.com/article",
        "pubDate": _pub_date(dt),
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


class TestStaleContractStillEnforced(unittest.TestCase):
    """상대 시각 전환이 stale 차단 계약을 약화시키지 않았음을 고정한다.

    위 fixture 들이 fresh 창 안으로 들어오면서 generic-category 계약을 제대로 검증하게
    됐는데, 그 대가로 "오래된 기사는 막는다"는 계약이 이 파일에서 사라지면 안 된다.
    같은 fixture 를 reference time 기준 FRESH_RELEVANCE_HOURS 밖으로 보내면 여전히
    stale_only 로 떨어져야 한다.
    """

    def _aged(self, articles, hours_ago):
        """동일 fixture 를 reference time 기준 hours_ago 만큼 과거로 옮긴 사본."""
        base = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        return [
            dict(a, pubDate=_pub_date(base - timedelta(minutes=i)))
            for i, a in enumerate(articles)
        ]

    def _entity_articles(self):
        return [
            _article("이정후 시즌 20호 홈런", "이정후가 시즌 20호 홈런을 기록했다.", "entity-a"),
            _article("이정후 시즌 20호 홈런 폭발", "이정후의 시즌 20호 홈런 소식.", "entity-b"),
        ]

    def test_fresh_fixture_passes_quality_gate(self):
        """대조군: 기본(fresh) fixture 는 stale 로 걸리지 않는다."""
        sig = cand.compute_news_signal("이정후", self._entity_articles())
        self.assertGreaterEqual(sig["fresh_high_relevance_count"], 1)
        self.assertIsNone(ranker._quality_gate_reason("이정후", sig))

    def test_fresh_window_threshold_is_not_relaxed(self):
        """계약 상수 자체를 고정한다 — 72h 창이 조용히 넓어지면 실패한다.

        아래 두 테스트가 fixture 나이를 `FRESH_RELEVANCE_HOURS` 로부터 계산하면
        상수를 키웠을 때 fixture 도 같이 밀려나 mutation 을 못 잡는다(자기참조).
        그래서 나이는 절대값(_STALE_AGE_HOURS)으로 두고, 상수는 여기서 따로 고정한다.
        """
        self.assertEqual(cand.FRESH_RELEVANCE_HOURS, 72)
        self.assertLess(cand.FRESH_RELEVANCE_HOURS, _STALE_AGE_HOURS)

    def test_articles_beyond_fresh_window_are_still_stale_only(self):
        """reference time 기준 72h(FRESH_RELEVANCE_HOURS)를 넘긴 기사는 여전히 stale_only."""
        aged = self._aged(self._entity_articles(), _STALE_AGE_HOURS)
        sig = cand.compute_news_signal("이정후", aged)
        # 관련성 자체는 그대로 높다 — 오직 오래됐다는 이유로 걸려야 한다.
        self.assertGreaterEqual(sig["high_relevance_count"], 2)
        self.assertGreater(sig["latest_relevant_age_hours"], cand.FRESH_RELEVANCE_HOURS)
        self.assertEqual(sig["fresh_high_relevance_count"], 0)
        self.assertEqual(ranker._quality_gate_reason("이정후", sig), "stale_only")

    def test_stale_entity_is_not_selected_end_to_end(self):
        """게이트뿐 아니라 최종 선정까지 — stale 후보는 발행되지 않는다."""
        aged = self._aged(self._entity_articles(), _STALE_AGE_HOURS)
        result = replay_selection({
            "keywords": ["이정후"],
            "articles_by_keyword": {"이정후": aged},
            "sources_by_keyword": {"이정후": {"daum_home": 1}},
        })
        self.assertEqual(result["selected"], [])


if __name__ == "__main__":
    unittest.main()
