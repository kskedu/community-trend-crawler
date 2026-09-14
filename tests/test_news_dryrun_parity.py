"""dry-run 이 production 과 **같은 선정 계약**을 쓰는지 고정한다.

배경(2026-09-14): `news/dryrun.py::run_ranking` 은 "production 과 동일 시퀀스" 라고
주석에 적어두고 실제로는 자체 시퀀스 사본을 들고 있었다. 그 사본이 조용히 drift 해서
- `enforce_display_source_grounding` 누락
- `exclude_no_representative` 누락
- `exclude_insufficient_display_articles` 가 `select_top` **뒤**에서 실행(운영은 앞)
  → 제외된 자리를 하위 정상후보가 채우지 못하고 그대로 비었다
상태였다. 저장소에 들어 있는 fixture 로 돌리면 production 이 전부 탈락시키는 후보
2건(summary 가 빈 no_representative)을 dry-run 이 Top 으로 내보냈다.

이 파일은 "gate 를 몇 개 부르는가" 같은 구현 세부가 아니라 **결과가 production 과
같은가**를 본다. 누군가 dry-run 에 시퀀스를 다시 적으면(= 또 drift 하면) 여기서
깨진다.
"""
import copy
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news import dryrun
from news import ranker
from news.builder import build_ranked_issues


# ── fixture helper ──────────────────────────────────────────────────────────
def _a(title, url, snippet="", **kw):
    """이미 relevance/primary 가 부여된 기사(= build_news_signals 통과 후 형태)."""
    d = {"title": title, "url": url, "originallink": url, "snippet": snippet,
         "published_at": None, "thumbnail": None, "relevance_score": 0.9,
         "relevance_reason": "keyword_main_topic", "is_incidental": False,
         "is_primary_cluster": True}
    d.update(kw)
    return d


def _news(arts, recent=3, age=1.0, diversity=3, relevance=0.9):
    """quality gate 를 통과하는 news_meta. 신호를 전부 동일하게 줘서 순서를 고정한다."""
    return {
        "recent_count": recent, "latest_age_hours": age, "domain_diversity": diversity,
        "title_relevance": relevance, "articles": arts,
        "representative_article": arts[0] if arts else None,
        "high_relevance_count": 2, "quality_cluster_size": 2,
        "fresh_high_relevance_count": 1, "fresh_quality_cluster_size": 1,
        "latest_relevant_age_hours": 1.0, "keyword_kind": "unknown",
        "has_dominant_event": True, "same_event_burst": True,
    }


# 서로 다른 사건 12건 — 어휘가 겹치지 않아 merge 되지 않는다(정상 후보 pool).
_HEALTHY = [
    ("금리 인상", ["금리 인상 단행 발표 충격", "금리 인상 단행 발표 반응"]),
    ("반도체 수출", ["반도체 수출 증가 발표 호조", "반도체 수출 증가 발표 지속"]),
    ("전기차 보조금", ["전기차 보조금 축소 확정 공지", "전기차 보조금 축소 확정 반발"]),
    ("태풍 북상", ["태풍 북상 경로 변경 예보", "태풍 북상 경로 변경 대비"]),
    ("의대 정원", ["의대 정원 확대 합의 도출", "의대 정원 확대 합의 후속"]),
    ("가상자산 과세", ["가상자산 과세 유예 결정 발표", "가상자산 과세 유예 결정 배경"]),
    ("항공권 할인", ["항공권 할인 특가 개시 안내", "항공권 할인 특가 개시 마감"]),
    ("독감 유행", ["독감 유행 주의보 발령 확산", "독감 유행 주의보 발령 대응"]),
    ("지하철 파업", ["지하철 파업 협상 타결 소식", "지하철 파업 협상 타결 여파"]),
    ("수능 난이도", ["수능 난이도 조정 방침 공개", "수능 난이도 조정 방침 분석"]),
    ("환율 방어", ["환율 방어 개입 시사 발언", "환율 방어 개입 시사 파장"]),
    ("청약 제도", ["청약 제도 개편 시행 예고", "청약 제도 개편 시행 세부"]),
]

# 각 제외 게이트를 정확히 하나씩 때리는 후보.
# '정보' 가 기사에는 '개인정보' 복합어 안에만 있다 — 앞 단계인
# enforce_display_article_consistency 는 substring 매칭이라 통과시키고,
# 형태소 경계를 보는 enforce_display_source_grounding 만 이 후보를 떨어뜨린다.
# (두 단계가 동시에 떨어뜨리는 fixture 를 쓰면 grounding 누락을 관측할 수 없다.)
GROUNDING_DROP_KW = "따릉이 정보"
NO_REP_KW = "민경욱"                        # 기사 2건에 공통 하위주제가 없다
DISPLAY_SHORT_KW = "단일기사"               # 표시 기사 1건


def _healthy(limit=None):
    news, cands = {}, []
    for i, (kw, titles) in enumerate(_HEALTHY[:limit]):
        news[kw] = _news([_a(t, f"https://h{i}{j}.co.kr/n", t)
                          for j, t in enumerate(titles)])
        cands.append({"keyword": kw, "sources": {"daum_home": i + 1}})
    return news, cands


def _scenario(with_bad=True, healthy_limit=None):
    """(candidates, signals) 한 벌. 신호가 전부 같아 merged 순서 = 입력 순서다."""
    news, cands = _healthy(healthy_limit)
    if with_bad:
        news[NO_REP_KW] = _news([
            _a("속보...", "https://r1.co.kr/n",
               "민경욱 전 의원이 자택에서 의식 불명 상태로 발견돼 병원으로 이송됐다."),
            _a("현장 화보", "https://r2.co.kr/n",
               "민경욱 전 의원이 자택에서 의식 불명 상태로 발견돼 병원으로 이송됐다."),
        ])
        news[DISPLAY_SHORT_KW] = _news(
            [_a("단일기사 후속 상황 발표", "https://s1.co.kr/n", "단일기사 후속")])
        # 기사 3건이 같은 사건을 반복해 대표기사·표시 기사 3건이 모두 성립한다 —
        # grounding 을 빼면 뒤의 display/no_rep 게이트가 대신 잡아주지 않으므로
        # "grounding 누락"이 최종 결과에 그대로 드러난다.
        news[GROUNDING_DROP_KW] = _news([
            _a("따릉이 개인정보 유출 피해자 보상 확정", "https://g1.co.kr/n"),
            _a("따릉이 개인정보 유출 피해자 보상 시행", "https://g2.co.kr/n"),
            _a("따릉이 개인정보 유출 피해자 보상 안내", "https://g3.co.kr/n"),
        ])
        # 세 후보를 앞에 넣어 merged 상위(= select_top 대상)에 놓는다. 제외가
        # select_top **이전**에 일어나야 하위 정상후보가 그 자리를 채운다.
        for kw in (GROUNDING_DROP_KW, DISPLAY_SHORT_KW, NO_REP_KW):
            cands.insert(0, {"keyword": kw, "sources": {"daum_home": 0}})
    return cands, {"news": news, "datalab": {}, "google": {}}


def _production(candidates, signals):
    """운영과 같은 단일 진실원으로 얻은 정답(stages + issues).

    data_sources 는 선정과 무관한 dry-run 표시 항목이라 dry-run 헬퍼를 그대로 쓴다.
    """
    stages = ranker.run_selection_stages(
        copy.deepcopy(candidates), copy.deepcopy(signals))
    cmap = {c["keyword"]: c for c in candidates}
    issues = build_ranked_issues(
        stages["top"], cmap, dryrun._data_sources(candidates, {}))
    return stages, issues


def _dryrun(candidates, signals):
    """같은 입력을 dry-run 에 흘려 넣는다(입력 adapter 만 대체 — 시퀀스는 건드리지 않음)."""
    def _inputs():
        return {
            "candidates": copy.deepcopy(candidates),
            "signals": copy.deepcopy(signals),
            "datalab_signals": {},
            "daum_ranked": [{"keyword": c["keyword"], "rank": i + 1}
                            for i, c in enumerate(candidates)],
        }
    with mock.patch.object(dryrun, "ranking_inputs", _inputs):
        return dryrun.run_ranking(verbose=False)


def _payload(issues):
    """비교 대상 payload — 실행 시각만 제거한다."""
    out = copy.deepcopy(issues)
    out.pop("generated_at", None)
    return out


class TestDryrunProductionParity(unittest.TestCase):
    """dry-run 결과 == production selection 결과(exact)."""

    def test_full_payload_matches_production_exactly(self):
        """selected / order / score / representative / articles / display_articles 전부 동일."""
        cands, signals = _scenario()
        _stages, prod = _production(cands, signals)
        dry = _dryrun(cands, signals)
        self.assertEqual(_payload(dry), _payload(prod))

    def test_selected_keywords_and_order_match(self):
        cands, signals = _scenario()
        stages, _prod = _production(cands, signals)
        dry = _dryrun(cands, signals)
        self.assertEqual([k["keyword"] for k in dry["keywords"]],
                         [t["keyword"] for t in stages["top"]])
        self.assertEqual([k["rank"] for k in dry["keywords"]],
                         list(range(1, len(stages["top"]) + 1)))

    # --- 게이트별 drift 재현 ------------------------------------------------

    def test_source_grounding_dropped_candidate_is_absent(self):
        """grounding 탈락 후보는 dry-run 에도 없어야 한다(누락 전에는 그대로 노출됐다)."""
        cands, signals = _scenario()
        stages, _prod = _production(cands, signals)
        self.assertIn(GROUNDING_DROP_KW, stages["gate_passed"],
                      "fixture 전제: quality gate 는 통과해야 grounding 단계에 닿는다")
        # 앞 단계(article consistency)가 아니라 **grounding 이** 떨어뜨리는지 확인한다.
        # 그렇지 않으면 grounding 이 빠져 있어도 이 테스트가 통과해 버린다.
        item = {"keyword": GROUNDING_DROP_KW, "display_keyword": GROUNDING_DROP_KW,
                "news_meta": signals["news"][GROUNDING_DROP_KW]}
        self.assertTrue(ranker.enforce_display_article_consistency([dict(item)]),
                        "fixture 전제: consistency 단계는 이 후보를 살려 둬야 한다")
        self.assertFalse(ranker.enforce_display_source_grounding([dict(item)]),
                         "fixture 전제: grounding 단계가 이 후보를 떨어뜨려야 한다")
        # 뒤 게이트(display 부족 / no_representative)가 대신 잡아주면 grounding 누락을
        # 관측할 수 없다 — 이 후보는 두 게이트를 모두 통과해야 한다.
        self.assertEqual(ranker.exclude_insufficient_display_articles([dict(item)])[1], [])
        self.assertEqual(ranker.exclude_no_representative([dict(item)])[1], [])
        self.assertNotIn(GROUNDING_DROP_KW, [m["keyword"] for m in stages["merged"]])
        dry = _dryrun(cands, signals)
        self.assertNotIn(GROUNDING_DROP_KW, [k["keyword"] for k in dry["keywords"]])

    def test_no_representative_candidate_is_absent(self):
        """대표 사건을 못 만드는 후보는 dry-run 에도 없어야 한다."""
        cands, signals = _scenario()
        stages, _prod = _production(cands, signals)
        self.assertEqual(stages["no_rep_excluded"], [NO_REP_KW])
        dry = _dryrun(cands, signals)
        self.assertNotIn(NO_REP_KW, [k["keyword"] for k in dry["keywords"]])
        for k in dry["keywords"]:
            self.assertNotEqual(
                k.get("summary_type"), "no_representative",
                "summary 가 빈 후보가 Top 에 남으면 안 된다")

    def test_display_short_candidate_excluded_before_select_top(self):
        """display 부족 제외는 select_top **이전**이라 하위 정상후보가 자리를 채운다."""
        cands, signals = _scenario()
        stages, _prod = _production(cands, signals)
        self.assertEqual(stages["display_excluded"], [DISPLAY_SHORT_KW])
        dry = _dryrun(cands, signals)
        kws = [k["keyword"] for k in dry["keywords"]]
        self.assertNotIn(DISPLAY_SHORT_KW, kws)
        # 정상 후보 12건이 있으므로 제외 3건이 있어도 Top10 이 가득 찬다.
        # select_top 뒤에서 제외하면 여기서 10 미만이 된다(옛 dry-run 의 증상).
        self.assertEqual(len(kws), ranker.TOP_N)

    def test_next_safe_candidate_is_promoted_like_production(self):
        """앞 후보 3건이 탈락한 뒤 승격되는 후보가 production 과 같다."""
        cands, signals = _scenario()
        stages, _prod = _production(cands, signals)
        dry = _dryrun(cands, signals)
        promoted = [k["keyword"] for k in dry["keywords"]][-1]
        self.assertEqual(promoted, stages["top"][-1]["keyword"])
        # 탈락 3건이 없었다면 들어오지 못했을 후보가 실제로 승격됐다.
        self.assertIn(promoted, [kw for kw, _ in _HEALTHY])

    def test_underfill_is_identical_when_safe_candidates_run_out(self):
        """안전 후보가 모자라면 production 과 똑같이 underfill 한다(임의 보충 없음)."""
        cands, signals = _scenario(healthy_limit=4)
        stages, prod = _production(cands, signals)
        dry = _dryrun(cands, signals)
        self.assertLess(len(stages["top"]), ranker.TOP_N,
                        "fixture 전제: 안전 후보가 Top_N 보다 적어야 한다")
        self.assertEqual(_payload(dry), _payload(prod))
        self.assertEqual(len(dry["keywords"]), len(stages["top"]))

    def test_healthy_only_top10_matches_production(self):
        """제외 후보가 하나도 없는 정상 pool 에서도 결과가 같다(회귀 없음)."""
        cands, signals = _scenario(with_bad=False)
        stages, prod = _production(cands, signals)
        self.assertEqual(
            (stages["pr_excluded"], stages["generic_excluded"],
             stages["display_excluded"], stages["no_rep_excluded"]), ([], [], [], []))
        self.assertEqual(len(stages["top"]), ranker.TOP_N)
        self.assertEqual(_payload(_dryrun(cands, signals)), _payload(prod))


class TestDryrunInputAdapter(unittest.TestCase):
    """dry-run 고유 입력/표시 adapter 계약(선정과 무관하지만 payload 를 바꾼다)."""

    def test_ranking_inputs_carries_datalab_signal(self):
        """datalab 신호를 버리면 search_demand 축이 통째로 죽는다."""
        inputs = dryrun.ranking_inputs()
        self.assertTrue(inputs["datalab_signals"],
                        "fixture 전제: datalab fixture 에 값이 있어야 한다")
        self.assertEqual(inputs["signals"]["datalab"], inputs["datalab_signals"])

    def test_data_sources_lists_datalab_and_participating_families(self):
        """표시용 data_sources 는 naver_news + datalab + 참여 seed family."""
        cands = [{"keyword": "가", "sources": {"daum_home": 1}},
                 {"keyword": "나", "sources": {"nate_home": 1}}]
        self.assertEqual(dryrun._data_sources(cands, {"가": {"score": 1}}),
                         ["naver_news", "datalab", "daum_home", "nate_home"])
        self.assertEqual(dryrun._data_sources(cands, {}),
                         ["naver_news", "daum_home", "nate_home"])


class TestDryrunUsesSelectionSingleSource(unittest.TestCase):
    """dry-run 결과가 run_selection_stages **에서 나온다**는 것 자체를 고정한다.

    결과 비교만으로는 "우연히 같은 시퀀스를 다시 적은" 사본을 잡지 못한다. 단일
    진실원을 통째로 갈아끼웠을 때 dry-run 출력이 따라 바뀌는지로 출처를 증명한다.
    """

    def test_dryrun_top_comes_from_run_selection_stages(self):
        cands, signals = _scenario(with_bad=False)
        sentinel_kw = "센티넬 후보"
        sentinel_top = [{
            "keyword": sentinel_kw, "score": 0.5, "display_keyword": sentinel_kw,
            "news_meta": _news([_a("센티넬 후보 단독 보도", "https://z.co.kr/n",
                                   "센티넬 후보 단독 보도")]),
            "source_breakdown": {}, "rank_reason": "sentinel",
        }]

        def _fake_stages(candidates, sig, observe=None):
            return {"gate_passed": [], "ranked": [], "pr_excluded": [],
                    "merged": [], "generic_excluded": [], "display_excluded": [],
                    "no_rep_excluded": [], "kept": sentinel_top, "top": sentinel_top}

        with mock.patch.object(ranker, "run_selection_stages", _fake_stages):
            dry = _dryrun(cands, signals)
        self.assertEqual([k["keyword"] for k in dry["keywords"]], [sentinel_kw])


# 저장소 fixture 가 보장해야 하는 구성 — 한쪽만 있으면 dry-run 으로 "무엇이 선정되고
# 무엇이 왜 제외됐는지"를 함께 볼 수 없다. 개수(>=1)가 아니라 **어떤 keyword 인지**를
# 고정한다(개수만 보면 fixture 가 엉뚱하게 바뀌어도 통과한다).
FIXTURE_SELECTED = ["강변터널 화재", "시내버스 노선 개편"]
FIXTURE_GATE_DROPPED = {           # quality gate 에서 탈락(= 사유 문자열까지 고정)
    "노트북 추천": "low_quality_news",
    "에어컨 세일": "low_quality_news",
}
FIXTURE_NO_SIGNAL = ["신작 게임 출시"]   # 기사 0건 → news 신호 자체가 없다
FIXTURE_NO_REP_DROPPED = ["환율 급등", "월드컵 예선"]   # gate 는 통과, 대표 사건 없음


def _normalized(issues):
    """두 번 실행해도 같아야 하는 부분만 남긴다(실행 시각 파생 필드 제거)."""
    out = copy.deepcopy(issues)
    out.pop("generated_at", None)
    for k in out.get("keywords", []):
        for a in (k.get("articles") or []) + (k.get("display_articles") or []):
            a.pop("published_at", None)
        if k.get("representative_article"):
            k["representative_article"].pop("published_at", None)
    return out


class TestShippedFixtureCoverage(unittest.TestCase):
    """저장소 fixture 가 selected 와 단계별 제외를 **둘 다** 보여준다.

    2026-09-14 이전에는 fixture 후보가 production 기준 전부 탈락해 dry-run 결과가
    0건이었다. 로직 문제가 아니라 fixture 품질 문제였고, 빈 결과는 "도구가 고장났다"로
    오해돼 게이트를 임의로 줄이는 drift 를 유발하기 쉽다.
    """

    def test_selected_keywords_and_order_are_pinned(self):
        dry = dryrun.run_ranking(verbose=False)
        self.assertEqual([k["keyword"] for k in dry["keywords"]], FIXTURE_SELECTED)
        self.assertEqual([k["rank"] for k in dry["keywords"]],
                         list(range(1, len(FIXTURE_SELECTED) + 1)))

    def test_selected_entries_satisfy_display_contract(self):
        """선정된 항목은 대표 사건과 표시 기사 하한을 실제로 만족한다."""
        for k in dryrun.run_ranking(verbose=False)["keywords"]:
            with self.subTest(keyword=k["keyword"]):
                self.assertNotEqual(k["summary_type"], "no_representative")
                self.assertTrue(k["summary"])
                self.assertTrue(k["representative_title"])
                self.assertGreaterEqual(len(k["display_articles"]),
                                        ranker.DISPLAY_ARTICLES_MIN)
                self.assertTrue(k["signals"]["news"])

    def test_payload_matches_production_on_shipped_fixture(self):
        """selected / 순서 / score / representative / display articles 전부 동일."""
        inputs = dryrun.ranking_inputs()
        stages = ranker.run_selection_stages(
            copy.deepcopy(inputs["candidates"]), copy.deepcopy(inputs["signals"]))
        cmap = {c["keyword"]: c for c in inputs["candidates"]}
        prod = build_ranked_issues(
            stages["top"], cmap,
            dryrun._data_sources(inputs["candidates"], inputs["datalab_signals"]))
        self.assertEqual([t["keyword"] for t in stages["top"]], FIXTURE_SELECTED)
        self.assertEqual(_normalized(dryrun.run_ranking(verbose=False)), _normalized(prod))

    def test_intended_drops_keep_their_stage_and_reason(self):
        """의도된 탈락 사례가 **원래 단계·원래 사유** 그대로 남아 있다."""
        inputs = dryrun.ranking_inputs()
        news = inputs["signals"]["news"]
        stages = ranker.run_selection_stages(
            copy.deepcopy(inputs["candidates"]), copy.deepcopy(inputs["signals"]))
        candidate_kws = {c["keyword"] for c in inputs["candidates"]}

        for kw, reason in FIXTURE_GATE_DROPPED.items():
            with self.subTest(keyword=kw):
                self.assertIn(kw, candidate_kws)
                self.assertEqual(ranker._quality_gate_reason(kw, news[kw]), reason)
                self.assertNotIn(kw, stages["gate_passed"])

        for kw in FIXTURE_NO_SIGNAL:
            with self.subTest(keyword=kw):
                self.assertIn(kw, candidate_kws)
                self.assertNotIn(kw, news, "기사 0건이면 news 신호 자체가 없어야 한다")
                self.assertNotIn(kw, stages["gate_passed"])

        for kw in FIXTURE_NO_REP_DROPPED:
            with self.subTest(keyword=kw):
                self.assertIn(kw, stages["gate_passed"],
                              "quality gate 는 통과해야 no_representative 단계에 닿는다")
                self.assertIn(kw, stages["no_rep_excluded"])

    def test_fixture_shows_both_sides(self):
        """선정과 제외가 동시에 관측된다 — 한쪽만 있으면 dry-run 의 의미가 반감된다."""
        inputs = dryrun.ranking_inputs()
        stages = ranker.run_selection_stages(
            copy.deepcopy(inputs["candidates"]), copy.deepcopy(inputs["signals"]))
        self.assertTrue(stages["top"], "정상 selected 사례가 있어야 한다")
        self.assertTrue(stages["no_rep_excluded"], "정상 탈락 사례가 있어야 한다")
        self.assertLess(len(stages["gate_passed"]), len(inputs["candidates"]),
                        "quality gate 탈락 사례도 있어야 한다")

    def test_ranked_order_differs_from_seed_order(self):
        """뉴스 근거 기반 순위가 포털 노출 순서를 그대로 따르지 않는다(이 도구의 원래 합격 기준)."""
        inputs = dryrun.ranking_inputs()
        seed_order = [i["keyword"] for i in inputs["daum_ranked"]]
        ranked_order = [k["keyword"] for k in dryrun.run_ranking(verbose=False)["keywords"]]
        self.assertTrue(ranked_order)
        self.assertNotEqual(seed_order[:len(ranked_order)], ranked_order)

    def test_ranking_inputs_contains_no_selection_gate(self):
        """입력 adapter 는 후보/신호만 만든다 — 선정 결과를 담지 않는다."""
        inputs = dryrun.ranking_inputs()
        self.assertEqual(set(inputs),
                         {"candidates", "signals", "datalab_signals", "daum_ranked"})
        self.assertEqual(set(inputs["signals"]), {"news", "datalab", "google"})


if __name__ == "__main__":
    unittest.main()
