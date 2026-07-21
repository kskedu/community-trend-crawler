"""고위험 사건어 키워드의 crime-attribution safety gate 회귀 fixture (2026-07-21).

문제(운영 재현): "박나래 공갈미수 구속"처럼 유명인 이름 + 범죄어 + 처분어가 결합된
키워드가 실시간 이슈에 노출됐다. 실제 기사들은 "박나래를 협박한 전 매니저가 공갈미수로
구속"이라, 범죄·처분의 주체는 박나래가 아니라 전 매니저다. 이름과 범죄어를 직결한
키워드는 유명인을 범죄 주체로 오인하게 만드는 명예·법적 위험이 있다.

기존 entity-role 정제는 다토큰 키워드(kind=unknown)에 적용되지 않았고, 적용됐어도
"키워드 엔티티가 기사 주제/주어인가"만 봐서 제목 앞머리 인물명은 통과했다. 실제 범죄
주체가 관계인(전 매니저/지인/직원/가족)인지를 검사하는 게이트가 없었다.

설계(fail-closed): 범죄·처분어를 포함한 키워드(crime_keyword_requires_check)는 기본
위험으로 두고, 고관련 기사들이 "이름 엔티티가 실제 범죄 주체"임을 적극 입증할 때만
안전(노출)한다. 입증 못 하면 drop. 하드코딩된 인물명·금칙어 없이 역할 판정 규칙 기반.

사용자 지정 필수 테스트:
- 박나래 전 매니저 공갈미수 구속 / 유명인 전 매니저 구속 / 기업 대표를 협박한 직원 구속
- 배우의 가족이 사기 혐의로 구속 / 피해자 이름이 제목 앞 반복 / 실제 본인 구속(정상)
- 주체 불명확 묶음 / 피해자·피의자 역할 혼재 충돌
"""
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news import candidates as cand
from news import ranker
from news.replay import replay_selection


_NOW = datetime.now(timezone.utc)


def _iso(hours_ago=1.0):
    return (_NOW - timedelta(hours=hours_ago)).strftime("%a, %d %b %Y %H:%M:%S +0000")


def _raw(title, host="news1.example.com", desc="", hours_ago=1.0):
    return {
        "title": title,
        "originallink": f"https://{host}/{abs(hash(title)) % 100000}",
        "link": f"https://{host}/x",
        "description": desc,
        "pubDate": _iso(hours_ago),
    }


def _sig(keyword, raw_items):
    return cand.compute_news_signal(keyword, raw_items)


def _selected(keyword, arts):
    r = replay_selection({"keywords": [keyword], "articles_by_keyword": {keyword: arts}})
    return [(s["keyword"], s["display_keyword"]) for s in r["selected"]]


# 실제 스크린샷 박나래 클러스터(전 매니저가 구속 주체). 서로 다른 press·시간으로 실전 근사.
PARKNARAE_ARTS = [
    _raw("“회사 매출 10% 달라”…박나래 전 매니저, 공갈미수 혐의로 구속", "kukinews.com",
         "박나래 전 매니저 A씨가 공갈미수 등 혐의로 구속됐다", hours_ago=1),
    _raw("\"폭로 않겠다, 3천만원 달라\" 박나래 前매니저 '구속송치'", "imaeil.com",
         "서울 용산경찰서는 박나래의 전 매니저 신모씨를 공갈미수와 횡령 혐의로 송치", hours_ago=2),
    _raw("박나래와 법적공방 前 매니저 A씨 공갈미수로 구속", "ize.co.kr",
         "공갈미수 등 혐의로 박나래의 전 매니저 A씨를 구속했다", hours_ago=3),
    _raw("\"회사매출 10% 달라\"…박나래 전 매니저 결국 송치", "wowtv.co.kr",
         "협박해 회사 매출 일부를 요구한 혐의로 박씨의 전 매니저가 검찰에 송치", hours_ago=2),
    _raw("박나래 前 매니저 구속, 허위 폭로 빌미로 금전 요구 혐의", "mk.co.kr",
         "박나래는 전 매니저들을 공갈미수 혐의로 고소", hours_ago=4),
    _raw("“회사 매출 10% 주면 폭로 안 할게” 박나래 전 매니저 구속", "joongang.co.kr",
         "공갈미수 등의 혐의로 박씨의 전 매니저 신모씨를 구속", hours_ago=3),
    _raw("\"폭로 안할테니 매출 10% 달라\"...박나래 전 매니저 구속", "hani.co.kr",
         "협박하며 회사 매출 일부를 요구한 전 매니저가 구속", hours_ago=2),
    _raw("경찰, \"폭로 막으려면 매출 10% 달라\" 박나래 전 매니저 구속 송치", "news1.kr",
         "허위 사실을 이용해 금전을 요구한 혐의를 받는 박 씨의 전 매니저", hours_ago=1),
]


class TestCrimeKeywordTrigger(unittest.TestCase):
    """crime_keyword_requires_check: 검증이 필요한 후보만 트리거(최종 판정 아님)."""

    def test_crime_keyword_triggers_check(self):
        self.assertTrue(cand.crime_keyword_requires_check("박나래 공갈미수 구속"))
        self.assertTrue(cand.crime_keyword_requires_check("박나래 구속"))
        self.assertTrue(cand.crime_keyword_requires_check("정유명 공갈 구속"))

    def test_non_crime_keyword_no_trigger(self):
        # 비범죄 정상 이슈는 트리거되지 않는다 → crime gate 완전 무영향.
        for kw in ("이정후 극적 동점 안타", "여의도공원 재조성", "일회용 팬티형 생리대",
                   "호프 영화", "따릉이 정보 명예"):
            self.assertFalse(cand.crime_keyword_requires_check(kw), kw)

    def test_relationship_labeled_keyword_no_trigger(self):
        # 관계인이 이미 표기에 포함된 안전명은 검증 불필요(안전).
        self.assertFalse(cand.crime_keyword_requires_check("박나래 전 매니저 공갈미수 구속"))
        self.assertFalse(cand.crime_keyword_requires_check("박나래 협박 전 매니저 구속"))
        self.assertFalse(cand.crime_keyword_requires_check("박나래 前 매니저 공갈 구속"))

    # Codex P1-A: bare 범죄어("협박"/"고소")는 트리거를 우회시키지 않는다.
    def test_bare_crime_word_still_triggers(self):
        self.assertTrue(cand.crime_keyword_requires_check("박나래 협박 구속"))
        self.assertTrue(cand.crime_keyword_requires_check("정유명 협박 구속"))

    # Codex P1-B: 직업 접두어 뒤 이름도 트리거된다("배우 김유명 공갈미수 구속").
    def test_occupation_prefixed_name_triggers(self):
        self.assertTrue(cand.crime_keyword_requires_check("배우 김유명 공갈미수 구속"))
        self.assertTrue(cand.crime_keyword_requires_check("가수 김유명 협박 구속"))
        # 직업 접두어 단독(이름 없음)은 트리거 안 됨.
        self.assertFalse(cand.crime_keyword_requires_check("배우 마약 구속"))

    # Codex P1(2R-873): 직업/관계 다의어 접두어(대표/유튜버)가 이름 앞에 와도 우회 안 됨.
    def test_ambiguous_prefix_before_name_triggers(self):
        self.assertTrue(cand.crime_keyword_requires_check("유튜버 김유명 사기 구속"))
        self.assertTrue(cand.crime_keyword_requires_check("대표 김유명 사기 구속"))
        # 관계명사가 이름 "뒤"에 오는 안전명은 여전히 억제("박나래 전 매니저 …").
        self.assertFalse(cand.crime_keyword_requires_check("박나래 전 매니저 사기 구속"))


class TestCrimeSubjectRole(unittest.TestCase):
    """classify_crime_subject_role: 기사에서 이름 엔티티가 실제 범죄 주체인가."""

    # 유명인 전 매니저 구속 — 이름에 종속된 제3자(전 매니저)가 주체.
    def test_celebrity_manager_is_third_party(self):
        art = _raw("배우 김유명 전 매니저, 협박 혐의로 구속", desc="김유명을 협박한 전 매니저가 구속")
        self.assertEqual(cand.classify_crime_subject_role("김유명", art),
                         "victim_or_bystander")

    # 기업 대표를 협박한 직원 구속 — victim-context("협박한") 우선.
    def test_ceo_threatened_by_employee(self):
        art = _raw("대박기업 대표를 협박한 직원 구속", desc="대표 협박 직원 구속")
        self.assertEqual(cand.classify_crime_subject_role("대박기업", art),
                         "victim_or_bystander")

    # 배우 가족(동생) 사기 구속 — 이름 종속 관계인 주체.
    def test_actor_family_fraud(self):
        art = _raw("배우 한유명 동생, 사기 혐의로 구속", desc="한유명 동생 사기 구속")
        self.assertEqual(cand.classify_crime_subject_role("한유명", art),
                         "victim_or_bystander")

    # 피해자 이름 제목 앞 반복 + 익명 주체(40대 남성)가 실제 범죄 주체.
    def test_victim_name_leading_generic_subject(self):
        for title, desc in [
            ("이유명 협박한 40대 남성 구속", "이유명 협박범 구속"),
            ("이유명 상대로 금품 요구한 남성 구속", "이유명 상대 남성 구속"),
            ("이유명 스토킹 혐의 남성 구속영장", "이유명 스토킹범 구속"),
        ]:
            art = _raw(title, desc=desc)
            self.assertEqual(cand.classify_crime_subject_role("이유명", art),
                             "victim_or_bystander", title)

    # 본인 실제 구속 — 관계·victim 마커 없이 이름+범죄어 직결 → subject 보존.
    def test_real_self_arrest_is_subject(self):
        for title in ("가수 박유명, 마약 투약 혐의로 구속",
                      "박유명 구속영장 발부…마약 혐의",
                      "박유명, 결국 구속…경찰 송치"):
            art = _raw(title, desc="박유명 구속")
            self.assertEqual(cand.classify_crime_subject_role("박유명", art),
                             "subject", title)

    # 본인 기소(정치인) — 관계명사 개입 없음 → subject.
    def test_self_indictment_is_subject(self):
        art = _raw("김의원, 뇌물수수 혐의로 기소", desc="김의원 기소")
        self.assertEqual(cand.classify_crime_subject_role("김의원", art), "subject")

    # 관계명이 단독 주체인 정상 범죄("남편 살인 구속"류)는 유명인 이름 anchor 가 없어
    # crime gate 자체가 트리거되지 않는다 → role 판정 경로에 도달하지 않음(정상 이슈 보존).
    def test_relation_only_crime_not_triggered(self):
        self.assertFalse(cand.crime_keyword_requires_check("남편 음주운전 입건"))
        self.assertFalse(cand.crime_keyword_requires_check("직원 횡령 구속"))
        self.assertFalse(cand.crime_keyword_requires_check("40대 남성 흉기 난동 구속"))

    # 주체 불명확 — subject 로 확정하지 않는다.
    def test_ambiguous_subject_unknown(self):
        art = _raw("최유명 관련 수사 계속…구속영장 검토", desc="최유명 관련 수사")
        self.assertIn(cand.classify_crime_subject_role("최유명", art),
                      ("unknown", "victim_or_bystander"))

    # Codex P1-C: 이름이 선두 수식어/기관명이고 실제 처분 대상이 다른 인물이면 subject 아님.
    def test_leading_modifier_not_self_subject(self):
        art = _raw("김건희 특검, 윤석열 전 대통령 구속영장 청구", desc="특검 구속영장 청구")
        self.assertNotEqual(cand.classify_crime_subject_role("김건희", art), "subject")

    # Codex P1(2R-963): 구두점 없는 제목도 다른 실명 주체가 끼면 subject 확정 보류.
    def test_no_punct_other_name_not_self_subject(self):
        art = _raw("김건희 특검 윤석열 구속영장 청구", desc="특검 구속영장 청구")
        self.assertNotEqual(cand.classify_crime_subject_role("김건희", art), "subject")

    # Codex P2(2R): bare 범죄어(협박/투약 등 일반어)가 본인 실제 사건을 victim/unknown 으로
    # 떨구지 않는다 — "박유명, 마약 투약 혐의로 구속"은 subject 유지.
    def test_self_arrest_common_noun_not_flipped(self):
        art = _raw("가수 박유명, 마약 투약 혐의로 구속", desc="박유명 마약 투약 구속")
        self.assertEqual(cand.classify_crime_subject_role("박유명", art), "subject")

    # Codex P2(3R): 마약 종류명(필로폰/대마/코카인)이 다른 실명 후보로 오인돼 본인 사건이
    # unknown 으로 떨어지지 않는다.
    def test_self_arrest_drug_names_not_flipped(self):
        for drug in ("필로폰", "대마", "코카인"):
            art = _raw(f"가수 김유명 {drug} 투약 혐의로 구속", desc=f"김유명 {drug} 투약 구속")
            self.assertEqual(cand.classify_crime_subject_role("김유명", art), "subject", drug)

    # Codex P1-B: 직업 접두어 키워드의 이름 anchor 로 role 판정.
    def test_occupation_prefix_role(self):
        art = _raw("배우 김유명 전 매니저, 협박 혐의로 구속", desc="김유명 전 매니저 구속")
        self.assertEqual(cand.classify_crime_subject_role("배우 김유명 공갈 구속", art),
                         "victim_or_bystander")


class TestCrimeAttributionGate(unittest.TestCase):
    """compute_news_signal 집계 + ranker._quality_gate_reason fail-closed."""

    # 박나래 케이스 — has_unsafe_crime_attribution True, gate 가 해당 사유로 drop.
    def test_parknarae_flagged_and_dropped(self):
        sig = _sig("박나래 공갈미수 구속", PARKNARAE_ARTS)
        self.assertTrue(sig.get("crime_check_triggered"))
        self.assertFalse(sig.get("crime_attribution_verified_self"))
        self.assertTrue(sig.get("has_unsafe_crime_attribution"))
        self.assertEqual(
            ranker._quality_gate_reason("박나래 공갈미수 구속", sig),
            "unsafe_crime_attribution",
        )

    # 최종 노출에 위험 표기가 남지 않는다(end-to-end replay).
    def test_parknarae_not_selected(self):
        selected = _selected("박나래 공갈미수 구속", PARKNARAE_ARTS)
        self.assertNotIn("박나래 공갈미수 구속", [kw for kw, _ in selected])
        self.assertNotIn("박나래 공갈미수 구속", [d for _, d in selected])

    # 본인 실제 구속(정상) — verified_self True, gate 통과(억제 안 됨, 회귀 방지).
    def test_self_arrest_preserved_through_gate(self):
        arts = [
            _raw("가수 박유명, 마약 투약 혐의로 구속", "a.example.com", "박유명 마약 구속", 1),
            _raw("박유명 구속영장 발부…마약 혐의", "b.example.com", "박유명 마약 구속", 2),
            _raw("박유명, 결국 구속…경찰 송치", "c.example.com", "박유명 송치", 3),
            _raw("박유명 마약 혐의 인정…구속 상태 송치", "d.example.com", "박유명 구속 송치", 1),
        ]
        sig = _sig("박유명 마약 구속", arts)
        self.assertTrue(sig.get("crime_check_triggered"))
        self.assertTrue(sig.get("crime_attribution_verified_self"))
        self.assertFalse(sig.get("has_unsafe_crime_attribution"))
        self.assertIsNone(ranker._quality_gate_reason("박유명 마약 구속", sig))

    # 피해자/피의자 혼재 충돌 — 관계인 주체 다수 → 위험 유지(fail-closed).
    def test_conflicting_roles_stays_unsafe(self):
        arts = [
            _raw("정유명 전 매니저 공갈 혐의 구속", "a.example.com", "전 매니저 구속", 1),
            _raw("정유명 전 매니저 협박으로 구속", "b.example.com", "전 매니저 협박 구속", 2),
            _raw("정유명, 억울함 호소…\"나는 피해자\"", "c.example.com", "정유명 피해자 주장", 3),
            _raw("정유명 소속사, 전 매니저 고소", "d.example.com", "정유명 소속사 고소", 2),
        ]
        sig = _sig("정유명 공갈 구속", arts)
        self.assertTrue(sig.get("has_unsafe_crime_attribution"))
        self.assertEqual(ranker._quality_gate_reason("정유명 공갈 구속", sig),
                         "unsafe_crime_attribution")


class TestNonCrimeRegression(unittest.TestCase):
    """비범죄·본인 사건 회귀: crime gate 가 기존 신호를 훼손하지 않는다."""

    # 비범죄 키워드는 crime 필드 자체가 붙지 않고 기존 게이트 동작 불변.
    def test_non_crime_signal_unchanged(self):
        arts = [
            _raw("이정후 극적 동점 안타로 승리 견인", "a.example.com", "이정후 안타", 1),
            _raw("이정후, 결승타…팀 연승", "b.example.com", "이정후 결승타", 2),
            _raw("이정후 멀티히트 활약", "c.example.com", "이정후 멀티히트", 1),
        ]
        sig = _sig("이정후", arts)
        # crime 트리거 안 됨 → 위험 판정 없음.
        self.assertFalse(sig.get("crime_check_triggered", False))
        self.assertFalse(sig.get("has_unsafe_crime_attribution", False))
        # 기존 품질 게이트가 crime 사유로 막지 않는다.
        self.assertNotEqual(ranker._quality_gate_reason("이정후", sig),
                            "unsafe_crime_attribution")

    # 단독 관계명 주체 정상 범죄("남편 음주운전 입건" 류)는 억제되지 않는다.
    def test_generic_relation_crime_not_over_blocked(self):
        # 유명인 이름 anchor 없이 관계명이 곧 주체인 일반 사회 이슈. crime keyword 트리거는
        # 되더라도, 기사에서 victim-context 없이 관계명 본인 사건이면 verified/unknown 경로.
        arts = [
            _raw("40대 남성, 흉기 난동 혐의로 구속", "a.example.com", "남성 구속", 1),
            _raw("흉기 난동 40대 남성 구속영장 발부", "b.example.com", "남성 구속", 2),
        ]
        sig = _sig("흉기 난동 구속", arts)
        # 이름 고유명 anchor 가 없으므로 victim_or_bystander 다수가 되지 않아야 한다
        # (익명 주체=실제 주체). 최소한 unsafe 로 과잉 판정하지 않는다.
        self.assertFalse(sig.get("has_unsafe_crime_attribution", False))


if __name__ == "__main__":
    unittest.main()
