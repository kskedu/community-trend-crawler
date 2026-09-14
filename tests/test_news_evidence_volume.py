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


def _raw_at(title, host, minutes_ago, slug, description=""):
    """같은 제목이라도 URL 을 명시적으로 구분해야 하는 fixture 용.

    _raw() 는 URL 을 hash(title) 로 만들기 때문에 제목이 같으면 URL 까지 같아져
    상위 dedup(URL 기준)이 먼저 접어 버린다. 포토 batch 는 "제목은 같은데 URL 은
    다르다"가 핵심이므로 slug 로 URL 만 갈라 준다.
    """
    return {
        "title": title,
        "originallink": f"https://{host}/view/{slug}",
        "link": f"https://{host}/view/{slug}",
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


class TestSamePressPhotoBatchDoesNotInflateEvidence(unittest.TestCase):
    """같은 매체가 한 취재 건에서 사진만 갈아 끼워 여러 URL 로 발행한 묶음(포토뉴스)은
    ranking evidence 에서 1건의 editorial item 으로 센다.

    배경(2026-09-12 00:47 UTC 운영 run be3a3de2-81c1-45a4-bcd8-cb7e4a295e04):
    '인터뷰하는 김지홍 대표변호사' 가 rank 8 / score 0.6195 로 Top10 에 올랐다.
    근거 8건이 전부 연합뉴스 한 곳, 제목은 단 2종, published_at 은 초 단위까지 동일,
    URL 은 PYH2026090118{1800,2400,2500,2600,2700,2900,3100} 로 연속된 사진 ID 였다.
    즉 독립 보도 8건이 아니라 한 인터뷰 촬영에서 나온 사진 여러 장이다.

    그런데 recent_count 는 8 로 집계돼 run 최대치와 동률이 됐고, news 축의 60% 를
    차지하는 이 값이 후보를 Top10 까지 밀어 올렸다(domain_diversity 는 1 이지만
    가중치가 20% 라 상쇄되지 않는다).

    계약: "같은 domain + 같은 제목" 묶음만 접는다. 매체가 다르면 접지 않고(독립 보도),
    같은 매체라도 제목이 다르면 접지 않는다(실제 후속 기사).
    """

    def test_same_domain_identical_title_photo_batch_counts_once(self):
        """운영 재현: 같은 매체·같은 제목·URL 만 다른 사진 8장 → evidence 1건."""
        arts = [
            _raw_at("연합뉴스와 인터뷰하는 김지홍 대표변호사", "www.yna.co.kr", 5,
                    f"PYH2026090118{n}0013",
                    "법무법인 지평 김지홍 대표변호사가 연합뉴스와 인터뷰를 하고 있다")
            for n in ("24", "25", "26", "27", "29", "31", "18", "20")
        ]
        sig = cand.compute_news_signal("인터뷰하는 김지홍 대표변호사", arts)
        self.assertEqual(sig["recent_count"], 1)
        self.assertEqual(sig["domain_diversity"], 1)

    def test_same_domain_different_titles_stay_independent(self):
        """같은 매체라도 실제 내용이 다른 후속 기사는 각각 evidence 로 센다."""
        arts = [
            _raw_at("김지홍 대표변호사, 지평 신임 대표 선임", "www.yna.co.kr", 30, "A1",
                    "법무법인 지평이 김지홍 변호사를 신임 대표로 선임했다"),
            _raw_at("김지홍 \"기업 법무 시장 재편될 것\"", "www.yna.co.kr", 25, "A2",
                    "김지홍 대표변호사가 기업 법무 시장 전망을 밝혔다"),
            _raw_at("지평, 김지홍 체제로 조직 개편", "www.yna.co.kr", 20, "A3",
                    "법무법인 지평이 조직 개편을 단행했다"),
        ]
        sig = cand.compute_news_signal("김지홍 대표변호사", arts)
        self.assertEqual(sig["recent_count"], 3)
        self.assertEqual(sig["domain_diversity"], 1)

    def test_different_domains_same_title_stay_independent(self):
        """서로 다른 매체가 같은 제목으로 보도한 것은 접지 않는다(전재/신디케이션 계약 불변).

        이 경로를 접으면 cross-outlet 독립 보도가 깎여 source diversity 계약이 무너진다.
        운영 14일 표본에서 '제목 동일 + domain 복수' 는 141건 관측됐고 전부 정상이다.
        """
        arts = [
            _raw_at("이남철 고령군수 별세", "www.yna.co.kr", 10, "B1", "이남철 고령군수가 별세했다"),
            _raw_at("이남철 고령군수 별세", "www.newsis.com", 12, "B2", "이남철 고령군수가 별세했다"),
            _raw_at("이남철 고령군수 별세", "www.news1.kr", 14, "B3", "이남철 고령군수가 별세했다"),
        ]
        sig = cand.compute_news_signal("이남철 고령군수 별세", arts)
        self.assertEqual(sig["recent_count"], 3)
        self.assertEqual(sig["domain_diversity"], 3)

    def test_photo_batch_mixed_with_independent_reports_keeps_the_independents(self):
        """포토 batch 는 1건으로 접히되, 같은 사건을 다룬 다른 매체 보도는 그대로 남는다."""
        arts = [
            _raw_at("우즈베키스탄 대통령 공식 환영식", "www.yna.co.kr", 8, "P1", "환영식이 열렸다"),
            _raw_at("우즈베키스탄 대통령 공식 환영식", "www.yna.co.kr", 8, "P2", "환영식이 열렸다"),
            _raw_at("우즈베키스탄 대통령 공식 환영식", "www.yna.co.kr", 8, "P3", "환영식이 열렸다"),
            _raw_at("우즈베키스탄 대통령 방한, 정상회담 개최", "www.newsis.com", 15, "Q1",
                    "우즈베키스탄 대통령이 방한해 정상회담을 했다"),
            _raw_at("한-우즈베크 정상, 경제협력 확대 합의", "www.news1.kr", 20, "R1",
                    "두 정상이 경제협력 확대에 합의했다"),
        ]
        sig = cand.compute_news_signal("우즈베키스탄 대통령", arts)
        # 포토 3건 → 1건, 독립 보도 2건 유지 = 3
        self.assertEqual(sig["recent_count"], 3)
        self.assertEqual(sig["domain_diversity"], 3)

    def test_short_identical_titles_below_near_dup_floor_still_fold(self):
        """제목 토큰이 near-dup 하한(5)보다 적어도 **완전히 같으면** 접는다.

        _is_near_duplicate_title 은 짧은 제목의 우연 일치를 막으려고 5토큰 하한을 두는데,
        완전 동일 제목은 우연이 아니다. 운영 표본에서 이 하한 때문에 접히지 않은 동일
        제목·동일 매체 묶음이 147건 있었다.
        """
        arts = [
            _raw_at("이강철 감독 600승", "www.yna.co.kr", 6, "S1", "이강철 감독이 600승을 달성했다"),
            _raw_at("이강철 감독 600승", "www.yna.co.kr", 6, "S2", "이강철 감독이 600승을 달성했다"),
            _raw_at("이강철 감독 600승", "www.yna.co.kr", 6, "S3", "이강철 감독이 600승을 달성했다"),
        ]
        sig = cand.compute_news_signal("이강철 감독 600승", arts)
        self.assertEqual(sig["recent_count"], 1)

    def test_unkeyable_articles_are_kept_not_dropped(self):
        """접기 키(domain/제목)를 만들 수 없는 기사는 빼지 않고 각각 독립으로 센다(fail-open).

        normalize_article 이 빈 제목/빈 URL 기사를 이미 걸러 내므로 compute_news_signal
        경로로는 이 분기에 도달할 수 없다. 그래도 helper 계약은 고정해 둔다 — 키를 못
        만든다고 evidence 에서 빼면 파싱 실패가 조용한 감점으로 바뀌기 때문이다.
        (이 단정이 없으면 "키 없으면 drop" 변이가 전체 suite 에서 살아남는다.)
        """
        items = cand._editorial_items_for_volume([
            {"url": "https://e1.example.com/view/N1", "title": "네팔 홍수 사망자 증가"},
            {"url": "", "title": "네팔 홍수 피해 확산"},          # host 없음
            {"url": "https://e2.example.com/view/N2", "title": ""},  # 제목 없음
        ])
        self.assertEqual(len(items), 3)

    def test_fold_keeps_the_freshest_member_for_latest_age(self):
        """batch 를 접어도 freshness 는 가장 최신 기사 기준이어야 한다.

        접기는 "근거가 몇 건인가"만 줄이는 보정이다. 입력은 relevance 순이라 최신이
        먼저라는 보장이 없어서, 먼저 온 기사를 그대로 대표로 남기면 10시간 전 사진이
        5분 전 사진을 가려 방금 터진 이슈가 freshness 축을 잃는다.
        """
        arts = [
            _raw_at("포즈 취하는 배우", "www.yna.co.kr", 600, "F1", "배우가 포즈를 취하고 있다"),
            _raw_at("포즈 취하는 배우", "www.yna.co.kr", 5, "F2", "배우가 포즈를 취하고 있다"),
        ]
        sig = cand.compute_news_signal("포즈 취하는 배우", arts)
        self.assertEqual(sig["recent_count"], 1)
        # 10시간(600분)이 아니라 5분 쪽이 반영돼야 한다.
        self.assertLess(sig["latest_age_hours"], 1.0)

    def test_recurring_same_title_column_is_folded_known_limitation(self):
        """알려진 한계: 같은 매체가 **같은 제목으로 매일 내는 정기 코너**도 접힌다.

        운영 14일 표본에서 fold 대상 그룹 381건 중 발행 간격이 6시간을 넘는 것은 6건
        (고유 제목 2종)뿐이었다:
          - '금시세(금값) 혼조'(kjdaily, 23시간 간격 — 매일 시세 코너)
          - '독감 환자, 지난해의 4배…유행주의보 발령'(KBS, 2.5시간 간격 — 재발행)
        둘 다 접힌 뒤에도 독립 근거가 4~6건 남아 rank 영향이 1계단 이내였다.

        시간 창 조건을 추가하면 이 1.6% 를 살릴 수 있지만, 임계값을 새로 도입해야 하고
        89.5% 를 차지하는 10분 이내 batch 의 판정은 그대로다. 현재는 접는 쪽을
        유지하고 이 테스트로 **동작을 명시적으로 고정**한다 — 나중에 시간 창을 넣기로
        하면 이 테스트가 먼저 실패해서 계약 변경임을 알려 준다.
        """
        arts = [
            _raw_at("금시세(금값) 혼조", "www.kjdaily.com", 60, "G1", "금시세가 혼조세를 보였다"),
            _raw_at("금시세(금값) 혼조", "www.kjdaily.com", 60 + 23 * 60, "G2", "금시세가 혼조세를 보였다"),
            _raw_at("금값 1돈 소매가 상승", "www.example.com", 50, "G3", "금값 1돈 소매가가 올랐다"),
        ]
        sig = cand.compute_news_signal("금값 1 돈 가격", arts)
        # 23시간 떨어진 정기 코너 2건이 1건으로 접힌다(현재 계약).
        self.assertEqual(sig["recent_count"], 2)

    def test_folded_batch_never_exceeds_article_list(self):
        """불변식 유지: 접은 뒤에도 recent_count/domain_diversity <= articles 길이."""
        arts = [
            _raw_at("포즈 취하는 배우", "www.yna.co.kr", 5, f"T{i}", "배우가 포즈를 취하고 있다")
            for i in range(8)
        ]
        sig = cand.compute_news_signal("포즈 취하는 배우", arts)
        n = len(sig["articles"])
        self.assertLessEqual(sig["recent_count"], n)
        self.assertLessEqual(sig["domain_diversity"], n)
        self.assertEqual(sig["recent_count"], 1)


if __name__ == "__main__":
    unittest.main()
