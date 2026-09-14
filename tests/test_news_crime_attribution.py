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


# 운영 재현(2026-09-10): 실시간 이슈에 "<유명인> 징역 10개월"이 노출됐는데, 기사 8건은
# 전부 "<유명인> 모친/母, 1심서 징역 10개월"이었다. 유명인은 사기에 **이름이 도용된**
# 쪽이고("딸 이름 내세워"), 형을 선고받은 사람은 모친이다. 실제 제목 형태만 옮기고
# 인물명은 합성으로 바꾼다 — production 도 test 도 특정 이름을 하드코딩하지 않는다.
MOTHER_SENTENCED_ARTS = [
    _raw("딸 이름 내세워 투자 사기…한유명 母 1심 실형", "wowtv.example.com",
         "가수 한유명의 모친이 딸 이름을 내세워 투자금을 받아 1심에서 실형", hours_ago=1),
    _raw("한유명 母, 딸 내세워 사기치더니…1심서 징역 10개월 선고", "muse.example.com",
         "한유명 모친이 징역 10개월을 선고받았다", hours_ago=2),
    _raw("한유명 이름 내세워 3100만원 투자 사기…모친 1심 징역 10개월", "donga.example.com",
         "모친 A씨는 딸 한유명을 언급하며 투자금을 받았다", hours_ago=1),
    _raw("딸 언급하며 또 사기…한유명 모친, 1심서 징역 10개월", "kmib.example.com",
         "한유명 모친이 다시 실형을 선고받았다", hours_ago=3),
    _raw("한유명 모친 또 사기죄 실형…1심서 징역 10개월", "viva.example.com",
         "동일 전과에도 사기 행각을 벌인 모친", hours_ago=2),
    _raw("'가수 딸 언급하며 사기' 한유명 모친 1심 실형", "ytn.example.com",
         "한유명 모친 1심 실형", hours_ago=4),
    _raw("'딸 팔아 사기' 한유명 모친, 1심서 징역 10개월 선고", "kookje.example.com",
         "한유명 모친에게 징역 10개월이 선고됐다", hours_ago=2),
    _raw("한유명 모친, 동일 전과에도 사기 행각…1심 징역 10개월 선고", "topstar.example.com",
         "한유명 모친 1심 징역 10개월", hours_ago=1),
]

# 같은 사건 구조인데 형을 받은 사람이 본인인 정상 대조군(과잉차단 회귀 방지).
SELF_SENTENCED_ARTS = [
    _raw("가수 한유명, 투자 사기로 1심 징역 10개월", "a.example.com", "한유명 징역 10개월", 1),
    _raw("한유명 1심 징역 10개월 선고…투자금 3100만원 편취", "b.example.com", "한유명 선고", 2),
    _raw("한유명, 사기 혐의 1심서 징역 10개월", "c.example.com", "한유명 징역", 1),
    _raw("한유명 징역 10개월 선고…\"피해 회복 없어\"", "d.example.com", "한유명 선고", 3),
]


class TestSentencingStageDisposition(unittest.TestCase):
    """선고·판결 단계 처분어도 crime gate 를 트리거한다(2026-09-10).

    기존 _DISPOSITION_TOKENS 는 체포·기소 단계 어휘(구속/송치/기소/체포/입건…)와 "실형"
    뿐이라, **확정 판결**을 담은 키워드가 has_disp=False 로 게이트를 아예 트리거하지
    못했다. 오귀속 피해는 체포 단계보다 선고 단계가 더 크다.
    """

    def test_sentencing_tokens_trigger(self):
        for kw in ("한유명 징역 10개월", "한유명 스토킹 벌금", "한유명 사기 집행유예",
                   "한유명 1심 선고", "한유명 유죄 확정", "한유명 사형"):
            self.assertTrue(cand.crime_keyword_requires_check(kw), kw)

    def test_arrest_stage_tokens_unchanged(self):
        # 기존 체포·기소 단계 동작은 그대로다(회귀 방지).
        self.assertTrue(cand.crime_keyword_requires_check("박나래 공갈미수 구속"))
        self.assertFalse(cand.crime_keyword_requires_check("흉기 난동 구속"))

    def test_homonym_tokens_not_added(self):
        """동음이의어라 정상 이슈를 오차단하는 어휘는 넣지 않는다(14일 운영 실측 근거).

        · "금고" — 관측 9행 중 7행이 "새마을금고"(금융기관).
        · "구형" — 舊型(구형 모델)과 동음이의. 관측된 형사 키워드는 모두 "사형 구형"/
          "징역 N년 구형"처럼 다른 선고 어휘를 함께 담아 이 토큰 없이도 트리거된다.
        """
        for kw in ("새마을 금고", "새마을금고 이사장", "아이폰 구형 모델", "한유명 구형 아이폰"):
            self.assertFalse(cand.crime_keyword_requires_check(kw), kw)


class TestRelationMarkerWordBoundary(unittest.TestCase):
    """관계명사 부분일치가 safety gate 를 스스로 끄지 못한다(2026-09-10).

    관계명사 "형"(오빠)이 처분어 "실형"/"사형" 안에서 부분일치해,
    (a) 트리거 억제 경로에서는 이름+실형 키워드가 전부 게이트를 우회했고,
    (b) role 판정 경로에서는 실제 피고인이 bystander 로 뒤집혔다.
    게이트를 **끄는** 방향이므로 반드시 어절 경계로만 인정해야 한다.
    """

    def test_sentence_word_does_not_suppress_trigger(self):
        # "실형"/"사형" 안의 "형"은 관계명사가 아니다 → 억제되지 않고 트리거된다.
        self.assertTrue(cand.crime_keyword_requires_check("한유명 사기 실형"))
        self.assertTrue(cand.crime_keyword_requires_check("한유명 횡령 실형"))
        self.assertTrue(cand.crime_keyword_requires_check("한유명 사형 선고"))

    def test_real_relation_word_still_suppresses(self):
        # 진짜 관계명사("형"=오빠)가 어절로 오면 기존대로 안전명 억제.
        self.assertFalse(cand.crime_keyword_requires_check("한유명 형 구속"))
        self.assertFalse(cand.crime_keyword_requires_check("한유명 동생 사기 구속"))

    def test_defendant_not_flipped_by_substring(self):
        # "…에 사형 구형" 제목에서 피고인은 그대로 subject 다. unknown 으로 물러나기만
        # 해도 verified_self(subject>=2)를 못 얻어 정상 사건이 통째로 차단되므로,
        # "victim 이 아니다"가 아니라 "subject 다"로 고정한다.
        art = _raw("'여고생 살해' 김유명에 사형 구형…\"영원히 격리해야\"",
                   desc="검찰이 김유명에게 사형을 구형했다")
        self.assertEqual(cand.classify_crime_subject_role("김유명", art), "subject")

    def test_sentence_length_word_not_treated_as_other_name(self):
        """형량·선고 일반어가 "다른 인물명"으로 오인돼 본인 사건을 가리지 않는다.

        "무기"(무기징역)·"실형"처럼 2자 순수 한글이라 인물명 후보로 잡히면, 이름이
        형량과 바로 붙은 정상 본인 사건까지 주체 확정이 보류된다(운영: 총책 이름 +
        "무기징역 확정").
        """
        for title in ("성범죄집단 총책 김유명 무기징역 확정",
                      "김유명 무기징역 확정…25개 혐의 유죄",
                      "김유명 실형 확정"):
            art = _raw(title, desc=title)
            self.assertEqual(cand.classify_crime_subject_role("김유명", art),
                             "subject", title)

    def test_legal_process_word_between_name_and_sentence(self):
        # 이름과 처분어 사이의 사법 절차어("형사재판")에 든 "형"이 관계명사로 오인돼
        # 본인 확정을 막지 않는다.
        art = _raw("김유명 형사재판 징역 2년", desc="김유명 형사재판 징역 2년")
        self.assertEqual(cand.classify_crime_subject_role("김유명", art), "subject")


class TestFamilyRelationAttribution(unittest.TestCase):
    """가족(부모·인척) 관계인이 실제 범죄 주체일 때 유명인에게 귀속하지 않는다."""

    def test_parent_relation_markers_recognized(self):
        # 기존엔 "부모"/"딸"/"아들"만 있어 한국 기사가 실제 쓰는 표기를 못 잡았다.
        for title in ("한유명 모친, 1심서 징역 10개월",
                      "한유명 母, 사기 혐의로 징역 10개월",
                      "한유명 부친, 횡령 혐의 구속",
                      "한유명 장모, 사기로 실형"):
            art = _raw(title, desc=title)
            self.assertEqual(cand.classify_crime_subject_role("한유명", art),
                             "victim_or_bystander", title)

    # 필수 1: 유명인 모친이 징역 → 유명인에게 징역이 귀속되면 안 된다.
    def test_mother_sentenced_is_unsafe_for_celebrity(self):
        sig = _sig("한유명 징역 10개월", MOTHER_SENTENCED_ARTS)
        self.assertTrue(sig.get("crime_check_triggered"))
        self.assertFalse(sig.get("crime_attribution_verified_self"))
        self.assertTrue(sig.get("has_unsafe_crime_attribution"))
        self.assertEqual(ranker._quality_gate_reason("한유명 징역 10개월", sig),
                         "unsafe_crime_attribution")

    # 필수 1(end-to-end): 최종 노출에 위험 표기가 남지 않는다.
    def test_mother_sentenced_not_selected(self):
        selected = _selected("한유명 징역 10개월", MOTHER_SENTENCED_ARTS)
        self.assertNotIn("한유명 징역 10개월", [kw for kw, _ in selected])
        self.assertNotIn("한유명 징역 10개월", [d for _, d in selected])

    # 필수 2: 유명인 본인이 징역 → 정상 귀속(과잉차단 회귀 방지).
    def test_self_sentenced_preserved(self):
        sig = _sig("한유명 징역 10개월", SELF_SENTENCED_ARTS)
        self.assertTrue(sig.get("crime_check_triggered"))
        self.assertTrue(sig.get("crime_attribution_verified_self"))
        self.assertFalse(sig.get("has_unsafe_crime_attribution"))
        self.assertIsNone(ranker._quality_gate_reason("한유명 징역 10개월", sig))

    # 관계를 표기에 드러낸 키워드는 안전명 → 검증 불필요(그대로 노출).
    def test_relation_labeled_keyword_is_safe_name(self):
        for kw in ("한유명 모친 사기 징역 10개월", "한유명 母 사기 실형",
                   "한유명 부친 횡령 구속"):
            self.assertFalse(cand.crime_keyword_requires_check(kw), kw)

    # 필수 3: 가족이 수사받고 유명인은 기사 context 에만 등장 → fail-closed.
    def test_family_investigated_celebrity_only_context(self):
        arts = [
            _raw("한유명 모친, 사기 혐의로 경찰 조사…1심 징역 구형", "a.example.com",
                 "한유명 모친 조사", 1),
            _raw("'한유명 딸 언급' 모친 사기 사건 징역 구형", "b.example.com",
                 "모친 사기 구형", 2),
            _raw("한유명 측 \"모친 사건과 무관\"…징역 선고 앞두고 입장", "c.example.com",
                 "한유명 측 입장", 3),
            _raw("한유명 모친 사기 사건 1심 징역 선고", "d.example.com", "모친 선고", 2),
        ]
        sig = _sig("한유명 징역", arts)
        self.assertTrue(sig.get("has_unsafe_crime_attribution"))

    # 필수 4: 같은 기사에 여러 인물이 있어도 처분 주체를 본인으로 오확정하지 않는다.
    def test_multiple_persons_no_self_overclaim(self):
        art = _raw("한유명 소속사 대표와 최유명, 사기 혐의로 징역 2년 선고",
                   desc="대표와 최유명이 징역 2년을 선고받았다")
        self.assertNotEqual(cand.classify_crime_subject_role("한유명", art), "subject")


class TestMergeDisplayCrimeAttribution(unittest.TestCase):
    """merge 표기 단계 우회 차단(PR #40 known risk 1 후속).

    candidate 게이트는 merge **이전** 키워드에만 걸린다. 그래서 각자는 안전한 후보가
    합쳐지면 _build_display_keyword 가 위험한 조합을 새로 만들 수 있다:
      · `"<이름> 사기"`  — 처분어가 없어 트리거 안 됨
      · `"징역 10개월"`   — 선두가 처분어라 이름 anchor 가 없어 트리거 안 됨
      → merge display `"<이름> 사기 징역 10개월"`
    실제로 재현됐고(운영 기사 8건 = 모친 사건), 조합 표기가 canonical 에 없던 형사
    처분 주장을 새로 만들면 canonical 로 되돌린다.
    """

    def _display(self, keywords, arts):
        r = replay_selection({
            "keywords": keywords,
            "articles_by_keyword": {k: arts for k in keywords},
            "sources_by_keyword": {k: {"nate_home": i + 1} for i, k in enumerate(keywords)},
        })
        return [(s["keyword"], s["display_keyword"]) for s in r["selected"]]

    def test_both_candidates_safe_but_merge_would_be_unsafe(self):
        # 전제: 두 후보 모두 candidate 게이트를 통과한다(= 우회 경로가 실재).
        for kw in ("한유명 사기", "징역 10개월"):
            self.assertFalse(cand.crime_keyword_requires_check(kw), kw)
        selected = self._display(["한유명 사기", "징역 10개월"], MOTHER_SENTENCED_ARTS)
        self.assertTrue(selected, "정상 후보가 통째로 사라지면 안 된다")
        for _, disp in selected:
            # 형을 받은 사람은 모친이다 → 이름 + 처분어 조합이 만들어지면 안 된다.
            self.assertNotIn("징역", disp, disp)
            self.assertFalse(cand.crime_keyword_requires_check(disp), disp)

    def test_disposition_leading_arrangement_also_blocked(self):
        # 조합 표기의 선두가 처분어면 이름 anchor 가 안 잡힌다("징역 10개월 한유명").
        # canonical 엔티티가 그 처분을 주장받는 형태로 세워 같은 판정기에 넘긴다.
        selected = self._display(["한유명", "징역 10개월"], MOTHER_SENTENCED_ARTS)
        self.assertTrue(selected)
        for _, disp in selected:
            self.assertNotIn("징역", disp, disp)

    def test_relation_labeled_display_is_preserved(self):
        # 관계를 표기에 드러낸 canonical 은 처분어를 붙여도 안전 → 그대로 유지한다
        # (관계어를 무조건 붙이지도, 무조건 떼지도 않는다).
        selected = self._display(["한유명 모친 사기", "징역 10개월"], MOTHER_SENTENCED_ARTS)
        self.assertIn(("한유명 모친 사기", "한유명 모친 사기 징역 10개월"), selected)

    def test_real_defendant_merge_keeps_disposition(self):
        # 본인이 실제 피고인이면 merge 표기가 처분어를 담아도 그대로 둔다(과잉차단 금지).
        selected = self._display(["한유명 사기", "징역 10개월"], SELF_SENTENCED_ARTS)
        self.assertTrue(selected)
        self.assertTrue(any("징역" in disp for _, disp in selected),
                        f"본인 사건인데 처분어가 사라졌다: {selected}")

    # --- 판정 함수 단위 계약 ---

    def test_guard_only_fires_on_newly_added_disposition(self):
        """이 guard 는 merge 가 **새로 더한** 형사 처분 주장에만 개입한다(범위 한정)."""
        mother = [{"title": t, "snippet": ""} for t in
                  ("한유명 모친, 1심서 징역 10개월", "한유명 母, 사기 혐의 징역 10개월")]
        # (a) 처분어를 새로 더하지 않으면 판정 대상이 아니다 — 기사가 모친 사건이어도.
        self.assertFalse(cand.display_adds_unsafe_crime_attribution(
            "한유명 사기 투자", "한유명 사기", mother))
        # (b) display == canonical 이면 판정 대상이 아니다.
        self.assertFalse(cand.display_adds_unsafe_crime_attribution(
            "한유명 사기", "한유명 사기", mother))
        # (c) 처분어를 새로 더했고 그 주장이 기사로 뒷받침되지 않으면 True.
        self.assertTrue(cand.display_adds_unsafe_crime_attribution(
            "한유명 사기 징역 10개월", "한유명 사기", mother))

    def test_guard_allows_added_disposition_when_person_is_subject(self):
        # 본인이 실제 피고인이면 처분어를 새로 더해도 되돌리지 않는다.
        self_arts = [{"title": a["title"], "snippet": ""} for a in SELF_SENTENCED_ARTS]
        self.assertFalse(cand.display_adds_unsafe_crime_attribution(
            "한유명 사기 징역 10개월", "한유명 사기", self_arts))

    def test_covers_others_path_is_guarded(self):
        """best 가 다른 member 를 문자로 포함해 그대로 display 가 되는 경로도 막는다.

        `"징역 10개월 <이름>"` 은 선두가 처분어라 candidate 게이트를 통과하고, 그 문자열이
        canonical `"<이름>"` 을 포함하므로 조합 없이 곧장 display 가 된다.
        """
        arts = [{"title": t, "snippet": "", "url": f"https://x/{i}", "press": "p",
                 "relevance_score": 0.9, "is_incidental": False, "is_primary_cluster": True}
                for i, t in enumerate(a["title"] for a in MOTHER_SENTENCED_ARTS)]
        kws = ["한유명", "징역 10개월 한유명"]
        for k in kws:
            self.assertFalse(cand.crime_keyword_requires_check(k), k)
        members = [{"keyword": k, "score": 1.0 - i * 0.1,
                    "news_meta": {"articles": arts}, "sources": {"nate_home": i + 1}}
                   for i, k in enumerate(kws)]
        self.assertEqual(ranker._build_display_keyword(members), "한유명")

    def test_guard_does_not_fire_without_new_disposition_claim(self):
        """처분 주장을 새로 더하지 않으면 개입하지 않는다 — 되돌리면 오히려 나빠진다.

        운영 관측(2026-09): canonical `"여고생 살해 사형"`(피해자가 선고받은 것처럼 읽힘)보다
        조합 표기 `"검찰 사형 구형 <피고인>"` 이 정확하다. 범위를 한정하지 않으면 정확한
        표기를 더 나쁜 canonical 로 되돌린다.
        """
        arts = [{"title": t, "snippet": ""} for t in (
            "'여고생 살해' 김유명에 사형 구형… \"영원히 격리해야\"",
            "검찰, 여고생 살해범 김유명에 사형 구형",
            "'여고생 살해범' 김유명 사형 구형…재범 위험 커",
            "여고생 살해 김유명 사형 구형")]
        self.assertFalse(cand.display_adds_unsafe_crime_attribution(
            "검찰 사형 구형 김유명", "여고생 살해 사형", arts))

    def test_guard_does_not_touch_non_crime_display(self):
        # 비범죄 조합 표기는 판정 대상 자체가 아니다(회귀 0).
        arts = [{"title": "한유명, 신곡 발표", "snippet": ""},
                {"title": "한유명 콘서트 매진", "snippet": ""}]
        self.assertFalse(cand.display_adds_unsafe_crime_attribution(
            "한유명 신곡 콘서트", "한유명", arts))


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
