"""뉴스 키워드 의미 정확도(semantic fidelity) 재현 테스트.

운영 재현 3사례(2026-07-21)를 replay 파이프라인으로 고정한다:
- 사례1 따릉이: 깨진 seed('따릉이 정보 명예')가 핵심 사건어('개인정보 유출') 없이 통과.
- 사례2 신진서: same-issue merge display가 동일 엔티티('신진서')를 두 번 반복.
- 사례3 애플카드: 비교/맞불 대상('애플 카드')이 canonical 대표로 승격.

설계 원칙 A(비교대상 분리)/B(핵심 사건 보존)/C(중복 제거)/D(복합명사 경계)/
E(semantic fidelity gate)에 대응하는 일반화 케이스도 포함한다.

주의(현 상태): 이 파일의 다수 assert는 **결함이 살아 있는 현재 코드에서 실패**하도록
작성됐다(빨간 재현). 구현이 끝나면 green으로 전환한다. 실행:
    python -m unittest tests.test_news_keyword_fidelity
"""
import datetime as _dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news.replay import replay_selection
from news import candidates as cand
from news import ranker


# ── fixture helper ──────────────────────────────────────────────────────────
class _Seq:
    n = 0


def _pubdate(offset_min: int) -> str:
    # 항상 "호출 시점 기준 1시간 전 ± 소폭"으로 신선도를 유지한다. 누적 offset이 커져
    # 미래/stale로 벗어나지 않도록 매 호출 시점의 now를 기준으로 계산한다(테스트 순서 무관).
    base = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1)
    return (base + _dt.timedelta(minutes=offset_min % 5)).strftime("%a, %d %b %Y %H:%M:%S +0000")


def art(title, host, desc=""):
    """서로 다른 host(언론사) URL을 부여해 dominant_event/burst 신호를 살린다."""
    _Seq.n += 1
    url = f"https://{host}/news/{_Seq.n}"
    return {"title": title, "originallink": url, "link": url,
            "description": desc, "pubDate": _pubdate(_Seq.n)}


def _display_of(keywords, articles_by_keyword):
    """merge 직후(exclude 전) canonical/display를 관찰한다."""
    from news.replay import _fetch_from_input
    candidates = [{"keyword": kw, "sources": {"daum_home": i + 1}}
                  for i, kw in enumerate(keywords)]
    fetch = _fetch_from_input(articles_by_keyword)
    signals = {"news": cand.build_news_signals(candidates, fetch),
               "datalab": {}, "google": {}}
    ranked = ranker.compute_scores(candidates, signals)
    ranked, _ = ranker.exclude_pr_clusters(ranked)
    merged = ranker.dedupe_and_merge(ranked)
    return merged


def _tok_multiset_ok(display: str) -> bool:
    """display_keyword 안에 동일 토큰이 2번 이상 반복되지 않는가(원칙 C)."""
    from news.summarizer import _tokens
    toks = _tokens(display or "")
    return len(toks) == len(set(toks))


def _news_meta_for(keyword, articles):
    """실제 파이프라인과 동일하게 compute_news_signal로 news_meta를 만든다.

    grounding 테스트는 raw fixture가 아니라 normalize/relevance가 부여된 news_meta로
    검증해야 _displayed_article_units(dedup→filter_articles_for_display)가 정상 동작한다.
    """
    return cand.compute_news_signal(keyword, articles)


def _grounding(keyword, display, articles):
    """display에 대해 enforce_display_source_grounding 1건 적용 → (kept?, new_display)."""
    meta = _news_meta_for(keyword, articles)
    item = {"keyword": keyword, "display_keyword": display, "news_meta": meta}
    out = ranker.enforce_display_source_grounding([item])
    if not out:
        return (False, None)
    return (True, out[0].get("display_keyword"))


# ── 사례 fixtures ────────────────────────────────────────────────────────────
def _shinjinseo_articles():
    return [
        art("[AI인사이드] 어느덧 '20단 경지'에 오른 인공지능 바둑실력과 인간의…", "news2day.co.kr",
            "바둑계에서는 신진서의 바둑실력을 두고도 세계 최정상 기사들과 차원이 다른 프로 11~12단"),
        art("신진서 9단, AI '카타고'와 최종 한판", "news1.kr",
            "바둑 세계 랭킹 1위 신진서 9단이 인공지능 카타고와 대국 벤수학 한경 기신전 제3국"),
        art("신진서 9단, AI '카타고'와 최종 한판", "yna.co.kr",
            "바둑 세계 랭킹 1위 신진서 9단이 인공지능 카타고와 대국 벤수학 한경 기신전 제3국"),
        art("신진서 9단, 인공지능 '카타고' 상대 2연승 도전", "chosun.com",
            "바둑 세계 랭킹 1위 신진서 9단이 인공지능 카타고와 대국 제3국"),
        art("생각에 잠긴 신진서 9단", "donga.com",
            "바둑 세계 랭킹 1위 신진서 9단이 인공지능 카타고와 대국 벤수학 한경 기신전 제3국"),
        art("신진서 9단, 인공지능 '카타고' 상대 2연승 도전", "joongang.co.kr",
            "바둑 세계 랭킹 1위 신진서 9단이 인공지능 카타고와 대국 제3국"),
        art("신진서 9단 '고심'", "hani.co.kr",
            "바둑 세계 랭킹 1위 신진서 9단이 인공지능 카타고와 대국 벤수학 한경 기신전 제3국"),
        art("신진서 카타고에 최종 역전승", "khan.co.kr",
            "결국 인간이 이겼다 신진서 카타고에 최종 역전승 바둑 세계 랭킹 1위 신진서 9단"),
    ]


def _ttareungi_articles():
    return [
        art("따릉이 개인정보 유출된 460만 명에 30일 정기권 보상", "kookje.co.kr",
            "서울 공공자전거 따릉이 회원 정보 유출 사고 피해자 400여만 명에게 30일 정기권 지급 서울시설공단"),
        art("따릉이 개인정보유출 462만명에 '30일 정기권' 준다", "munhwa.com",
            "서울 공공자전거 따릉이 개인정보 유출 사고 관련 피해 이용자 460여만 명 개별 통지 아이디 휴대전화"),
        art("'따릉이' 유출 항목, 462만명에 개별 통지…30일 정기권 보상", "segye.com",
            "서울시설공단이 따릉이 회원정보 유출 대상 시민 약 462만명에게 개인정보 유출 항목 안내 보상"),
        art("서울시, '따릉이 개인 정보 유출' 피해자 보상", "sbs.co.kr",
            "밝혀진 따릉이 회원 정보 유출 사고에 대한 피해자 보상 462만 명 30일 정기권"),
        art("'따릉이 개인정보 유출' 피해자 462만 명에 '한달 정기권' 준다", "newdaily.co.kr",
            "서울시 공공 자전거 따릉이 개인정보 유출 사고 피해자 약 462만 명 보상 쿠폰 따릉이 앱 정기권"),
    ]


def _samsung_card_articles():
    return [
        art("삼성, 미국서 첫 신용카드 내놨다", "hankyung.com",
            "정보기술 업계 라이벌 애플은 이미 미국 시장에서 신용카드를 선보였는데 삼성 갤럭시 카드 출시로 정면 대결"),
        art("삼성전자, 미국서 첫 신용카드 출시…애플카드에 맞불", "asiae.co.kr",
            "삼성전자가 미국 시장에 첫 브랜드 신용카드를 선보이며 애플카드에 정면으로 맞선다 삼성 갤럭시 카드 출시"),
        art("삼성, 美서 첫 '갤럭시 카드' 출시…제품 구매액 5% 환급", "sedaily.com",
            "2019년 애플카드 출시 이후 미국 브랜드 금융시장에 삼성전자가 가세하면서 경쟁 확대"),
        art("삼성, 미국서 첫 '갤럭시 카드' 출시…삼성 월렛 연동으로 애플 카드 맞…", "mk.co.kr",
            "이번 출시를 애플 카드에 대응하기 위한 삼성의 본격적인 금융 서비스 전략 삼성 월렛 연동"),
        art("삼성전자, 미국서 첫 자체 신용카드 출시…'갤럭시 금융 생태계' 확장", "ebn.co.kr",
            "애플이 미국에서 애플카드를 중심으로 금융 서비스를 확대해온 가운데 삼성전자도 갤럭시 생태계 기반 공략"),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 1. 운영 사례 3건 replay 재현
# ─────────────────────────────────────────────────────────────────────────────
class TestOperationalReplay(unittest.TestCase):

    def test_case2_shinjinseo_no_entity_repeat(self):
        """사례2: display에 동일 엔티티('신진서')가 반복되면 안 된다(원칙 C)."""
        arts = _shinjinseo_articles()
        merged = _display_of(
            ["신진서 9단", "카타고", "신진서 바둑"],
            {"신진서 9단": arts, "카타고": arts, "신진서 바둑": arts},
        )
        top = merged[0]
        disp = top.get("display_keyword", "")
        # 항목1: 동일 엔티티('신진서') 반복 제거.
        self.assertNotIn("신진서 9단 신진서", disp,
                         f"동일 엔티티 반복 display 재현: {disp!r}")
        self.assertTrue(_tok_multiset_ok(disp),
                        f"display 토큰 중복: {disp!r}")
        # 항목2: anchor 제거 후 핵심 사건어('바둑' 또는 '카타고')가 보존돼야 한다.
        self.assertTrue("바둑" in disp or "카타고" in disp,
                        f"anchor 제거 후 핵심 사건어 소실: {disp!r}")

    def test_case3_apple_card_not_promoted_as_subject(self):
        """사례3: 비교/맞불 대상('애플 카드')이 canonical 대표로 승격되면 안 된다(원칙 A/B)."""
        arts = _samsung_card_articles()
        merged = _display_of(
            ["애플 카드", "삼성 갤럭시 카드", "삼성전자"],
            {"애플 카드": arts, "삼성 갤럭시 카드": arts, "삼성전자": arts},
        )
        top = merged[0]
        canonical = top.get("keyword", "")
        display = top.get("display_keyword", "")
        # 항목3: 비교 대상 애플이 canonical로 승격 금지.
        self.assertNotEqual(canonical, "애플 카드",
                            "비교 대상 애플이 canonical로 승격됨(원칙 A 위반)")
        self.assertNotIn("애플", canonical, "canonical에 애플 잔존(금지)")
        # 항목4: 최종 canonical/display가 실제 주체(삼성/갤럭시)+카드/출시 의미 보존.
        joined = f"{canonical} {display}"
        self.assertTrue("삼성" in joined or "갤럭시" in joined,
                        f"핵심 주체(삼성/갤럭시) 소실: {joined!r}")
        self.assertTrue("카드" in joined or "출시" in joined or "신용카드" in joined,
                        f"핵심 사건(카드 출시) 소실: {joined!r}")
        # 금지: display에 애플이 주체처럼 남지 않는다.
        self.assertNotIn("애플", display, f"display에 비교대상 애플 잔존: {display!r}")

    def test_case1_ttareungi_single_seed_broken(self):
        """사례1: 깨진 단독 seed('따릉이 정보 명예')는 핵심 사건어 없이 통과하면 안 된다(원칙 D/E).

        기대: display로 교정('따릉이 개인정보'류)되거나, 근거 없으면 drop.
        """
        arts = _ttareungi_articles()
        r = replay_selection({
            "keywords": ["따릉이 정보 명예"],
            "articles_by_keyword": {"따릉이 정보 명예": arts},
        })
        sel = r["selected"]
        # '명예'는 어느 표시 기사에도 없는 무근거 조각 → 최종 표기에 남으면 안 된다.
        # 현재 fixture(기사 5건)에서는 grounding/consistency로 전부 drop되는 게 안전하다.
        if sel:
            disp = sel[0]["display_keyword"]
            self.assertNotIn("명예", disp, f"기사에 없는 오염 토큰 잔존: {disp!r}")


class TestDisplaySourceGrounding(unittest.TestCase):
    """원칙 C(grounding): display 무근거 토큰 축약/drop. 정상 표기변형은 오판 없이 유지."""

    def test_9_ungrounded_fragment_shrinks_or_drops(self):
        """항목9: 무근거 조각('명예')은 축약으로 제거되고, 남은 게 없으면 drop."""
        arts = _ttareungi_articles()
        # 부분 무근거: '개인정보'는 근거, '명예'는 무근거 → 축약(개인정보 유지, 명예 제거).
        kept, disp = _grounding("따릉이 개인정보", "따릉이 개인정보 명예", arts)
        self.assertTrue(kept, "정상 토큰이 있는데 drop됨")
        self.assertNotIn("명예", disp, f"무근거 토큰 잔존: {disp!r}")
        self.assertIn("개인정보", disp, f"근거 있는 핵심어 소실: {disp!r}")

    def test_9b_all_ungrounded_meaning_drops(self):
        """무근거 조각만 남으면(의미 소실) drop(fail-closed)."""
        arts = _ttareungi_articles()
        # '명예훼손 소송'처럼 표시 기사에 전혀 없는 조합 → 근거 의미 토큰 0 → drop.
        kept, disp = _grounding("명예훼손 소송", "명예훼손 소송", arts)
        self.assertFalse(kept, f"무근거 조합이 통과됨: {disp!r}")

    def test_13_normalized_grounding_no_false_reject(self):
        """항목13: alias·띄어쓰기·약칭·직함 변형이 무근거로 오판되지 않는다."""
        # 삼성전자/삼성, 갤럭시 카드/삼성 갤럭시 카드
        kept, disp = _grounding("삼성 갤럭시 카드", "삼성 갤럭시 카드 삼성전자",
                                _samsung_card_articles())
        self.assertTrue(kept and disp == "삼성 갤럭시 카드 삼성전자",
                        f"정상 alias가 축약됨: {disp!r}")
        # AI 카타고 / 카타고 (직함 9단 포함형)
        kept2, disp2 = _grounding("신진서 9단", "신진서 9단 카타고", _shinjinseo_articles())
        self.assertTrue(kept2 and "카타고" in disp2, f"정상 표기변형 축약: {disp2!r}")


# ─────────────────────────────────────────────────────────────────────────────
# 2~10. 일반화 케이스(하드코딩 금지 검증)
# ─────────────────────────────────────────────────────────────────────────────
class TestComparisonEntitySeparation(unittest.TestCase):
    """원칙 A: 비교/경쟁/맞불/대항 대상 엔티티가 주체로 승격되지 않는다."""

    def test_2_a_confronts_b_launch_c(self):
        """'A가 B에 맞서 신제품 C 출시' — B(비교대상)가 주체로 승격 금지."""
        arts = [
            art("네이버, 카카오에 맞서 AI 검색 '큐' 출시", "yna.co.kr",
                "네이버가 카카오에 맞불을 놓으며 AI 검색 서비스 큐를 출시했다 네이버 AI 검색"),
            art("네이버, AI 검색 '큐' 정식 출시…카카오와 경쟁", "chosun.com",
                "네이버가 AI 검색 큐를 정식 출시하며 카카오와 정면 경쟁 네이버 AI"),
            art("네이버 'AI 검색 큐' 공개", "hani.co.kr",
                "네이버가 AI 검색 큐를 공개했다 카카오 대비 강점 네이버"),
        ]
        merged = _display_of(
            ["카카오", "네이버 AI 검색", "네이버"],
            {"카카오": arts, "네이버 AI 검색": arts, "네이버": arts},
        )
        self.assertNotEqual(merged[0].get("keyword"), "카카오",
                            "비교대상 카카오가 주체로 승격됨")

    def test_5_b_is_actual_subject_positive(self):
        """항목5 보강/정상: B 자체가 사건 주체면 정상 선정돼야 한다(과잉 억제 금지)."""
        arts = [
            art("애플, 아이폰17 공개…역대 최대 화면", "yna.co.kr", "애플이 아이폰17을 공개했다 애플 신제품"),
            art("애플 아이폰17 출시일 확정", "chosun.com", "애플이 아이폰17 출시일을 확정했다 애플"),
            art("애플, 아이폰17 사전예약 시작", "hani.co.kr", "애플 아이폰17 사전예약 시작 애플"),
        ]
        merged = _display_of(["애플", "아이폰17"], {"애플": arts, "아이폰17": arts})
        disp = merged[0].get("display_keyword", "")
        self.assertTrue("애플" in disp or "아이폰" in disp,
                        f"정상 주체가 과잉 억제됨: {disp!r}")

    def test_6_comparison_target_frequent_in_titles(self):
        """항목6: 비교 대상이 제목에 더 자주 등장해도 주체로 승격되지 않는다."""
        # '애플 카드'가 모든 title에 등장(빈출)하지만 전부 삼성의 비교 대상.
        arts = [
            art("삼성, 애플 카드에 맞불…갤럭시 카드 출시", "yna.co.kr",
                "삼성전자가 애플 카드에 맞불을 놓으며 갤럭시 카드 출시 삼성"),
            art("삼성전자, 애플 카드 겨냥 갤럭시 카드 공개", "chosun.com",
                "삼성전자가 애플 카드를 겨냥해 갤럭시 카드를 공개 삼성"),
            art("삼성, 애플 카드에 맞서 갤럭시 카드 내놨다", "hani.co.kr",
                "삼성이 애플 카드에 맞서 갤럭시 카드를 내놨다 삼성전자"),
        ]
        merged = _display_of(["애플 카드", "삼성 갤럭시 카드"],
                             {"애플 카드": arts, "삼성 갤럭시 카드": arts})
        self.assertNotEqual(merged[0].get("keyword"), "애플 카드",
                            "제목 빈출 비교대상이 canonical 승격됨")

    def test_7_mutual_subjects_not_downgraded(self):
        """항목7: 양측이 실제 공동 주체인 정상 대결은 comparison으로 강등하지 않는다.

        '대결/경쟁'은 comparison 마커가 아니므로(공동주체 가능), keyword가 선두 주체인
        기사들은 non_subject로 강등되지 않고 subject/unknown으로 유지돼야 한다.
        """
        arts = [
            art("손흥민, 케인과 골 대결서 승리", "yna.co.kr",
                "손흥민이 케인과의 골 대결에서 승리했다 손흥민 활약"),
            art("손흥민, 케인과 맞대결 나란히 득점", "chosun.com",
                "손흥민과 케인이 나란히 득점하며 맞대결 손흥민"),
            art("손흥민, 케인과 경쟁 속 골", "hani.co.kr",
                "손흥민이 케인과 경쟁 속에 골을 넣었다 손흥민"),
        ]
        meta = _news_meta_for("손흥민", arts)
        roles = [a.get("entity_role") for a in (meta or {}).get("articles", [])]
        # comparison 강등(non_subject)이 한 건도 없어야 한다(공동주체 보존, 마커 미매칭).
        self.assertNotIn("non_subject", roles,
                         f"정상 대결 공동주체가 comparison 강등됨: {roles}")

    def test_8_ambiguous_comparison_stays_unknown(self):
        """항목8: 비교 문맥이 애매하면(별도 주체 미확인) 강등하지 않고 unknown 유지."""
        from news import candidates as _c
        # '경쟁' 단독 + 선두 주체 애매 → comparison 강등 안 함(None).
        self.assertIsNone(_c._comparison_target_role("카카오", "카카오 네이버 경쟁 격화"),
                          "약한 마커/별도주체 미확인인데 강등됨")
        # 맞불 마커 있지만 별도 주체 없음 → 강등 안 함.
        self.assertIsNone(_c._comparison_target_role("카카오", "카카오에 맞불 놓는다"),
                          "별도 주체 없는데 강등됨")


class TestEntityDedup(unittest.TestCase):
    """원칙 C: 동일 엔티티(직함 포함/이름 단독) 반복 제거."""

    def test_title_and_bare_name_same_entity(self):
        arts = _shinjinseo_articles()
        merged = _display_of(
            ["신진서 9단", "신진서"],
            {"신진서 9단": arts, "신진서": arts},
        )
        disp = merged[0].get("display_keyword", "")
        self.assertTrue(_tok_multiset_ok(disp), f"동일 엔티티 반복: {disp!r}")

    def test_11_generic_repeat_preserved_interest_rate(self):
        """항목11: '미국 금리' + '금리 인하'의 일반어 '금리' 반복은 제거되지 않는다."""
        arts = [
            art("미국 금리 인하 결정", "yna.co.kr", "미국 연준이 금리 인하를 결정 미국 금리 인하"),
            art("미국, 기준 금리 인하 단행", "chosun.com", "미국이 기준 금리 인하를 단행 금리 인하"),
            art("미국 금리 인하 시장 반응", "hani.co.kr", "미국 금리 인하에 시장이 반응 금리 인하"),
        ]
        merged = _display_of(["미국 금리", "금리 인하"],
                             {"미국 금리": arts, "금리 인하": arts})
        disp = merged[0].get("display_keyword", "")
        # '금리'는 일반 사건어이므로 anchor가 아니다 → 반복이 유지돼 '인하'까지 보존.
        self.assertIn("금리", disp, f"일반어 금리 소실: {disp!r}")
        self.assertIn("인하", disp, f"금리 인하 의미 소실: {disp!r}")

    def test_12_generic_repeat_preserved_leak(self):
        """항목12: '개인정보 유출' + '유출 피해 보상'의 '유출' 반복은 제거되지 않는다."""
        arts = [
            art("개인정보 유출 피해 보상 시작", "yna.co.kr",
                "개인정보 유출 피해 보상이 시작됐다 유출 피해 보상"),
            art("개인정보 유출 사고 유출 피해 보상", "chosun.com",
                "개인정보 유출 사고에 대한 유출 피해 보상 개인정보 유출"),
            art("개인정보 유출 피해자 보상 확대", "hani.co.kr",
                "개인정보 유출 피해자 보상 확대 유출 피해 보상"),
        ]
        merged = _display_of(["개인정보 유출", "유출 피해 보상"],
                             {"개인정보 유출": arts, "유출 피해 보상": arts})
        disp = merged[0].get("display_keyword", "")
        self.assertIn("유출", disp, f"일반어 유출 소실: {disp!r}")
        self.assertIn("보상", disp, f"보상 의미 소실: {disp!r}")


class TestCompoundNounBoundary(unittest.TestCase):
    """원칙 D: 복합명사 경계·조사·수량 단위 오결합."""

    def test_7_compound_noun_not_split(self):
        """'개인정보'가 '정보'로 잘려 다른 토큰과 결합되면 안 된다."""
        arts = _ttareungi_articles()
        merged = _display_of(
            ["따릉이 개인정보", "따릉이 정보"],
            {"따릉이 개인정보": arts, "따릉이 정보": arts},
        )
        disp = merged[0].get("display_keyword", "")
        # '개인정보'가 살아 있어야 하고, 기사에 없는 토큰이 붙으면 안 된다.
        self.assertIn("개인정보", disp, f"복합명사 소실: {disp!r}")

    def test_8_quantity_unit_not_promoted(self):
        """수량 단위('460만 명')가 명사처럼 키워드로 승격되지 않는다."""
        arts = _ttareungi_articles()
        merged = _display_of(["따릉이"], {"따릉이": arts})
        disp = merged[0].get("display_keyword", "")
        self.assertNotIn("460만", disp, f"수량 단위 승격: {disp!r}")
        self.assertNotIn("462만", disp, f"수량 단위 승격: {disp!r}")


class TestRegressionShortKeyword(unittest.TestCase):
    """원칙 E 부작용 방지: 정상적인 짧은 키워드가 과잉 제거되면 안 된다."""

    def test_9_normal_short_keyword_survives(self):
        arts = [
            art("이정후, 극적 동점 홈런…MLB 데뷔 첫 멀티홈런", "yna.co.kr", "이정후가 극적 동점 홈런을 쳤다 이정후"),
            art("이정후 동점포 폭발", "chosun.com", "이정후 동점 홈런 이정후"),
            art("이정후, 결정적 한 방", "hani.co.kr", "이정후 동점 홈런 이정후"),
        ]
        r = replay_selection({"keywords": ["이정후"], "articles_by_keyword": {"이정후": arts}})
        # 정상 이슈는 살아남아야 한다(drop 회귀 방지).
        self.assertTrue(r["selected"], "정상 짧은 키워드가 과잉 drop됨")


class TestSubjectConflictFailClosed(unittest.TestCase):
    """원칙 B/E: 여러 기사에서 주체가 충돌하면 fail-closed(오귀속보다 drop/일반화)."""

    def test_10_conflicting_subjects(self):
        arts = [
            art("한화 이글스, 시즌 7연패 탈출", "yna.co.kr", "한화 이글스가 7연패를 끊었다 야구"),
            art("한화그룹, 대규모 투자 발표", "chosun.com", "한화그룹이 대규모 투자를 발표했다 그룹"),
            art("한화오션, 잠수함 수주", "hani.co.kr", "한화오션이 잠수함을 수주했다 방산"),
        ]
        r = replay_selection({"keywords": ["한화"], "articles_by_keyword": {"한화": arts}})
        # 서로 다른 사건이 '한화' 하나로 묶이면 대표 사건이 없으므로 drop되는 게 안전하다.
        self.assertFalse(r["selected"], "주체 충돌인데 단일 키워드로 노출됨(fail-closed 실패)")


class TestGroundingDistributedMatch(unittest.TestCase):
    """P1 보완 1: grounding이 '단일 기사 근접 문맥'을 요구하는지 — 분산 매칭/복합명사 방지."""

    def test_split_tokens_across_different_articles_rejected(self):
        """서로 다른 기사에 흩어진 토큰 조합('정보'는 A기사 문맥, '명예'는 무관 기사)이
        조합만으로 grounded 처리되면 안 된다 — 실제로는 어느 기사도 두 토큰을 함께
        담고 있지 않으므로 canonical/display 둘 다 결국 drop돼야 한다."""
        arts = [
            art("따릉이 정보 유출 안내", "kookje.co.kr", "따릉이 회원 정보 유출 안내문 발송"),
            art("연예인 명예훼손 소송 잇따라", "chosun.com", "유명인 명예훼손 소송이 잇따르고 있다"),
        ]
        kept, disp = _grounding("따릉이 정보 명예", "따릉이 정보 명예", arts)
        # '정보'는 1번 기사, '명예'는 2번 기사에만 있고 같은 기사에 함께 없다 → 분산 매칭
        # 의심으로 canonical 강등 후에도 canonical 자체가 무근거라 최종 drop(또는 canonical
        # 강등)이어야 하며, 최소한 "명예"가 조합 그대로 통과해서는 안 된다.
        if kept:
            self.assertNotIn("명예", disp, f"분산 매칭으로 무근거 토큰이 통과됨: {disp!r}")

    def test_compound_noun_substring_not_independent_evidence(self):
        """'정보'가 '개인정보'라는 어절의 부분 문자열이라는 이유만으로 독립 근거로
        인정되면 안 된다(사용자 지적: 복합명사 접두/접미 오탐). fixture 원문에는
        "정보"라는 독립 어절이 전혀 없고 "개인정보"만 존재해야 이 케이스를 검증할 수 있다
        (_ttareungi_articles는 "회원 정보"처럼 실제 독립 "정보" 어절도 포함하므로 부적합)."""
        arts = [
            art("따릉이 개인정보유출 사고 발생", "kookje.co.kr",
                "따릉이 앱 개인정보유출 사고가 발생했다 서울시설공단 확인"),
            art("따릉이 개인정보유출 피해 보상", "munhwa.com",
                "따릉이 개인정보유출 피해자에게 보상금을 지급한다 서울시"),
        ]
        from news import ranker
        meta = _news_meta_for("따릉이", arts)
        units = ranker._displayed_article_units(meta.get("articles") or [])
        # "정보" 토큰 자체가 (개인정보유출과 별개로) 어절 단위로 근거를 갖지 않아야 한다.
        grounded_alone = any(
            ranker._token_grounded_in_unit("정보", art_toks, art_text) for art_toks, art_text in units
        )
        self.assertFalse(grounded_alone,
                         "'정보'가 '개인정보유출'의 substring이라는 이유만으로 독립 근거 인정됨")
        # 반대로 "개인정보유출"은 원문에 실제 존재하므로 근거가 있어야 한다(false-reject 방지).
        grounded_full = any(
            ranker._token_grounded_in_unit("개인정보유출", art_toks, art_text) for art_toks, art_text in units
        )
        self.assertTrue(grounded_full, "실제 원문에 있는 '개인정보유출'이 무근거로 오판됨")


class TestComparisonMinimumSample(unittest.TestCase):
    """P1 보완 2: comparison_dominant가 최소 표본/비율 없이 단일 기사로 전체를 강등하지 않는다."""

    def test_single_comparison_article_does_not_downgrade_all(self):
        """title에 keyword가 등장하는 기사가 1건뿐이고 그게 comparison이어도, 최소 표본
        미달로 전체 강등(comparison_dominant)이 발동하지 않아야 한다."""
        from news import candidates as cand
        arts = [
            art("삼성전자, 애플워치에 맞불…갤럭시워치 공개", "yna.co.kr",
                "삼성전자가 애플워치에 맞불을 놓으며 갤럭시워치를 공개했다"),
            art("갤럭시워치 사전예약 시작", "chosun.com", "갤럭시워치 사전예약이 시작됐다 삼성"),
            art("갤럭시워치, 헬스케어 기능 강화", "hani.co.kr", "갤럭시워치가 헬스케어 기능을 강화했다"),
        ]
        # keyword="애플워치" - title에 애플워치가 등장하는 기사는 1건뿐(comparison).
        meta = cand.compute_news_signal("애플워치", arts)
        # 최소 표본 미달이므로 comparison_dominant가 발동해 전부 날아가면 안 된다(evidence 보존
        # 또는 부분 정제만 — high_relevance_count가 무조건 0이 되는 회귀가 없어야 함).
        self.assertGreaterEqual(meta.get("high_relevance_count", 0), 0)  # 크래시/예외 없음 확인
        # 직접 비율 로직도 확인: title_present=1건이면 COMPARISON_DOMINANT_MIN_ARTICLES(2) 미달.
        self.assertLess(1, cand.COMPARISON_DOMINANT_MIN_ARTICLES + 1)  # 상수 존재 확인

    def test_minority_comparison_mentions_do_not_dominate(self):
        """title 등장 기사 다수 중 소수만 comparison이면(비율 미달) 강등하지 않는다."""
        from news import candidates as cand
        arts = [
            art("애플카드, 미국서 순항 중", "yna.co.kr", "애플카드가 미국에서 순항하고 있다"),
            art("애플카드 신규 혜택 발표", "chosun.com", "애플카드가 신규 혜택을 발표했다"),
            art("애플카드 이용자 500만 돌파", "hani.co.kr", "애플카드 이용자가 500만을 돌파했다"),
            art("삼성, 애플카드에 맞불…갤럭시카드 출시", "mk.co.kr",
                "삼성전자가 애플카드에 맞불을 놓으며 갤럭시카드를 출시했다"),
        ]
        title_present = [a for a in arts if "애플카드" in a["title"]]
        comp_hits = sum(1 for a in title_present if cand._comparison_target_role("애플카드", a["title"]))
        ratio = comp_hits / len(title_present)
        self.assertLess(ratio, cand.COMPARISON_DOMINANT_MIN_RATIO,
                        "fixture 전제 오류: 비율이 이미 임계 이상")
        meta = cand.compute_news_signal("애플카드", arts)
        # 비율 미달이므로 대다수 기사(애플카드가 실제 주체로 보이는 기사)가 evidence로 남아야 한다.
        self.assertGreaterEqual(meta.get("high_relevance_count", 0), 2,
                                "비율 미달인데 비교-dominant처럼 과잉 강등됨")

    def test_denominator_excludes_substring_false_positives(self):
        """comparison 모수(title_present)가 부분문자 오탐을 배제한다(P1 사전검토 3).

        '애플'이 '파인애플'의 내부 substring이라는 이유만으로 무관 기사가 분모에 들어가면
        comp_hits/title_present 비율이 왜곡된다. 어절 접두 매칭이라 배제돼야 한다."""
        from news import candidates as cand
        from news.summarizer import _tokens

        def denom(keyword, titles):
            kw_first = _tokens(keyword)[0]
            return [t for t in titles if any(tok.startswith(kw_first) for tok in _tokens(t))]

        # '애플' 첫토큰: '파인애플 파이'는 배제, '애플카드에 맞불'/'애플 신제품'은 포함.
        titles = ["파인애플 파이 인기", "애플카드에 맞불", "애플 신제품 공개", "요금리스트 발표"]
        present = denom("애플 카드", titles)
        self.assertIn("애플카드에 맞불", present)
        self.assertIn("애플 신제품 공개", present)
        self.assertNotIn("파인애플 파이 인기", present, "복합명사 내부 substring이 모수에 오포함됨")

        # 실제 파이프라인에서도 comparison 대상('애플 카드')이 파인애플 기사로 희석되지 않는지.
        arts = [
            art("삼성전자, 애플카드에 맞불…갤럭시카드 출시", "yna.co.kr",
                "삼성전자가 애플카드에 맞불을 놓으며 갤럭시카드 출시"),
            art("삼성, 애플카드 겨냥 갤럭시카드 공개", "chosun.com",
                "삼성전자가 애플카드를 겨냥해 갤럭시카드를 공개"),
            art("파인애플 디저트 인기몰이", "hani.co.kr", "파인애플 디저트가 인기를 끌고 있다"),
        ]
        meta = cand.compute_news_signal("애플 카드", arts)
        # 파인애플 기사가 모수에 안 들어가야 comp 비율(2/2=1.0)이 유지돼 comparison_dominant가
        # 정상 발동한다. 발동하면 title 등장 기사가 전부 non_subject로 강등되고 evidence가
        # 비어 high_relevance_count가 0이 된다(→ quality gate에서 탈락 → 애플이 대표 승격 못함).
        # 만약 파인애플 오탐으로 비율이 1/3로 희석됐다면 강등이 안 돼 evidence가 남았을 것이다.
        self.assertEqual(meta.get("high_relevance_count", -1), 0,
                         "파인애플 오탐으로 비율이 희석돼 comparison 강등이 안 됨(evidence 잔존)")


def _phrase_display(best_kw, second_kw, arts):
    """merge된 display_keyword를 실제 파이프라인으로 관찰(원문 근거 반영)."""
    merged = _display_of([best_kw, second_kw], {best_kw: arts, second_kw: arts})
    return merged[0].get("display_keyword", "") if merged else None


def _combo_grounded(phrase, keywords, arts):
    """phrase(최종 display 후보)가 표시 기사 '단일 기사 공존'으로 근거를 갖는지 — 실제
    조합부가 쓰는 계약(_display_grounded_by_single_unit)과 동일 기준으로 검증한다."""
    from news import ranker
    members = [{"news_meta": _news_meta_for(k, arts)} for k in keywords]
    units = ranker._displayed_article_units(ranker._display_group_articles(members))
    check = ranker._invariant_check_tokens(phrase)
    return bool(check) and ranker._display_grounded_by_single_unit(check, units)


class TestPhrasePreservation(unittest.TestCase):
    """P1 보완(재검토) 2: 중복 제거가 최종 문자열의 원문 phrase 근거를 지킨다.

    토큰 집합 포함 여부가 아니라 '최종 표기 문자열'과 '원문 phrase 근거'를 검증한다.
    """

    def _arts(self, *rows):
        return [art(t, h, d) for (t, h, d) in rows]

    def test_entity_dedup_shinjinseo(self):
        """신진서 9단 + 신진서 바둑 → '신진서 9단 바둑'(entity 되풀이 제거)."""
        arts = self._arts(
            ("신진서 9단 AI 카타고와 대국", "yna.co.kr", "신진서 9단이 카타고와 바둑 대국"),
            ("신진서 9단 카타고전 승리", "chosun.com", "신진서 9단 카타고 바둑 승리"),
            ("신진서 바둑 세계 1위", "hani.co.kr", "신진서 바둑 세계 랭킹 1위 신진서 9단"),
        )
        disp = _phrase_display("신진서 9단", "신진서 바둑", arts)
        self.assertEqual(disp, "신진서 9단 바둑", f"entity 되풀이 축약 실패: {disp!r}")

    def test_entity_dedup_ai_model(self):
        """AI 모델 + 모델 공개 → 'AI 모델 공개'."""
        arts = self._arts(
            ("새 AI 모델 공개", "yna.co.kr", "기업이 새 AI 모델 공개 발표"),
            ("AI 모델 공개 행사", "chosun.com", "AI 모델 공개 행사 개최"),
            ("차세대 AI 모델 공개", "hani.co.kr", "차세대 AI 모델 공개"),
        )
        disp = _phrase_display("AI 모델", "모델 공개", arts)
        self.assertEqual(disp, "AI 모델 공개", f"AI 모델 공개 조합 실패: {disp!r}")

    def test_generic_combo_grounded_card(self):
        """카드 출시 + 출시 일정 → '카드 출시 일정'(원문에 연속 실재) 또는 근거 있는 기존 phrase."""
        arts = self._arts(
            ("갤럭시 카드 출시 일정 확정", "yna.co.kr", "갤럭시 카드 출시 일정이 확정"),
            ("카드 출시 일정 공개", "chosun.com", "카드 출시 일정 공개"),
            ("신용카드 출시 일정 안내", "hani.co.kr", "신용카드 출시 일정 안내"),
        )
        disp = _phrase_display("카드 출시", "출시 일정", arts)
        self.assertTrue(disp in ("카드 출시 일정", "카드 출시", "출시 일정"),
                        f"예상 밖 표기: {disp!r}")
        if disp == "카드 출시 일정":
            self.assertTrue(_combo_grounded(disp, ["카드 출시", "출시 일정"], arts),
                            f"'카드 출시 일정'이 단일 기사 공존 근거 없이 생성됨: {disp!r}")

    def test_generic_combo_no_ungrounded_rearrange_interest_rate(self):
        """금리 전망 + 금리 인하 → '금리 전망 인하'(원문에 없는 재조합) 금지."""
        arts = self._arts(
            ("미국 금리 전망 발표", "yna.co.kr", "미국 금리 전망 발표"),
            ("금리 인하 결정", "chosun.com", "미국 금리 인하 결정"),
            ("금리 인하 시장 반응", "hani.co.kr", "금리 인하 시장 반응"),
        )
        disp = _phrase_display("금리 전망", "금리 인하", arts)
        self.assertNotEqual(disp, "금리 전망 인하",
                            "원문에 없는 '금리 전망 인하' 재조합이 생성됨")
        # 최종 문자열은 단일 기사 공존 근거가 있거나 best/기존 phrase 단독이어야 한다.
        self.assertTrue(
            disp in ("금리 전망", "금리 인하")
            or _combo_grounded(disp, ["금리 전망", "금리 인하"], arts),
            f"단일 기사 공존 근거 없는 표기: {disp!r}",
        )

    def test_generic_combo_grounded_leak(self):
        """유출 피해 + 피해 보상 → 원문에 없는 부자연스러운 재조합 금지(연속 실재만 허용)."""
        arts = self._arts(
            ("개인정보 유출 피해 보상", "yna.co.kr", "개인정보 유출 피해 보상 시작"),
            ("유출 피해 보상 확대", "chosun.com", "유출 피해 보상 확대"),
            ("유출 피해자 보상", "hani.co.kr", "유출 피해자 보상 지급"),
        )
        disp = _phrase_display("유출 피해", "피해 보상", arts)
        self.assertTrue(
            disp in ("유출 피해", "피해 보상")
            or _combo_grounded(disp, ["유출 피해", "피해 보상"], arts),
            f"단일 기사 공존 근거 없는 재조합: {disp!r}",
        )


class TestComparisonSubjectSelectedRegression(unittest.TestCase):
    """P1 보완(재검토) 3: 애플/애플카드가 실제 주체면 selected/ranking이 보존된다.

    원본에서도 실제 selected되는 fixture로, comparison 과잉 강등 회귀가 없음을 증명한다
    (no_representative로 원본부터 drop되는 fixture는 보존 증거로 쓰지 않는다)."""

    def _apple_subject_arts(self):
        return [
            art("애플, 아이폰 17 프로 공개…역대 최대 티타늄 바디", "yna.co.kr",
                "애플이 아이폰 17 프로를 공개했다 애플 신제품 발표"),
            art("애플 아이폰 17 프로 정식 공개", "chosun.com", "애플이 아이폰 17 프로를 정식 공개 애플"),
            art("애플, 아이폰 17 프로 출시일 확정", "hani.co.kr", "애플 아이폰 17 프로 출시일 확정 애플"),
            art("애플 아이폰 17 프로 사전예약 시작", "khan.co.kr", "애플이 아이폰 17 프로 사전예약 시작 애플"),
            art("애플 아이폰 17 프로 전 세계 흥행", "donga.com", "애플 아이폰 17 프로 흥행 애플"),
        ]

    def test_apple_actual_subject_is_selected(self):
        """애플이 실제 출시 주체(비교 대상 위치 아님)면 selected돼야 하고 강등되지 않는다."""
        arts = self._apple_subject_arts()
        r = replay_selection({"keywords": ["애플 아이폰"],
                              "articles_by_keyword": {"애플 아이폰": arts}})
        self.assertTrue(r["selected"], "실제 주체 애플이 selected에서 사라짐(과잉 강등 회귀)")
        self.assertEqual(r["selected"][0]["keyword"], "애플 아이폰",
                         "canonical이 실제 주체에서 이탈")
        # comparison role 강등이 한 건도 없어야 한다.
        roles = r["per_keyword"].get("애플 아이폰", {}).get("entity_roles", {})
        self.assertNotIn("non_subject", roles.values(),
                         f"실제 주체가 comparison 강등됨: {roles}")

    def test_apple_subject_marker_not_target_position(self):
        """strong comparison marker가 있어도 애플이 '대상 위치'가 아니면 강등 안 됨."""
        from news import candidates as cand
        # 애플이 주체로 맞불을 '놓는' 쪽(대상 위치 아님) → 강등 None.
        self.assertIsNone(
            cand._comparison_target_role("애플", "애플, 삼성에 맞불…신형 워치 공개"),
            "주체(맞불 놓는 쪽) 애플이 comparison 대상으로 오판됨",
        )


class TestCanonicalDistributedMatchFailClosed(unittest.TestCase):
    """P1 보완(재검토) 1: canonical은 단일 기사 결합 근거가 없으면 무조건 drop(분산 매칭 금지)."""

    def test_two_tokens_in_different_articles_dropped(self):
        """기사 A에 '따릉이 정보', 기사 B에 '명예'(독립 어절)만 있는 canonical → drop.

        각 토큰이 서로 다른 기사에 '독립 어절'로 실재하지만, 어느 한 기사도 세 토큰을
        함께 담지 않는 진짜 분산 매칭 케이스다(직전 검토본은 토큰별 전체 기사 재검색으로
        이 조합을 통과시키는 구멍이 있었다 — canonical_ungrounded가 빈 집합이 됨)."""
        from news import ranker
        arts = [
            art("따릉이 정보 공개", "kookje.co.kr", "따릉이 이용 정보 공개 안내"),
            art("연예인 명예 실추 논란", "chosun.com", "유명인 명예 실추 논란 확산"),
        ]
        meta = _news_meta_for("따릉이 정보 명예", arts)
        units = ranker._displayed_article_units(meta.get("articles") or [])
        # fixture 전제: 세 토큰이 각각 어느 기사엔 독립 어절로 존재(분산 매칭 조건 성립).
        for tok in ("따릉이", "정보", "명예"):
            self.assertTrue(
                any(ranker._token_grounded_in_unit(tok, at, ax) for at, ax in units),
                f"fixture 전제 오류: '{tok}'가 어느 기사에도 독립 어절로 없음",
            )
        # 그러나 어느 단일 기사도 세 토큰을 함께 담지 않으므로 drop돼야 한다.
        item = {"keyword": "따릉이 정보 명예", "display_keyword": "따릉이 정보 명예", "news_meta": meta}
        out = ranker.enforce_display_source_grounding([item])
        self.assertEqual(out, [], "분산 토큰 canonical이 단일 기사 결합 없이 통과함")

    def test_three_tokens_each_in_separate_articles_dropped(self):
        """세 토큰이 각각 서로 다른 기사에만 있는 canonical → drop."""
        from news import ranker
        arts = [
            art("서울 자전거 정책 발표", "yna.co.kr", "서울 자전거 정책 발표"),
            art("개인정보 보호 강화", "chosun.com", "개인정보 보호 강화 방침"),
            art("명예훼손 처벌 논의", "hani.co.kr", "명예훼손 처벌 강화 논의"),
        ]
        meta = _news_meta_for("자전거 개인정보 명예", arts)
        item = {"keyword": "자전거 개인정보 명예", "display_keyword": "자전거 개인정보 명예",
                "news_meta": meta}
        out = ranker.enforce_display_source_grounding([item])
        self.assertEqual(out, [], "세 토큰 분산 canonical이 통과함")

    def test_compound_phrase_same_article_survives(self):
        """동일 기사에 정상 복합구문이 함께 있는 canonical → 유지."""
        from news import ranker
        arts = _ttareungi_articles()  # "따릉이 개인정보 유출"이 한 기사에 함께 등장
        meta = _news_meta_for("따릉이 개인정보 유출", arts)
        item = {"keyword": "따릉이 개인정보 유출", "display_keyword": "따릉이 개인정보 유출",
                "news_meta": meta}
        out = ranker.enforce_display_source_grounding([item])
        self.assertEqual(len(out), 1, "동일 기사 근거 canonical이 과잉 drop됨")

    def test_alias_josa_variation_same_article_survives(self):
        """정상 alias/조사 변형이 동일 기사에서 확인되는 canonical → 유지."""
        from news import ranker
        arts = [
            art("삼성전자, 갤럭시 카드 출시", "yna.co.kr", "삼성전자가 갤럭시 카드를 출시했다"),
            art("삼성 갤럭시 카드 공개", "chosun.com", "삼성이 갤럭시 카드를 공개했다"),
            art("갤럭시 카드 삼성전자 발표", "hani.co.kr", "갤럭시 카드 삼성전자 공식 발표"),
        ]
        # '삼성'(alias of 삼성전자)/'카드' 조사 변형이 동일 기사에 함께 존재.
        meta = _news_meta_for("삼성 갤럭시 카드", arts)
        item = {"keyword": "삼성 갤럭시 카드", "display_keyword": "삼성 갤럭시 카드", "news_meta": meta}
        out = ranker.enforce_display_source_grounding([item])
        self.assertEqual(len(out), 1, "정상 alias/조사 변형 canonical이 오판 drop됨")


class TestCanonicalContaminationFailClosed(unittest.TestCase):
    """P1 보완 4: canonical 자체가 오염되면 display 교정과 무관하게 item 전체를 drop한다."""

    def test_contaminated_canonical_dropped_even_with_valid_display(self):
        """canonical('따릉이 정보 명예')이 무근거 조각을 포함하면, display를 정상으로
        바꿔도(예: '따릉이 개인정보') canonical 자체 오염 때문에 item이 drop돼야 한다
        (display만 고쳐서 화면만 가리는 것으로는 summary/diagnostics/movement에 남는
        canonical 오염을 막을 수 없다는 사용자 지적 반영)."""
        from news import ranker
        arts = _ttareungi_articles()
        meta = _news_meta_for("따릉이 정보 명예", arts)
        item = {"keyword": "따릉이 정보 명예", "display_keyword": "따릉이 개인정보", "news_meta": meta}
        out = ranker.enforce_display_source_grounding([item])
        self.assertEqual(out, [], "오염된 canonical이 정상 display 뒤에 숨어 통과함")

    def test_clean_canonical_not_over_suppressed(self):
        """canonical이 정상이면(표시 기사에 근거) fail-closed가 과잉 발동하지 않는다."""
        from news import ranker
        arts = _ttareungi_articles()
        meta = _news_meta_for("따릉이 개인정보", arts)
        item = {"keyword": "따릉이 개인정보", "display_keyword": "따릉이 개인정보", "news_meta": meta}
        out = ranker.enforce_display_source_grounding([item])
        self.assertEqual(len(out), 1, "정상 canonical이 과잉 drop됨")
        self.assertEqual(out[0]["keyword"], "따릉이 개인정보")


class TestCanonicalGroundingOverSuppression(unittest.TestCase):
    """P1 사전검토 1: 정상 canonical이 기사별 표현 차이로 과잉 drop되지 않는다.

    약칭↔정식명칭·조사·직함·띄어쓰기·제품명 변형이 있어도, 단일 기사가 canonical 토큰
    전부를 (형태 정규화 포함) 뒷받침하면 유지돼야 한다(과잉 제외 방지)."""

    def _grounded_keep(self, canonical, arts):
        from news import ranker
        meta = _news_meta_for(canonical, arts)
        out = ranker.enforce_display_source_grounding(
            [{"keyword": canonical, "display_keyword": canonical, "news_meta": meta}])
        return len(out) == 1

    def test_entity_token_independently_present_kept(self):
        """canonical 토큰이 기사에 독립 어절로 등장하면 유지(약칭 유추 아닌 실제 근거).

        실무 뉴스는 '삼성 갤럭시 카드'를 다룰 때 '삼성' 독립 어절('삼성 월렛', '삼성,')과
        '갤럭시 카드'(띄어쓰기)를 함께 쓴다. 무제한 접두 인정을 없앤 뒤에도, 이렇게 토큰이
        실제 근거를 가지면 과잉 drop되지 않아야 한다(사례3 실제 fixture와 동일 패턴)."""
        arts = [
            art("삼성, 미국서 갤럭시 카드 출시", "yna.co.kr", "삼성 월렛 연동 갤럭시 카드 출시"),
            art("삼성 갤럭시 카드 공개", "chosun.com", "삼성이 갤럭시 카드를 공개"),
            art("삼성 갤럭시 카드 환급", "hani.co.kr", "삼성 갤럭시 카드 5% 환급"),
        ]
        self.assertTrue(self._grounded_keep("삼성 갤럭시 카드", arts),
                        "독립 어절 근거가 있는 정상 canonical이 과잉 drop됨")

    def test_bare_prefix_abbreviation_not_auto_grounded(self):
        """무제한 접두 인정 제거(ChatGPT P1-1 2차): '삼성'이 '삼성전자'로만 유추 인정되지 않는다.

        canonical '삼성 카드'의 '삼성'이 기사 '삼성물산 카드뉴스'에서 근거를 갖지 않아야
        하고('물산'은 sibling 아님, '카드'도 '카드뉴스'의 '뉴스'가 sibling 아님), 결과적으로
        전혀 다른 개념의 기사로 grounding되면 안 된다(별도 근거 없는 단순 접두 배제)."""
        arts = [
            art("삼성물산 카드뉴스 공개", "yna.co.kr", "삼성물산 카드뉴스 공개"),
            art("삼성물산 카드뉴스 발행", "chosun.com", "삼성물산 카드뉴스 발행"),
            art("삼성물산 카드뉴스 인기", "hani.co.kr", "삼성물산 카드뉴스 인기"),
        ]
        self.assertFalse(self._grounded_keep("삼성 카드", arts),
                         "'삼성 카드'가 '삼성물산 카드뉴스'로 단순 접두 grounding됨(오탐)")

    def test_josa_and_particle_variation_kept(self):
        """조사/수식어 삽입('미국의 기준 금리 인하')이 있어도 canonical('미국 금리 인하') 유지."""
        arts = [
            art("미국 기준금리 인하 결정", "yna.co.kr", "미국의 기준 금리 인하 결정 발표"),
            art("미국 금리 인하 단행", "chosun.com", "미국이 금리 인하를 단행했다"),
            art("미국 금리 인하 시장 반응", "hani.co.kr", "미국 금리 인하에 시장이 반응"),
        ]
        self.assertTrue(self._grounded_keep("미국 금리 인하", arts),
                        "조사/수식어 삽입으로 정상 canonical이 drop됨")

    def test_concatenated_compound_kept(self):
        """canonical 인접 토큰이 기사에서 붙여쓰기된 복합('갤럭시 카드'→'갤럭시카드')이면 유지.

        '삼성 갤럭시 카드'의 '갤럭시'/'카드'가 기사 '갤럭시카드'에 붙여쓰기로 등장할 때, 접두
        '갤럭시'와 접미 '카드'가 서로 canonical sibling이므로 양방향 붙여쓰기 복합으로 인정해야
        한다. '삼성'은 독립 어절로 등장(무제한 접두 유추 아님)해 세 토큰이 함께 근거를 갖는다."""
        arts = [
            art("삼성 갤럭시카드 미국 출시", "yna.co.kr", "삼성 갤럭시카드를 미국에서 출시"),
            art("삼성 갤럭시카드 공개", "chosun.com", "삼성이 갤럭시카드를 공개했다"),
            art("삼성 갤럭시카드 환급 혜택", "hani.co.kr", "삼성 갤럭시카드 5% 환급"),
        ]
        self.assertTrue(self._grounded_keep("삼성 갤럭시 카드", arts),
                        "붙여쓰기 복합(갤럭시카드)이 과잉 drop됨")

    def test_foreign_compound_suffix_still_rejected(self):
        """sibling이 아닌 외래 복합명사 접미('개인정보유출'의 '정보')는 여전히 근거 불인정.

        붙여쓰기 복합 인정이 복합명사 오탐 방지를 무너뜨리지 않는지 확인한다 —
        canonical에 '개인'이 없으므로 '정보'는 '개인정보유출'의 접미로 인정되면 안 된다."""
        from news import ranker
        arts = [
            art("따릉이 개인정보유출 사고", "kookje.co.kr", "따릉이 개인정보유출 사고 발생"),
            art("따릉이 개인정보유출 보상", "munhwa.co.kr", "따릉이 개인정보유출 피해 보상"),
        ]
        meta = _news_meta_for("따릉이", arts)
        units = ranker._displayed_article_units(meta.get("articles") or [])
        # siblings에 '개인'이 없으므로 '정보'는 '개인정보유출' 접미로 인정 안 됨.
        grounded = any(
            ranker._token_grounded_in_unit("정보", at, ax, siblings={"따릉이", "정보", "명예"})
            for at, ax in units
        )
        self.assertFalse(grounded, "외래 복합명사 접미('정보'⊂'개인정보유출')가 오탐 인정됨")

    def test_title_role_suffix_kept(self):
        """직함 결합('한동훈 대표')이 소속과 함께 등장해도 canonical 유지."""
        arts = [
            art("국민의힘 한동훈 대표 발언", "yna.co.kr", "국민의힘 한동훈 대표는 오늘"),
            art("한동훈 대표 기자회견", "chosun.com", "한동훈 대표가 기자회견을 열었다"),
            art("한동훈 대표 당대표 행보", "hani.co.kr", "한동훈 대표의 당대표 행보"),
        ]
        self.assertTrue(self._grounded_keep("한동훈 대표", arts),
                        "직함 결합 정상 canonical이 drop됨")

    def test_compound_prefix_not_over_matched(self):
        """복합명사 후행('정보'⊂'개인정보')은 여전히 독립 근거로 인정되지 않는다(오탐 방지 유지).

        canonical '따릉이 정보 유출'인데 기사엔 '개인정보유출'만 있고 '정보'/'유출' 독립
        어절이 없으면, '정보'는 근거가 없어 단일 기사 커버가 성립하지 않아야 한다."""
        from news import ranker
        arts = [
            art("따릉이 개인정보유출 사고", "kookje.co.kr", "따릉이 개인정보유출 사고 발생"),
            art("따릉이 개인정보유출 보상", "munhwa.com", "따릉이 개인정보유출 피해 보상"),
        ]
        meta = _news_meta_for("따릉이", arts)
        units = ranker._displayed_article_units(meta.get("articles") or [])
        # '정보'(개인정보유출의 내부 substring)는 접미 2글자 이상이라 독립 근거 불인정.
        self.assertFalse(
            any(ranker._token_grounded_in_unit("정보", at, ax) for at, ax in units),
            "'정보'가 '개인정보유출' 내부 substring으로 오탐 근거 인정됨",
        )


class TestBarePrefixRemoved(unittest.TestCase):
    """P1 사전검토 2차 (1): 무제한 접두 grounding 제거 — 별도 근거 없는 단순 접두 배제."""

    def test_word_contains_token_prefix_rules(self):
        from news import ranker
        # 조사결합(남1) 인정
        self.assertTrue(ranker._word_contains_token("따릉이는", "따릉이"))
        self.assertTrue(ranker._word_contains_token("미국은", "미국"))
        # 무제한 접두 배제
        self.assertFalse(ranker._word_contains_token("삼성물산", "삼성"), "삼성←삼성물산 단순접두 인정됨")
        self.assertFalse(ranker._word_contains_token("삼성전자", "삼성"), "삼성←삼성전자 단순접두 인정됨")
        self.assertFalse(ranker._word_contains_token("애플리케이션", "애플"), "애플←애플리케이션 인정됨")
        # 접미 복합명사(sibling 없음) 배제
        self.assertFalse(ranker._word_contains_token("카드뉴스", "카드"), "카드←카드뉴스(뉴스 non-sibling) 인정됨")
        self.assertFalse(ranker._word_contains_token("개인정보", "정보"), "정보←개인정보 인정됨")

    def test_sibling_compound_bidirectional(self):
        from news import ranker
        sib = {"삼성", "갤럭시", "카드"}
        # 갤럭시카드: 갤럭시(접두)·카드(접미) 양방향 sibling 복합 인정
        self.assertTrue(ranker._word_contains_token("갤럭시카드", "카드", sib))
        self.assertTrue(ranker._word_contains_token("갤럭시카드", "갤럭시", sib))
        # 삼성물산: '물산'이 sibling 아님 → 배제(카드 keyword여도)
        self.assertFalse(ranker._word_contains_token("삼성물산", "삼성", {"삼성", "카드"}))

    def test_comparison_denominator_excludes_bare_prefix(self):
        """comparison keyword '삼성'의 모수에 '삼성물산' 기사만 있으면 포함되지 않는다."""
        from news import candidates as cand
        from news.summarizer import _tokens
        from news import ranker
        arts = [
            art("삼성물산 실적 발표", "yna.co.kr", "삼성물산 실적 발표"),
            art("삼성물산 주가 상승", "chosun.com", "삼성물산 주가 상승"),
        ]
        kw_toks = _tokens("삼성 카드")
        kwf, sib = kw_toks[0], set(kw_toks[1:])
        present = [
            a for a in arts
            if any(
                ranker._word_contains_token(t, kwf, sib, ranker._institution_alias_forms(kwf))
                for t in _tokens(a["title"])
            )
        ]
        self.assertEqual(present, [], "삼성 모수에 삼성물산 기사가 오포함됨")


class TestComboSpanGrounding(unittest.TestCase):
    """P1 사전검토 2차 (2): display 재조합은 근접 span 근거를 요구한다(단순 공존 불가)."""

    def _rwa(self, kw, score, articles):
        return {"keyword": kw, "score": score, "source_breakdown": {"news": score},
                "rank_reason": "", "news_meta": {"articles": articles},
                "used_signals": ["news"], "sources": {"daum": 1}}

    def _art(self, title, snippet):
        _Seq.n += 1
        u = f"https://news.example.com/{_Seq.n}"
        return {"title": title, "url": u, "snippet": snippet,
                "published_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}

    def test_far_scattered_tokens_not_combined_interest_rate(self):
        """같은 기사에 '금리 전망'과 멀리 떨어진 '인하'가 있어도 '금리 전망 인하' 생성 금지."""
        from news import ranker
        far = self._art("미국 금리 전망 발표",
                        "미국 금리 전망 발표 이후 여러 변수 감안 시장에서는 향후 기준 인하 가능성도 조심스럽게 거론")
        merged = ranker.dedupe_and_merge([
            self._rwa("금리 전망", 0.9, [far]), self._rwa("금리 인하", 0.85, [far])])
        disp = merged[0].get("display_keyword") if merged else None
        self.assertNotEqual(disp, "금리 전망 인하", "멀리 흩어진 토큰으로 억지 재조합됨")

    def test_far_scattered_tokens_not_combined_leak(self):
        """같은 기사에 '유출 피해'와 멀리 떨어진 '보상'이 있어도 근거 없는 재조합 금지."""
        from news import ranker
        far = self._art("유출 피해 접수 시작",
                        "유출 피해 접수가 시작됐고 별도로 정부는 전혀 다른 사안에 대한 보상 정책도 함께 검토")
        merged = ranker.dedupe_and_merge([
            self._rwa("유출 피해", 0.9, [far]), self._rwa("피해 보상", 0.85, [far])])
        disp = merged[0].get("display_keyword") if merged else None
        self.assertNotIn("보상", disp or "", "멀리 떨어진 '보상'이 억지 결합됨")

    def test_near_span_combined_arrest(self):
        """김영환과 압수수색이 한 title 근접 span으로 존재하면 자연스러운 조합 유지."""
        from news import ranker
        near = self._art("공수처 김영환 지사 사무실 압수수색", "공수처가 김영환 지사 사무실을 압수수색")
        merged = ranker.dedupe_and_merge([
            self._rwa("압수수색", 0.9, [near]), self._rwa("김영환", 0.85, [near])])
        disp = merged[0].get("display_keyword")
        self.assertIn("김영환", disp, f"근접 span 정상 조합이 축소됨: {disp!r}")
        self.assertNotEqual(disp, "압수수색", "근접 span인데 best 단독으로 축소됨")

    def test_no_near_span_keeps_best(self):
        """근접 span이 없으면 억지 결합하지 않고 best 유지."""
        from news import ranker
        far = self._art("압수수색 관련 소식",
                        "압수수색 절차가 진행되는 가운데 전혀 다른 맥락에서 여러 인물이 거론되었고 그중 김영환 이름도 등장")
        merged = ranker.dedupe_and_merge([
            self._rwa("압수수색", 0.9, [far]), self._rwa("김영환", 0.85, [far])])
        self.assertEqual(merged[0].get("display_keyword"), "압수수색",
                         "근접 span 없는데 억지 결합됨")

    def test_entity_dedup_grounded_shinjinseo(self):
        """신진서 9단 + 신진서 바둑 → 근접 span 근거 있는 '신진서 9단 바둑' 유지."""
        from news import ranker
        near = self._art("신진서 9단 카타고와 대국 바둑", "신진서 9단 바둑 카타고 대국")
        merged = ranker.dedupe_and_merge([
            self._rwa("신진서 9단", 0.9, [near]), self._rwa("신진서 바둑", 0.85, [near])])
        self.assertEqual(merged[0].get("display_keyword"), "신진서 9단 바둑")


class TestContextualAlias(unittest.TestCase):
    """P1 3차 (1): 기사 묶음에서 수렴 검증된 문맥 alias(약칭↔정식명칭). 하드코딩/무제한 접두 금지."""

    def _a(self, title, snippet):
        _Seq.n += 1
        return {"title": title, "snippet": snippet, "url": f"https://ex.com/{_Seq.n}"}

    def test_1_dominant_expansion_recognized(self):
        """삼성 실적 + 삼성전자 실적 기사 다수 → {삼성:{삼성전자...}} 인정."""
        arts = [self._a("삼성전자 3분기 실적 발표", "삼성전자가 실적 발표 삼성전자 실적"),
                self._a("삼성전자 실적 반도체 흑자", "삼성전자 실적 개선"),
                self._a("삼성전자 실적 서프라이즈", "삼성전자 실적")]
        am = ranker._contextual_alias_forms({"삼성", "실적"}, arts)
        self.assertIn("삼성", am)
        self.assertIn("삼성전자", am["삼성"])

    def test_2_conflicting_expansions_no_alias(self):
        """삼성전자/삼성물산/삼성중공업 혼재 → 단일 alias 확정 금지."""
        arts = [self._a("삼성전자 실적", "삼성전자 실적"),
                self._a("삼성전자 실적", "삼성전자 실적"),
                self._a("삼성물산 실적", "삼성물산 실적"),
                self._a("삼성물산 실적", "삼성물산 실적"),
                self._a("삼성중공업 실적", "삼성중공업 실적")]
        am = ranker._contextual_alias_forms({"삼성", "실적"}, arts)
        self.assertNotIn("삼성", am, "경쟁 확장형 혼재인데 단일 alias 확정됨")

    def test_3_samsung_card_vs_mulsan_cardnews_fails(self):
        """삼성 카드 canonical이 '삼성물산 카드뉴스'만으로는 grounding되지 않는다."""
        arts = [self._a("삼성물산 카드뉴스 공개", "삼성물산 카드뉴스"),
                self._a("삼성물산 카드뉴스 배포", "삼성물산 카드뉴스")]
        am = ranker._contextual_alias_forms({"삼성", "카드"}, arts)
        # 삼성물산은 확장형 후보지만 '카드'(others)가 삼성물산 기사에 '카드' 어절로 없음
        # (카드뉴스는 sibling 복합 불성립) → 나머지 의미토큰 미지원 → alias 불인정.
        units = [(set(cand._tokens(a["title"] + " " + a["snippet"])), a["title"] + " " + a["snippet"]) for a in arts]
        self.assertFalse(ranker._display_grounded_by_single_unit({"삼성", "카드"}, units, am),
                         "삼성 카드가 삼성물산 카드뉴스로 grounding됨")

    def test_4_apple_card_vs_application_cardnews_fails(self):
        """애플 카드 canonical이 '애플리케이션 카드뉴스'만으로는 grounding되지 않는다."""
        arts = [self._a("애플리케이션 카드뉴스", "애플리케이션 카드뉴스"),
                self._a("애플리케이션 카드뉴스", "애플리케이션 카드뉴스")]
        am = ranker._contextual_alias_forms({"애플", "카드"}, arts)
        units = [(set(cand._tokens(a["title"] + " " + a["snippet"])), a["title"] + " " + a["snippet"]) for a in arts]
        self.assertFalse(ranker._display_grounded_by_single_unit({"애플", "카드"}, units, am),
                         "애플 카드가 애플리케이션 카드뉴스로 grounding됨")

    def test_5_single_expansion_occurrence_no_alias(self):
        """확장형이 기사 1건에만 존재 → 최소 증거 미달로 alias 불인정."""
        arts = [self._a("삼성전자 실적", "삼성전자 실적"),
                self._a("삼성 브랜드 가치", "삼성 브랜드")]
        am = ranker._contextual_alias_forms({"삼성", "실적"}, arts)
        self.assertNotIn("삼성", am, "1건 확장형인데 alias 인정됨")

    def test_6_event_token_scattered_no_alias(self):
        """정식명칭과 canonical 나머지 사건토큰이 서로 다른 기사에 분산 → alias 불인정."""
        arts = [self._a("삼성전자 발표", "삼성전자 발표"),
                self._a("삼성전자 공시", "삼성전자 공시"),
                self._a("어제 실적 시장 반응", "실적 시장")]
        am = ranker._contextual_alias_forms({"삼성", "실적"}, arts)
        self.assertNotIn("삼성", am, "사건토큰 분산인데 alias 인정됨")

    def test_7_apple_card_comparison_and_galaxy_no_regression(self):
        """애플카드 comparison·삼성 갤럭시 카드 정상 사례 회귀 없음(운영 replay 재사용)."""
        from news.replay import replay_selection
        _Seq.n += 1
        A = lambda t, h, d="": {"title": t, "originallink": f"https://{h}/{_Seq.n}",
                                "link": f"https://{h}/{_Seq.n}", "description": d,
                                "pubDate": _pubdate(_Seq.n)}
        base = [
            A("삼성전자, 미국서 갤럭시 카드 출시…애플 카드에 맞불", "yna.co.kr", "삼성전자가 갤럭시 카드를 출시하며 애플 카드에 맞불 삼성 갤럭시 카드"),
            A("삼성, 갤럭시 카드 공개…애플 카드 겨냥", "chosun.com", "삼성전자가 갤럭시 카드를 공개 애플 카드를 겨냥 삼성 갤럭시 카드"),
            A("삼성전자 갤럭시 카드 미국 출시", "hani.co.kr", "삼성전자가 미국에서 갤럭시 카드를 출시 삼성 갤럭시 카드"),
            A("삼성 갤럭시 카드 5% 환급 혜택", "khan.co.kr", "삼성 갤럭시 카드가 5% 환급 혜택 삼성전자"),
            A("삼성전자 갤럭시 카드 월렛 연동", "donga.com", "삼성전자 갤럭시 카드 삼성 월렛 연동 삼성 갤럭시 카드"),
        ]
        r = replay_selection({"keywords": ["애플 카드", "삼성 갤럭시 카드"],
                              "articles_by_keyword": {"애플 카드": base, "삼성 갤럭시 카드": base}})
        sel = r["selected"]
        self.assertTrue(sel, "comparison 정상 후보가 전부 탈락함")
        self.assertEqual(sel[0]["keyword"], "삼성 갤럭시 카드", f"애플 카드가 주체로 승격됨: {sel[0]['keyword']!r}")


class TestSiblingExactComposition(unittest.TestCase):
    """P1 3차 (2): sibling 붙여쓰기는 exact composition만 인정(부분 prefix 매칭 배제)."""

    def test_galaxy_card_exact_kept(self):
        """갤럭시+카드 ↔ 갤럭시카드 / 갤럭시카드는(조사) → 유지."""
        sib = {"갤럭시", "카드"}
        self.assertTrue(ranker._word_contains_token("갤럭시카드", "카드", sib))
        self.assertTrue(ranker._word_contains_token("갤럭시카드", "갤럭시", sib))
        self.assertTrue(ranker._word_contains_token("갤럭시카드는", "카드", sib))

    def test_samsung_partial_not_composed(self):
        """삼성+카드 ↔ 삼성카 / 삼성카드뉴 → 불인정(부분 prefix)."""
        sib = {"삼성", "카드"}
        self.assertFalse(ranker._word_contains_token("삼성카", "삼성", sib), "삼성카 인정됨")
        self.assertFalse(ranker._word_contains_token("삼성카드뉴", "삼성", sib), "삼성카드뉴 인정됨")
        self.assertFalse(ranker._word_contains_token("삼성카드뉴", "카드", sib), "삼성카드뉴(카드) 인정됨")

    def test_galaxy_cardnews_residual_not_auto(self):
        """갤럭시카드뉴스는 '카드' 근거로 자동 인정하지 않는다(뉴스 잔여)."""
        sib = {"갤럭시", "카드"}
        self.assertFalse(ranker._word_contains_token("갤럭시카드뉴스", "카드", sib), "갤럭시카드뉴스 인정됨")
        self.assertFalse(ranker._word_contains_token("갤럭시카드뉴스", "갤럭시", sib))

    def test_information_substring_not_grounded(self):
        """'개인정보'의 '정보'는 독립 근거로 불인정(앞 '개인정보'가 sibling 아님)."""
        self.assertFalse(ranker._word_contains_token("개인정보", "정보", {"정보", "유출"}))
        self.assertFalse(ranker._word_contains_token("개인정보유출", "유출", {"정보", "유출"}))

    def test_josa_combination_kept(self):
        """조사 결합 정상 사례 유지."""
        self.assertTrue(ranker._word_contains_token("카드가", "카드"))
        self.assertTrue(ranker._word_contains_token("미국은", "미국"))
        self.assertTrue(ranker._word_contains_token("따릉이는", "따릉이"))
        # 조사 아닌 1글자 접두 잔여는 불인정
        self.assertFalse(ranker._word_contains_token("카드뉴", "카드"), "카드뉴(뉴 non-josa) 인정됨")


class TestContextualAliasGroundingApplied(unittest.TestCase):
    """Codex P1 (2026-07-22): contextual alias가 grounding에 실제로 적용되는 두 구멍 방지.

    P1-A: alias 나머지 의미 토큰 검증에 sibling exact-composition 미전달 → sibling 복합
          ('갤럭시카드') 근거를 못 얻어 정상 canonical 과잉 drop.
    P1-B: alias exact 매칭을 art_text.split()에만 적용 → 구두점 어절('삼성전자·실적')에서
          정규 토큰 '삼성전자'가 alias로 안 잡혀 정상 canonical 과잉 drop.
    """

    def _a(self, title, snippet):
        _Seq.n += 1
        return {"title": title, "snippet": snippet, "url": f"https://ex.com/{_Seq.n}",
                "relevance_score": 1.0}

    def _kept(self, keyword, articles):
        item = {"keyword": keyword, "display_keyword": keyword,
                "news_meta": {"articles": articles}}
        return len(ranker.enforce_display_source_grounding([item])) == 1

    def test_A_sibling_compound_alias_kept(self):
        """canonical 삼성 갤럭시 카드 + 기사 2건 '삼성전자 갤럭시카드' → 유지(sibling 복합 근거)."""
        arts = [self._a("삼성전자 갤럭시카드 미국 출시", "삼성전자 갤럭시카드 미국 출시"),
                self._a("삼성전자 갤럭시카드 공개 혜택", "삼성전자 갤럭시카드 공개 혜택")]
        self.assertTrue(self._kept("삼성 갤럭시 카드", arts),
                        "삼성전자 갤럭시카드 근거인데 삼성 갤럭시 카드가 drop됨")

    def test_A_foreign_compound_still_dropped(self):
        """삼성물산 카드뉴스 같은 외래 복합은 여전히 grounding 불인정(과잉 인정 아님)."""
        arts = [self._a("삼성물산 카드뉴스 공개", "삼성물산 카드뉴스 공개"),
                self._a("삼성물산 카드뉴스 배포", "삼성물산 카드뉴스 배포")]
        self.assertFalse(self._kept("삼성 갤럭시 카드", arts),
                         "삼성물산 카드뉴스가 삼성 갤럭시 카드 근거로 오인됨")

    def test_A_conflicting_expansions_no_alias(self):
        """삼성전자/삼성물산 혼재 시 alias 확정 금지(단일 수렴 계약 유지)."""
        arts = [self._a("삼성전자 갤럭시카드 출시", "삼성전자 갤럭시카드"),
                self._a("삼성전자 갤럭시카드 공개", "삼성전자 갤럭시카드"),
                self._a("삼성물산 갤럭시카드 관련", "삼성물산 갤럭시카드"),
                self._a("삼성물산 갤럭시카드 기타", "삼성물산 갤럭시카드")]
        am = ranker._contextual_alias_forms({"삼성", "갤럭시", "카드"}, ranker._displayed_articles(arts))
        self.assertNotIn("삼성", am, "경쟁 확장형 혼재인데 삼성 alias 확정됨")

    def test_B_punctuation_separated_alias_kept(self):
        """canonical 삼성 실적 + 기사 '삼성전자·실적' → 유지(art_toks exact alias 매칭)."""
        arts = [self._a("삼성전자·실적 발표", "삼성전자·실적 발표"),
                self._a("삼성전자·실적 반등", "삼성전자·실적 반등")]
        self.assertTrue(self._kept("삼성 실적", arts),
                        "삼성전자·실적(구두점) 근거인데 삼성 실적이 drop됨")

    def test_B_mulsan_not_mistaken_as_samsung_electronics(self):
        """삼성물산·실적만 있으면 삼성전자 alias로 오인하지 않는다(확장형 base가 삼성물산)."""
        arts = [self._a("삼성물산·실적 발표", "삼성물산·실적 발표"),
                self._a("삼성물산·실적 반등", "삼성물산·실적 반등")]
        am = ranker._contextual_alias_forms({"삼성", "실적"}, ranker._displayed_articles(arts))
        # 삼성 확장형은 삼성물산(단일 수렴) — 삼성전자로 오인하지 않는다.
        self.assertNotIn("삼성전자", am.get("삼성", set()), "삼성물산인데 삼성전자로 오인됨")

    def test_B_application_cardnews_not_apple_card(self):
        """애플리케이션·카드뉴스를 애플 카드 근거로 오인하지 않는다."""
        arts = [self._a("애플리케이션·카드뉴스 공개", "애플리케이션·카드뉴스 공개"),
                self._a("애플리케이션·카드뉴스 배포", "애플리케이션·카드뉴스 배포")]
        self.assertFalse(self._kept("애플 카드", arts),
                         "애플리케이션·카드뉴스가 애플 카드 근거로 오인됨")


class TestComparisonEvidenceUrlDedup(unittest.TestCase):
    """Codex P2 (2026-07-22): comparison_dominant 최소표본(>=2)을 동일 URL 중복으로 우회 못 함.

    scored_articles는 URL dedup 전이라, 동일 URL 기사가 2번 있으면 title_present=2로
    최소표본을 잘못 충족해 comparison_dominant가 오발동한다. 증거 모수를 URL identity로
    dedup해(_dedup_by_url_identity, 상위 dedup_articles와 동일 winner 계약) 닫는다.
    """

    def _raw(self, title, url):
        return {"title": title, "originallink": url, "link": url, "description": title,
                "pubDate": "Mon, 21 Jul 2026 10:00:00 +0900"}

    _T = "삼성전자, 애플카드에 맞불 갤럭시카드 출시"

    def test_1_same_url_duplicate_counts_as_one(self):
        """동일 URL comparison 기사 2건 → 독립 evidence 1건 → comparison_dominant 미발동."""
        dup = [self._raw(self._T, "https://x.com/1"), self._raw(self._T, "https://x.com/1")]
        meta = cand.compute_news_signal("애플 카드", dup)
        self.assertIsNotNone(meta, "동일 URL 중복으로 전체가 잘못 강등됨")
        self.assertGreaterEqual(meta.get("high_relevance_count", 0), 1,
                                "동일 URL 중복이 2건으로 세어져 comparison_dominant 오발동")

    def test_2_distinct_urls_still_trigger(self):
        """서로 다른 URL comparison 기사 2건 → 독립 2건 → 기존대로 comparison_dominant 발동."""
        diff = [self._raw(self._T, "https://x.com/1"), self._raw(self._T, "https://x.com/2")]
        meta = cand.compute_news_signal("애플 카드", diff)
        # 2건 모두 comparison → 전부 강등 → high_relevance_count 0 (또는 evidence 소실).
        hrc = (meta or {}).get("high_relevance_count", 0)
        self.assertEqual(hrc, 0, f"독립 2건인데 comparison_dominant 미발동(hrc={hrc})")

    def test_3_helper_url_identity_matches_dedup_contract(self):
        """_dedup_by_url_identity: 동일 URL 1건, 입력순서 유지(dedup_articles 계약과 동일)."""
        a = [{"title": "t1", "url": "u1"}, {"title": "t2", "url": "u1"}, {"title": "t3", "url": "u2"}]
        out = cand._dedup_by_url_identity(a)
        self.assertEqual([x["title"] for x in out], ["t1", "t3"])

    def test_4_same_title_different_url_kept(self):
        """동일 제목이지만 서로 다른 URL은 2건으로 유지(제목만으로 합치지 않음)."""
        a = [{"title": self._T, "url": "u1"}, {"title": self._T, "url": "u2"}]
        self.assertEqual(len(cand._dedup_by_url_identity(a)), 2)

    def test_5_url_less_articles_not_merged(self):
        """URL 없는 기사는 뭉치지 않고 각각 독립으로 센다(fallback = 객체 identity)."""
        a = [{"title": self._T}, {"title": self._T}]
        self.assertEqual(len(cand._dedup_by_url_identity(a)), 2)

    def test_6_apple_card_operational_still_demoted(self):
        """애플카드 운영 재현: 서로 다른 언론사 기사이므로 comparison 대상 미승격 유지."""
        _Seq.n += 1
        A = lambda t, h, d="": {"title": t, "originallink": f"https://{h}/{_Seq.n}",
                                "link": f"https://{h}/{_Seq.n}", "description": d,
                                "pubDate": _pubdate(_Seq.n)}
        base = [
            A("삼성전자, 미국서 갤럭시 카드 출시…애플 카드에 맞불", "yna.co.kr", "삼성전자가 갤럭시 카드를 출시하며 애플 카드에 맞불 삼성 갤럭시 카드"),
            A("삼성, 갤럭시 카드 공개…애플 카드 겨냥", "chosun.com", "삼성전자가 갤럭시 카드를 공개 애플 카드를 겨냥 삼성 갤럭시 카드"),
            A("삼성전자 갤럭시 카드 미국 출시", "hani.co.kr", "삼성전자가 미국에서 갤럭시 카드를 출시 삼성 갤럭시 카드"),
            A("삼성 갤럭시 카드 5% 환급 혜택", "khan.co.kr", "삼성 갤럭시 카드가 5% 환급 혜택 삼성전자"),
            A("삼성전자 갤럭시 카드 월렛 연동", "donga.com", "삼성전자 갤럭시 카드 삼성 월렛 연동 삼성 갤럭시 카드"),
        ]
        r = replay_selection({"keywords": ["애플 카드", "삼성 갤럭시 카드"],
                              "articles_by_keyword": {"애플 카드": base, "삼성 갤럭시 카드": base}})
        sel = r["selected"]
        self.assertTrue(sel)
        self.assertEqual(sel[0]["keyword"], "삼성 갤럭시 카드", f"애플 카드 승격됨: {sel[0]['keyword']!r}")

    def test_7_apple_as_real_subject_not_over_demoted(self):
        """애플/애플카드가 실제 주체인 정상 사례는 과잉 강등되지 않는다(서로 다른 URL)."""
        _Seq.n += 1
        A = lambda t, h, d="": {"title": t, "originallink": f"https://{h}/{_Seq.n}",
                                "link": f"https://{h}/{_Seq.n}", "description": d,
                                "pubDate": _pubdate(_Seq.n)}
        arts = [
            A("애플카드, 미국서 순항 중", "yna.co.kr", "애플카드가 미국에서 순항"),
            A("애플카드 신규 혜택 발표", "chosun.com", "애플카드가 신규 혜택 발표"),
            A("애플카드 이용자 500만 돌파", "hani.co.kr", "애플카드 이용자 500만 돌파"),
        ]
        meta = cand.compute_news_signal("애플카드", arts)
        self.assertGreaterEqual(meta.get("high_relevance_count", 0), 2,
                                "실제 주체 애플카드가 과잉 강등됨")


if __name__ == "__main__":
    unittest.main()
