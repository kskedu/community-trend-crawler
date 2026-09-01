"""evidence volume 신호(recent_count/domain_diversity/latest_age_hours) 계약 회귀 테스트.

배경(2026-09-01 운영 진단, run 7e3c8933-af05-4f0b-9a37-6290d83230ad / 11:19 KST):
"이현균"이 실제 evidence 3건인데 news 축 0.78 로 최종 0.8252 / rank 4 를 받았다.
원인은 compute_news_signal() 이 recent_count/domain_diversity/latest_age_hours 만
entity-role 정제 **이전**의 raw normalized 집합으로 세고 있었던 것이다. 정제로 evidence
에서 빠진 오염 기사가 ranker news 축(recent_count 0.60 + domain_diversity 0.20 = 축의
80%)에는 계속 가산돼, 같은 축에서 근거 3건이 근거 8건과 동급으로 평가됐다.

이 파일은 "ranking 근거 수 = canonical evidence set 크기"라는 계약을 고정한다.
날짜 고정 fixture 를 쓰지 않는다 — FRESH_RELEVANCE_HOURS(72h) 창을 넘기면 stale_only
gate 로 조용히 무력화되는 time-bomb 이 되기 때문이다(tests/test_news_generic_category.py
가 실제로 그렇게 굳었다). 항상 실행 시각 기준 상대 시각을 쓴다.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news import candidates as cand


def _ago(minutes):
    """지금으로부터 N분 전 시각을 네이버 pubDate 형식으로."""
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return t.strftime("%a, %d %b %Y %H:%M:%S +0000")


def _raw(title, host, minutes_ago, description=""):
    return {
        "title": title,
        "originallink": f"https://{host}/news/{abs(hash(title)) % 10**6}",
        "link": f"https://{host}/news/{abs(hash(title)) % 10**6}",
        "pubDate": _ago(minutes_ago),
        "description": description,
    }


class TestEvidenceVolumeMatchesRefinedSet(unittest.TestCase):
    """recent_count/domain_diversity 는 정제 후 evidence 집합으로 세야 한다."""

    def test_side_mention_articles_do_not_inflate_evidence_volume(self):
        """운영 재현: entity keyword 의 side-mention 오염 기사가 rc/dd 를 부풀리면 안 된다.

        keyword 가 사건 주체가 아닌 기사 5건이 정제로 evidence 에서 빠지는데, 수정 전에는
        그 5건이 recent_count(8)/domain_diversity(7)에 그대로 남아 news 축을 최대치로
        끌어올렸다.
        """
        arts = [
            _raw("이현균, 무명 시절→공장 알바와 연기 병행", "www.tvreport.co.kr", 5,
                 "배우 이현균이 무명 시절을 회상했다"),
            _raw("신동엽 \"'신병4' 이현균, 류승룡 앞에서도 기 안 죽어\"", "www.insight.co.kr", 33,
                 "신동엽이 이현균의 연기를 극찬했다"),
            _raw("'김부장' 흥행 후 결혼 골인한 이현균", "www.insight.co.kr", 27,
                 "이현균이 무명 시절 일당 20만원 공장 알바를 했다고 밝혔다"),
            # 아래 5건은 keyword 가 주체가 아닌 부수 언급 — 정제 대상.
            _raw("류승룡, 신작 영화 촬영 돌입", "d1.example.com", 79,
                 "류승룡이 이현균과 함께 출연한 신병4"),
            _raw("신동엽, 예능 복귀 확정", "d2.example.com", 99,
                 "신동엽이 이현균을 언급했다"),
            _raw("드라마 시청률 종합 순위", "d3.example.com", 129, "김부장 이현균"),
            _raw("연예 뉴스 브리핑", "d4.example.com", 149, "이현균 등"),
            _raw("오늘의 방송 편성", "d5.example.com", 179, "이현균 출연"),
        ]
        sig = cand.compute_news_signal("이현균", arts)

        self.assertEqual(sig["keyword_kind"], "entity")
        refined = sig["refined_article_count"]
        self.assertLess(refined, len(arts), "정제가 실제로 기사를 제거해야 이 테스트가 유효하다")
        # 핵심 계약: 랭킹 근거 수는 정제 후 evidence 집합 크기를 넘지 않는다.
        self.assertLessEqual(sig["recent_count"], refined)
        self.assertLessEqual(sig["domain_diversity"], refined)
        self.assertEqual(sig["recent_count"], refined)

    def test_same_press_reissue_counts_articles_but_not_domains(self):
        """같은 매체가 같은 방송을 제목만 달리해 2건 발행 — domain_diversity 는 1 증가하지 않는다."""
        arts = [
            _raw("신동엽 \"'신병4' 이현균, 류승룡 앞에서도 기 안 죽어\"", "www.insight.co.kr", 33,
                 "신동엽이 이현균의 연기를 극찬했다"),
            _raw("'김부장' 흥행 후 결혼 골인한 이현균", "www.insight.co.kr", 27,
                 "이현균이 무명 시절 일당 20만원 공장 알바를 했다고 밝혔다"),
        ]
        sig = cand.compute_news_signal("이현균", arts)
        self.assertEqual(sig["domain_diversity"], 1)
        self.assertEqual(sig["recent_count"], 2)

    def test_multiple_press_reissue_of_same_broadcast_keeps_domain_diversity(self):
        """여러 매체가 같은 방송을 재가공 — 독립 domain 은 실제 매체 수만큼 인정한다.

        이번 수정은 '같은 사건이면 감점'이 아니다. domain_diversity 의 의미(서로 다른
        매체가 다뤘는가)는 그대로 두고, 세는 모수만 evidence 집합으로 맞춘다.
        """
        arts = [
            _raw("신동엽, '신병4' 이현균 연기 극찬", "www.insight.co.kr", 30,
                 "신동엽이 이현균의 연기를 극찬했다"),
            _raw("이현균, 신병4 출연 소감 밝혀", "www.tvreport.co.kr", 28,
                 "이현균이 신병4 출연 소감을 밝혔다"),
            _raw("이현균 \"류승룡 앞에서도 안 떨려\"", "www.newsen.com", 26,
                 "이현균이 류승룡과의 호흡을 언급했다"),
        ]
        sig = cand.compute_news_signal("이현균", arts)
        self.assertEqual(sig["domain_diversity"], 3)
        self.assertEqual(sig["recent_count"], 3)

    def test_url_distinct_near_duplicate_titles_still_count_as_articles(self):
        """URL 이 다른 near-duplicate 는 여전히 개별 기사로 센다(이번 수정 범위 밖).

        이 수정은 near-duplicate 병합을 도입하지 않는다 — evidence 모수 정렬만 한다.
        계약을 명시적으로 고정해 두어 후속 변경이 조용히 의미를 바꾸지 않게 한다.
        """
        arts = [
            _raw("이현균, 무명 시절 공장 알바 고백", "a.example.com", 20,
                 "이현균이 무명 시절 공장 알바를 했다고 고백했다"),
            _raw("이현균, 무명 시절 공장 알바 고백했다", "b.example.com", 21,
                 "이현균이 무명 시절 공장 알바를 했다고 고백했다"),
        ]
        sig = cand.compute_news_signal("이현균", arts)
        self.assertEqual(sig["recent_count"], 2)
        self.assertEqual(sig["domain_diversity"], 2)

    def test_small_but_independent_burst_keeps_full_evidence_volume(self):
        """소수 기사여도 독립 매체에서 동시에 터진 이슈는 근거가 깎이지 않는다.

        GO 기준의 반대쪽 방어 — '기사 수가 적으면 감점'이 아님을 고정한다.
        """
        arts = [
            _raw("이현균 결혼 발표", "a.example.com", 4, "이현균이 결혼을 발표했다"),
            _raw("이현균, 비연예인과 결혼", "b.example.com", 6, "이현균이 비연예인과 결혼한다"),
        ]
        sig = cand.compute_news_signal("이현균", arts)
        self.assertEqual(sig["recent_count"], 2)
        self.assertEqual(sig["domain_diversity"], 2)
        self.assertEqual(sig["recent_count"], sig["refined_article_count"])

    def test_event_keyword_signals_unchanged_by_refinement_scope(self):
        """event/unknown 키워드는 정제를 건너뛰므로 값이 수정 전과 동일하다(회귀 방어)."""
        arts = [
            _raw("네팔 홍수 사망자 증가", "e1.example.com", 10, "네팔 홍수로 사망자가 늘었다"),
            _raw("네팔 홍수 피해 확산", "e2.example.com", 25, "네팔 홍수 피해가 확산되고 있다"),
            _raw("네팔 홍수 구조 작업 계속", "e3.example.com", 40, "네팔 홍수 구조 작업이 이어진다"),
        ]
        sig = cand.compute_news_signal("네팔 홍수", arts)
        self.assertNotEqual(sig["keyword_kind"], "entity")
        self.assertEqual(sig["recent_count"], 3)
        self.assertEqual(sig["domain_diversity"], 3)
        self.assertEqual(sig["refined_article_count"], 3)

    def test_latest_age_hours_reflects_refined_evidence(self):
        """latest_age_hours 도 evidence 집합 기준 — 정제로 빠진 기사가 freshness 를 대표하지 않는다."""
        arts = [
            _raw("이현균, 무명 시절 공장 알바 고백", "a.example.com", 120,
                 "이현균이 무명 시절 공장 알바를 했다고 고백했다"),
            _raw("이현균, 결혼 소감 밝혀", "b.example.com", 150,
                 "이현균이 결혼 소감을 밝혔다"),
            # 훨씬 최신이지만 keyword 가 주체가 아닌 오염 기사.
            _raw("류승룡, 신작 촬영 돌입", "c.example.com", 2,
                 "류승룡이 이현균과 함께 출연한 신병4"),
        ]
        sig = cand.compute_news_signal("이현균", arts)
        if sig["refined_article_count"] < len(arts):
            # 정제가 최신 오염 기사를 뺐다면 latest_age_hours 는 남은 evidence 기준이어야 한다.
            self.assertGreater(sig["latest_age_hours"], 0.5)

    def test_all_non_subject_rollback_preserves_previous_behavior(self):
        """정제 결과가 비면 롤백되므로(과잉 제외 방지) 값이 raw 기준과 같아진다."""
        arts = [
            _raw("류승룡, 신작 영화 촬영 돌입", "d1.example.com", 30,
                 "류승룡이 이현균과 함께 출연한 신병4"),
            _raw("신동엽, 예능 복귀 확정", "d2.example.com", 45,
                 "신동엽이 이현균을 언급했다"),
        ]
        sig = cand.compute_news_signal("이현균", arts)
        # 롤백 경로든 아니든, 근거 수는 항상 evidence 집합과 일치해야 한다.
        self.assertEqual(sig["recent_count"], sig["refined_article_count"])

    def test_evidence_volume_never_exceeds_article_list_length(self):
        """불변식: recent_count/domain_diversity 는 articles 길이를 넘을 수 없다."""
        for kw, arts in [
            ("이현균", [
                _raw("이현균 결혼 발표", "a.example.com", 5, "이현균이 결혼을 발표했다"),
                _raw("류승룡 신작 촬영", "b.example.com", 8, "류승룡이 이현균과 출연"),
                _raw("드라마 시청률 순위", "c.example.com", 12, "김부장 이현균"),
            ]),
            ("네팔 홍수", [
                _raw("네팔 홍수 사망자 증가", "e1.example.com", 10, "네팔 홍수 사망자"),
                _raw("네팔 홍수 피해 확산", "e2.example.com", 20, "네팔 홍수 피해"),
            ]),
        ]:
            with self.subTest(keyword=kw):
                sig = cand.compute_news_signal(kw, arts)
                n = len(sig["articles"])
                self.assertLessEqual(sig["recent_count"], n)
                self.assertLessEqual(sig["domain_diversity"], n)


if __name__ == "__main__":
    unittest.main()
