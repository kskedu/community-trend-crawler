"""뉴스 키워드 진단 수집/적재 테스트 (unittest, 외부호출/DB write 없음).

검증 계약(사용자 확정 2026-07-16):
- 랭킹 불변: 진단 ON/OFF에서 Top/순위/필드가 완전히 동일
- snapshot 소유권: 호출자 생성·명시 전달, 폐기 pass 혼입 0
- degraded: 채택 pass degraded면 RPC 0회 / 폐기 pass degraded는 정상 저장을 막지 않음
- 예외 격리: _safe_diag thunk 규약, 인자 계산 예외까지 격리
- 로그 위생: 예외 메시지/secret/기사 본문 미저장
- 카운트 invariant: candidate_count = decisions = selected + not_selected + rule_excluded
- skip: 랭킹 전 0 / 후랭킹 skip은 snapshot 보존
"""
import json
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news import diagnostics
from news import ranker
import main as main_module


class _Suffixes(dict):
    """키워드별 고유 제목 어휘. 등록되지 않은 키워드는 키워드 자체로 파생시킨다."""

    def __missing__(self, key):
        return [f"{key}발 속보", f"{key} 후속 보도"]


# 키워드 간 토큰이 겹치지 않아야 same_article_cluster로 merge되지 않는다.
# 또한 각 키워드의 2개 suffix는 공통 사건 토큰 2개+를 공유해야 대표기사가 생성된다
# (summarizer._SUBTOPIC_MIN_TOKENS=2). 그래야 B2(no_representative gate, 2026-07)를 통과한다.
# 키워드 자체(및 파생)는 subtopic에서 제외되므로 suffix의 공통어로 사건을 표현한다.
_TITLE_SUFFIXES = _Suffixes({
    "민경욱": ["측근 발언 파문 확산", "측근 발언 파문 반응"],
    "코스피": ["장중 상승 마감 랠리", "장중 상승 마감 지속"],
    "환율": ["당국 개입 관측 확대", "당국 개입 관측 여파"],
    "폭염": ["온열질환 주의보 발령", "온열질환 주의보 확대"],
    "지진": ["여진 대피 이어져", "여진 대피 계속"],
    "태풍": ["북상 경로 변경 예고", "북상 경로 변경 확대"],
    "백신접종": ["예약 접종 시작 안내", "예약 접종 시작 혼선"],
    "국제유가": ["배럴당 급등 감산 여파", "배럴당 급등 감산 지속"],
})


def _news_meta(keyword="키워드", recent=3, age=1.0, diversity=3, relevance=0.9,
               high_rel=3, cluster=3, fresh_high_rel=2, fresh_cluster=2,
               latest_relevant_age=1.0):
    """기존 tests/test_news_ranking.py::_news와 동일 규약 + 키워드별 고유 기사.

    두 가지 함정을 피한다:
    - fresh_* / latest_relevant_age_hours를 빠뜨리면 fresh relevance gate가 전 후보를 drop
    - 모든 키워드가 같은 기사 URL/제목을 쓰면 dedupe_and_merge가 same_article_cluster로
      전부 1건에 흡수한다(6→1). URL뿐 아니라 **제목 토큰도 겹치면 안 된다** —
      _article_overlap은 URL 불일치 시 title/snippet token Jaccard로 판정하므로
      "관련 N번 단독 기사" 같은 공통 상용구를 쓰면 전부 merge된다. 키워드별 고유 어휘를 준다.
    두 경우 모두 top이 비어 실행이 NO_RANKING_RESULT로 조기 종료되고, 테스트는 의도한
    계약이 아니라 엉뚱한 이유로 통과/실패한다.
    """
    articles = [
        {
            "title": f"{keyword} {suffix}",
            "url": f"https://news.example.com/{keyword}/{i}",
            "source": f"press{i}",
            "press": f"press{i}",
            "published_at": "2026-07-16T00:00:00+00:00",
            "relevance_score": 0.9,
            "is_incidental": False,
            "is_primary_cluster": True,
            "description": "본문 요약 — 저장되면 안 되는 필드",
        }
        for i, suffix in enumerate(_TITLE_SUFFIXES[keyword])
    ]
    return {
        "recent_count": recent,
        "latest_age_hours": age,
        "domain_diversity": diversity,
        "title_relevance": relevance,
        "high_relevance_count": high_rel,
        "quality_cluster_size": cluster,
        "fresh_high_relevance_count": fresh_high_rel,
        "fresh_quality_cluster_size": fresh_cluster,
        "latest_relevant_age_hours": latest_relevant_age,
        # entity-role 정제(2026-07) 신규 필드: 이 fixture는 다양한 사건·현상 키워드라
        # unknown/정상 이슈로 취급 — cohesion gate 미적용, 대표 생성 가능.
        "keyword_kind": "unknown",
        "has_dominant_event": True,
        "same_event_burst": True,
        "representative_article": articles[0] if articles else None,
        # 제목은 keyword를 포함해야 display anchor를 통과하고(build_display_articles),
        # 동시에 키워드 간 공통 상용구가 없어야 same_article_cluster merge를 피한다.
        # is_primary_cluster는 운영에서 compute_news_signal이 미리 표시한다.
        "articles": articles,
    }


def _candidates(*keywords):
    return [{"keyword": k, "sources": {"daum_home": i + 1}} for i, k in enumerate(keywords)]


def _signals(*keywords):
    return {
        "news": {k: _news_meta(keyword=k) for k in keywords},
        "datalab": {},
        "google": {},
    }


class SafeDiagBoundaryTest(unittest.TestCase):
    """§3-0 — thunk 규약으로 인자 계산 예외까지 격리한다."""

    def test_thunk_exception_marks_degraded_not_raised(self):
        snap = diagnostics.PassSnapshot("pass1")

        def boom():
            raise ValueError("terrible")

        main_module._safe_diag(snap, boom)   # 예외가 새면 이 줄에서 테스트가 실패한다
        self.assertTrue(snap.degraded)
        self.assertEqual(snap.errors, ["ValueError"])

    def test_argument_computation_exception_is_isolated(self):
        """인자 계산이 thunk 안에서 일어나므로 격리된다(Codex plan P1-1의 핵심)."""
        snap = diagnostics.PassSnapshot("pass1")
        bad = {}   # KeyError를 낼 dict

        main_module._safe_diag(snap, lambda: snap.record(bad["missing"], "selected", "SELECTED"))
        self.assertTrue(snap.degraded)
        self.assertEqual(snap.errors, ["KeyError"])

    def test_none_target_is_noop(self):
        main_module._safe_diag(None, lambda: (_ for _ in ()).throw(RuntimeError("x")))

    def test_mark_degraded_failure_does_not_raise(self):
        class Hostile:
            def mark_degraded(self, exc):
                raise RuntimeError("mark_degraded도 터진다")

        main_module._safe_diag(Hostile(), lambda: (_ for _ in ()).throw(ValueError("x")))


class SnapshotContractTest(unittest.TestCase):
    """PassSnapshot 기록/집계/seal 계약."""

    def test_duplicate_decision_raises(self):
        snap = diagnostics.PassSnapshot("pass1")
        snap.record("민경욱", diagnostics.STATUS_SELECTED, "SELECTED")
        with self.assertRaises(RuntimeError):
            snap.record("민경욱", diagnostics.STATUS_SELECTED, "SELECTED")

    def test_norm_key_absorbs_notation_variants(self):
        """표기 변형은 같은 후보로 본다(candidates._merge pool 키와 동일 규약)."""
        self.assertEqual(diagnostics._norm_key("민경욱 "), diagnostics._norm_key("민경욱"))
        self.assertNotEqual(diagnostics._norm_key("민 경 욱"), diagnostics._norm_key("민경욱"))

    def test_close_seals_but_preserves_decisions(self):
        """close는 폐기가 아니라 seal — 수집분은 보존한다(Codex 4차)."""
        snap = diagnostics.PassSnapshot("pass2")
        snap.record("A", diagnostics.STATUS_SELECTED, "SELECTED")
        snap.close()
        self.assertEqual(len(snap.payload_decisions()), 1)
        with self.assertRaises(RuntimeError):
            snap.record("B", diagnostics.STATUS_SELECTED, "SELECTED")

    def test_article_body_is_not_stored(self):
        """기사 메타 allowlist — 본문/description 저장 금지."""
        snap = diagnostics.PassSnapshot("pass1")
        snap.record(
            "A", diagnostics.STATUS_SELECTED, "SELECTED",
            articles=[{
                "title": "제목", "url": "u", "source": "s",
                "description": "본문이 들어가면 안 된다",
                "content": "전체 본문",
            }],
        )
        stored = json.dumps(snap.payload_decisions(), ensure_ascii=False)
        self.assertNotIn("본문이 들어가면 안 된다", stored)
        self.assertNotIn("전체 본문", stored)
        self.assertIn("제목", stored)

    def test_press_mapped_to_source_when_source_absent(self):
        """article dict가 실제로 갖는 필드는 press뿐(source는 오늘 코드가 만들지 않음).
        진단 저장 컬럼명은 source로 고정돼 있어 여기서만 press->source로 투영한다."""
        snap = diagnostics.PassSnapshot("pass1")
        snap.record(
            "A", diagnostics.STATUS_SELECTED, "SELECTED",
            articles=[{"title": "제목", "url": "u", "press": "조선일보"}],
        )
        stored = snap.payload_decisions()[0]["articles"][0]
        self.assertEqual(stored["source"], "조선일보")
        self.assertNotIn("press", stored)  # allowlist 밖 원본 키는 담기지 않는다

    def test_explicit_source_field_wins_over_press(self):
        """방어적 우선순위: article에 실제 source 값이 있으면(향후 upstream 확장 대비)
        그것을 쓰고, press로 덮어쓰지 않는다."""
        snap = diagnostics.PassSnapshot("pass1")
        snap.record(
            "A", diagnostics.STATUS_SELECTED, "SELECTED",
            articles=[{"title": "제목", "url": "u", "source": "명시적소스", "press": "조선일보"}],
        )
        stored = snap.payload_decisions()[0]["articles"][0]
        self.assertEqual(stored["source"], "명시적소스")

    def test_article_without_press_or_source_omits_source_key(self):
        snap = diagnostics.PassSnapshot("pass1")
        snap.record(
            "A", diagnostics.STATUS_SELECTED, "SELECTED",
            articles=[{"title": "제목", "url": "u"}],
        )
        stored = snap.payload_decisions()[0]["articles"][0]
        self.assertNotIn("source", stored)

    def test_counts_invariant(self):
        snap = diagnostics.PassSnapshot("pass1")
        snap.record("a", diagnostics.STATUS_SELECTED, "SELECTED")
        snap.record("b", diagnostics.STATUS_SELECTED_NO_REP, "NO_REPRESENTATIVE")
        snap.record("c", diagnostics.STATUS_NOT_SELECTED, "RANK_CUTOFF")
        snap.record("d", diagnostics.STATUS_NOT_SELECTED, "GENERIC_SINGLETON")
        c = snap.counts()
        self.assertEqual(c["candidate_count"], 4)
        self.assertEqual(c["selected_count"], 2)          # NO_REPRESENTATIVE 포함
        self.assertEqual(c["not_selected_count"], 1)      # RANK_CUTOFF만
        self.assertEqual(c["rule_excluded_count"], 1)
        self.assertEqual(c["no_representative_count"], 1) # selected의 부분집합
        self.assertEqual(
            c["candidate_count"],
            c["selected_count"] + c["not_selected_count"] + c["rule_excluded_count"],
        )


class ErrorSummaryHygieneTest(unittest.TestCase):
    """§10-1 — 예외 메시지/secret/payload를 남기지 않는다."""

    def test_error_summary_is_type_name_only(self):
        run = diagnostics.RunDiagnostics()
        toxic = RuntimeError(
            "APIError: {'Authorization': 'Bearer SUPABASE_KEY=secret123', "
            "'payload': {'article_body': '기사 전체 본문'}}"
        )
        run.mark_failed(None, toxic)
        self.assertEqual(run.error_summary, "RuntimeError")

        run.commit(diagnostics.PassSnapshot("pass1"))
        payload, decisions = run.build_payload("ts:2026-07-16T00:00:00+00:00")
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("secret123", blob)
        self.assertNotIn("Bearer", blob)
        self.assertNotIn("기사 전체 본문", blob)

    def test_degraded_errors_hold_type_names_only(self):
        snap = diagnostics.PassSnapshot("pass1")
        snap.mark_degraded(ValueError("SUPABASE_KEY=secret123 유출 문자열"))
        self.assertEqual(snap.errors, ["ValueError"])
        self.assertNotIn("secret123", json.dumps(snap.errors))


class PayloadTest(unittest.TestCase):

    def test_collected_count_in_thresholds_not_in_invariant(self):
        """collected_candidate_count는 thresholds 메타값 — 합계에 넣지 않는다(§8-1)."""
        run = diagnostics.RunDiagnostics()
        run.mark_collected(30)
        snap = diagnostics.PassSnapshot("pass1")
        snap.record("a", diagnostics.STATUS_SELECTED, "SELECTED")
        run.commit(snap)
        payload, decisions = run.build_payload("ts:x")
        self.assertEqual(payload["thresholds"]["collected_candidate_count"], 30)
        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(len(decisions), 1)

    def test_over_limit_decisions_raise(self):
        run = diagnostics.RunDiagnostics()
        snap = diagnostics.PassSnapshot("pass1")
        for i in range(diagnostics.MAX_DECISIONS + 1):
            snap.record(f"k{i}", diagnostics.STATUS_NOT_SELECTED, "RANK_CUTOFF")
        run.commit(snap)
        with self.assertRaises(ValueError):
            run.build_payload("ts:x")

    def test_run_key_local_uses_ts_prefix(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(diagnostics.build_run_key().startswith("ts:"))

    def test_run_key_github_uses_id_and_attempt(self):
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "16999", "GITHUB_RUN_ATTEMPT": "2"}):
            self.assertEqual(diagnostics.build_run_key(), "16999:2")

    def test_different_attempt_yields_different_run_key(self):
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "16999", "GITHUB_RUN_ATTEMPT": "1"}):
            first = diagnostics.build_run_key()
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "16999", "GITHUB_RUN_ATTEMPT": "2"}):
            second = diagnostics.build_run_key()
        self.assertNotEqual(first, second)

    def test_rules_version_passed_through_to_payload(self):
        run = diagnostics.RunDiagnostics()
        run.commit(diagnostics.PassSnapshot("pass1"))
        payload, _ = run.build_payload("ts:x", rules_version=diagnostics.RULES_VERSION)
        self.assertEqual(payload["rules_version"], diagnostics.RULES_VERSION)

    def test_rules_version_defaults_to_none_when_not_passed(self):
        """호출부가 실수로 인자를 빼먹으면 예전처럼 조용히 NULL — 회귀를 명시적으로 남긴다."""
        run = diagnostics.RunDiagnostics()
        run.commit(diagnostics.PassSnapshot("pass1"))
        payload, _ = run.build_payload("ts:x")
        self.assertIsNone(payload["rules_version"])

    def test_main_module_passes_rules_version(self):
        """main.py의 실제 호출부가 RULES_VERSION을 빠뜨리지 않는지 회귀 방지."""
        run = diagnostics.RunDiagnostics()
        run.commit(diagnostics.PassSnapshot("pass1"))
        with patch.object(main_module, "record_news_diagnostics", return_value=True) as rpc:
            main_module._finalize_diagnostics(run)
            payload = rpc.call_args[0][0]
            self.assertEqual(payload["rules_version"], diagnostics.RULES_VERSION)

    def test_duration_ms_is_nonnegative_integer(self):
        run = diagnostics.RunDiagnostics()
        run.commit(diagnostics.PassSnapshot("pass1"))
        time.sleep(0.01)
        payload, _ = run.build_payload("ts:x")
        self.assertIsInstance(payload["duration_ms"], int)
        self.assertGreaterEqual(payload["duration_ms"], 0)

    def test_duration_ms_populated_on_failed_run(self):
        run = diagnostics.RunDiagnostics()
        run.mark_failed("NEWS_TOP_UPSERT_FAILED")
        run.commit(diagnostics.PassSnapshot("pass1"))
        time.sleep(0.01)
        payload, _ = run.build_payload("ts:x")
        self.assertIsInstance(payload["duration_ms"], int)
        self.assertGreaterEqual(payload["duration_ms"], 0)

    def test_duration_ms_populated_on_skipped_run(self):
        run = diagnostics.RunDiagnostics()
        run.mark_skipped("NO_CANDIDATES")
        run.commit(diagnostics.PassSnapshot("pass1"))
        time.sleep(0.01)
        payload, _ = run.build_payload("ts:x")
        self.assertIsInstance(payload["duration_ms"], int)
        self.assertGreaterEqual(payload["duration_ms"], 0)

    def test_duration_ms_uses_monotonic_not_wall_clock(self):
        """시스템 시각이 뒤로 감겨도(NTP 보정 등) duration이 음수로 새지 않는다."""
        run = diagnostics.RunDiagnostics()
        run.commit(diagnostics.PassSnapshot("pass1"))
        with patch("news.diagnostics._now_iso", return_value="2000-01-01T00:00:00+00:00"):
            payload, _ = run.build_payload("ts:x")
        self.assertIsInstance(payload["duration_ms"], int)
        self.assertGreaterEqual(payload["duration_ms"], 0)


class DegradedGateTest(unittest.TestCase):
    """§3-1 — 부분 이력 저장 금지."""

    def test_adopted_snapshot_degraded_blocks_rpc(self):
        run = diagnostics.RunDiagnostics()
        snap = diagnostics.PassSnapshot("pass1")
        snap.record("a", diagnostics.STATUS_SELECTED, "SELECTED")
        snap.mark_degraded(ValueError("x"))
        run.commit(snap)
        self.assertTrue(run.is_degraded())

        with patch.object(main_module, "record_news_diagnostics") as rpc:
            main_module._finalize_diagnostics(run)
            rpc.assert_not_called()

    def test_run_global_degraded_blocks_rpc(self):
        run = diagnostics.RunDiagnostics()
        run.commit(diagnostics.PassSnapshot("pass1"))
        run.mark_degraded(RuntimeError("payload 조립 실패"))
        with patch.object(main_module, "record_news_diagnostics") as rpc:
            main_module._finalize_diagnostics(run)
            rpc.assert_not_called()

    def test_discarded_pass_degraded_does_not_block_healthy_save(self):
        """폐기된 pass2의 degraded는 정상 pass1 저장을 막지 않는다(계약 2)."""
        run = diagnostics.RunDiagnostics()
        snap1 = diagnostics.PassSnapshot("pass1(strict)")
        snap1.record("a", diagnostics.STATUS_SELECTED, "SELECTED")
        snap2 = diagnostics.PassSnapshot("pass2(backfill)")
        snap2.mark_degraded(ValueError("pass2만 터짐"))
        run.commit(snap1)          # pass2 폐기 → commit 안 됨
        self.assertFalse(run.is_degraded())

        with patch.object(main_module, "record_news_diagnostics", return_value=True) as rpc:
            main_module._finalize_diagnostics(run)
            rpc.assert_called_once()
            payload, decisions = rpc.call_args[0]
            self.assertEqual(payload["pass_name"], "pass1(strict)")
            self.assertEqual(len(decisions), 1)

    def test_rpc_failure_is_isolated(self):
        run = diagnostics.RunDiagnostics()
        snap = diagnostics.PassSnapshot("pass1")
        snap.record("a", diagnostics.STATUS_SELECTED, "SELECTED")
        run.commit(snap)
        with patch.object(main_module, "record_news_diagnostics", side_effect=RuntimeError("boom")):
            main_module._finalize_diagnostics(run)   # 예외가 새면 실패


class RankingInvarianceTest(unittest.TestCase):
    """진단 ON/OFF에서 랭킹 결과가 완전히 동일해야 한다."""

    def _run(self, diag):
        kws = ["민경욱", "코스피", "환율", "폭염"]
        return main_module._rank_and_select(
            _candidates(*kws), _signals(*kws), "pass1(strict)", diag=diag,
        )

    def test_ranking_identical_with_and_without_diagnostics(self):
        top_off = self._run(None)
        top_on = self._run(diagnostics.PassSnapshot("pass1"))
        self.assertEqual(
            json.dumps(top_off, sort_keys=True, default=str),
            json.dumps(top_on, sort_keys=True, default=str),
        )

    def test_record_exception_does_not_change_ranking(self):
        """diag.record가 터져도 랭킹은 불변이고 부분 이력도 저장되지 않는다."""
        class Exploding(diagnostics.PassSnapshot):
            def record(self, *a, **kw):
                raise RuntimeError("record 폭발")

        top_off = self._run(None)
        snap = Exploding("pass1")
        top_on = self._run(snap)
        self.assertEqual(
            json.dumps(top_off, sort_keys=True, default=str),
            json.dumps(top_on, sort_keys=True, default=str),
        )
        self.assertTrue(snap.degraded)

        run = diagnostics.RunDiagnostics()
        run.commit(snap)
        with patch.object(main_module, "record_news_diagnostics") as rpc:
            main_module._finalize_diagnostics(run)
            rpc.assert_not_called()

    def test_every_candidate_gets_exactly_one_decision(self):
        kws = ["민경욱", "코스피", "환율", "폭염"]
        snap = diagnostics.PassSnapshot("pass1")
        main_module._rank_and_select(
            _candidates(*kws), _signals(*kws), "pass1(strict)", diag=snap,
        )
        self.assertFalse(snap.degraded, f"degraded: {snap.errors}")
        self.assertEqual(len(snap.decisions), len(kws))
        c = snap.counts()
        self.assertEqual(
            c["candidate_count"],
            c["selected_count"] + c["not_selected_count"] + c["rule_excluded_count"],
        )

    def test_gate_dropped_candidate_gets_real_reason_code(self):
        """품질 게이트 탈락은 추측 코드가 아니라 실제 판정 함수의 사유로 기록된다."""
        kws = ["민경욱", "코스피"]
        cands = _candidates(*kws)
        sig = _signals(*kws)
        # 코스피: 고관련 기사 0 + cluster 0 → low_quality_news
        sig["news"]["코스피"] = _news_meta(
            keyword="코스피", high_rel=0, cluster=0, fresh_high_rel=0, fresh_cluster=0,
        )
        snap = diagnostics.PassSnapshot("pass1")
        main_module._rank_and_select(cands, sig, "pass1(strict)", diag=snap)
        row = snap.decisions[diagnostics._norm_key("코스피")]
        self.assertEqual(row["result_status"], diagnostics.STATUS_NOT_SELECTED)
        self.assertEqual(row["reason_code"], "LOW_QUALITY_NEWS")

    def test_display_consistency_reject_branches_are_distinguished(self):
        """enforce_display_article_consistency의 두 reject 분기가 각각 다른 코드로 남는다.

        이 단계는 제외 목록을 반환하지 않아 차집합만으로는 구분이 불가능하다 —
        구분에 실패하면 DISPLAY_GENERIC_ONLY가 영구 도달 불가 코드가 된다.
        """
        from news import ranker

        # 실제 ranker 판정과 대조: generic-only면 DISPLAY_GENERIC_ONLY여야 한다.
        self.assertTrue(ranker._is_generic_only_display("신임"))
        self.assertFalse(ranker._is_generic_only_display("민경욱"))

        kws = ["신임", "민경욱"]
        cands = _candidates(*kws)
        sig = _signals(*kws)
        # display 정합성 단계에서 떨어지도록 기사 제목에서 키워드 근거를 제거한다.
        for k in kws:
            for a in sig["news"][k]["articles"]:
                a["title"] = "무관한 다른 사건 보도"
                a["is_primary_cluster"] = False

        snap = diagnostics.PassSnapshot("pass1")
        main_module._rank_and_select(cands, sig, "pass1(strict)", diag=snap)

        codes = {row["keyword"]: row["reason_code"] for row in snap.payload_decisions()}
        # 조건부 assert를 쓰지 않는다 — 두 분기에 도달하지 못하면 그 자체로 실패해야 한다.
        # (도달 못 해도 통과하면 DISPLAY_GENERIC_ONLY 회귀를 영영 못 잡는다)
        self.assertEqual(codes.get("신임"), "DISPLAY_GENERIC_ONLY")
        self.assertEqual(codes.get("민경욱"), "DISPLAY_ARTICLE_INCONSISTENT")

    def test_no_news_signal_candidate_gets_no_news_evidence(self):
        kws = ["민경욱", "코스피"]
        cands = _candidates(*kws)
        sig = _signals(*kws)
        del sig["news"]["코스피"]
        snap = diagnostics.PassSnapshot("pass1")
        main_module._rank_and_select(cands, sig, "pass1(strict)", diag=snap)
        row = snap.decisions[diagnostics._norm_key("코스피")]
        self.assertEqual(row["reason_code"], "NO_NEWS_EVIDENCE")


# 대표(summary_type='rule')가 실제로 뽑히는 fixture.
# 조건(news/summarizer.py): evidence 2건 이상 + 키워드를 뺀 공통 하위주제 토큰이
# _SUBTOPIC_MIN_TOKENS(2) 이상. 공통 토큰이 1개면 no_representative가 된다.
# 키워드 수는 harness와 동일하게 유지한다 — MIN_RECENT_KEYWORDS(5) 가드에 걸리면
# recent_guard_failed로 skip돼 발행 자체가 일어나지 않는다.
_REP_SUBTOPICS = {
    "민경욱": ("구속영장", "법원"),
    "코스피": ("반도체주", "급등"),
    "환율": ("당국개입", "관측"),
    "폭염": ("온열질환", "경보"),
    "지진": ("여진", "대피"),
    "태풍": ("북상", "경로"),
}
_REP_KEYWORDS = list(_REP_SUBTOPICS)


def _rep_signals():
    sig = _signals(*_REP_KEYWORDS)
    for kw, (tok1, tok2) in _REP_SUBTOPICS.items():
        arts = sig["news"][kw]["articles"]
        arts[0]["title"] = f"{kw} {tok1} {tok2} 청구"
        arts[1]["title"] = f"{kw} {tok1} {tok2} 심사"
        sig["news"][kw]["representative_article"] = arts[0]
        sig["news"][kw]["representative_title"] = arts[0]["title"]
    return sig


class _BriefingHarness:
    """run_news_briefing의 외부 의존성을 막고 pass 흐름만 결정적으로 구동한다.

    실제 랭킹(_rank_and_select)은 그대로 돌린다 — 진단이 실제 판정을 따라가는지 봐야 하므로
    _rank_and_select를 가짜로 대체하지 않는다.
    """

    PASS1 = ["민경욱", "코스피", "환율", "폭염", "지진", "태풍"]
    # pass2 전용 sentinel — pass1 집합과 disjoint. 혼입 여부 판별에 쓴다.
    PASS2_ONLY = ["백신접종", "국제유가"]

    def __init__(self, testcase, backfill_result="adopt"):
        self.tc = testcase
        self.backfill_result = backfill_result
        self.patches = []

    def __enter__(self):
        m = main_module
        p = [
            patch.object(m, "_collect_home_seeds",
                         return_value=({"daum_home": self.PASS1}, {"daum_home": "ok"})),
            patch.object(m.google_adapter, "fetch_candidates", return_value=[]),
            patch.object(m, "_cache_google_keywords"),
            patch.object(m.cand, "derive_aux_keywords", return_value=[]),
            patch.object(m, "_seed_sources_from", return_value={"daum_home": self.PASS1}),
            patch.object(m.cand, "collect_candidates", return_value=_candidates(*self.PASS1)),
            patch.object(m.cand, "count_source_families", return_value=9),
            patch.object(m.cand, "build_news_signals",
                         return_value=_signals(*self.PASS1)["news"]),
            patch.object(m.datalab_adapter, "fetch", return_value={}),
            patch.object(m.google_adapter, "fetch_signals", return_value={}),
            patch.object(m, "fetch_news_issues", return_value=None),
            patch.object(m, "apply_movement", side_effect=lambda prev, iss: iss),
            patch.object(m, "enrich_issue_thumbnails", side_effect=lambda iss, prev: iss),
            patch.object(m, "search_news", return_value=None),
        ]
        for x in p:
            x.start()
            self.patches.append(x)
        return self

    def __exit__(self, *exc):
        for x in reversed(self.patches):
            x.stop()
        return False


class BriefingPassFlowTest(unittest.TestCase):
    """§3-1 / §8-1-1 — pass 소유권·격리·후랭킹 skip snapshot 보존."""

    def _backfill(self, top2_keywords, degraded=False):
        """_backfill_pass 대역 — snapshot에 pass2 판정을 남기고 채택/폐기를 결정한다."""
        def fake(pass1_top, *a, diag=None, **kw):
            try:
                if degraded and diag is not None:
                    diag.mark_degraded(ValueError("pass2 진단 실패"))
                if diag is not None:
                    for k in top2_keywords:
                        diag.record(k, diagnostics.STATUS_SELECTED, "SELECTED")
                if top2_keywords is None:
                    return None, None
                return None, None
            finally:
                if diag is not None:
                    diag.close()
        return fake

    def test_discarded_pass2_does_not_leak_into_saved_history(self):
        """폐기된 pass2의 decisions/degraded가 최종 저장에 섞이지 않는다."""
        with _BriefingHarness(self), \
             patch.object(main_module, "upsert_news_issues", return_value=True), \
             patch.object(main_module, "_backfill_pass",
                          side_effect=self._backfill(_BriefingHarness.PASS2_ONLY, degraded=True)), \
             patch.object(main_module, "record_news_diagnostics", return_value=True) as rpc:
            main_module.run_news_briefing()

            rpc.assert_called_once()   # 폐기 pass의 degraded는 정상 저장을 막지 않는다
            payload, decisions = rpc.call_args[0]
            self.assertEqual(payload["pass_name"], "pass1(strict)")
            saved = {d["keyword"] for d in decisions}
            for sentinel in _BriefingHarness.PASS2_ONLY:
                self.assertNotIn(sentinel, saved, "폐기된 pass2 decisions가 혼입됐다")
            self.assertTrue(saved <= set(_BriefingHarness.PASS1))

    def test_saved_decisions_match_published_payload_exactly(self):
        """저장된 selected의 keyword·rank·대표 여부가 실제 발행 payload와 1:1 일치한다.

        비교 대상은 _rank_and_select 반환값이 아니라 **실제 발행되는 issues["keywords"]** 다.
        전자에는 rank/summary/대표가 아직 없어(builder 이전) 양쪽 None끼리 비교하는
        위양성 테스트가 된다(Codex diff review P1).
        """
        published = []
        with _BriefingHarness(self), \
             patch.object(main_module, "upsert_news_issues",
                          side_effect=lambda i, source="news_top": published.append(i) or True), \
             patch.object(main_module, "record_news_diagnostics", return_value=True) as rpc:
            main_module.run_news_briefing()

            payload, decisions = rpc.call_args[0]
            entries = published[0]["keywords"]

            def from_entry(e):
                # 대표 없음의 권위 기준은 builder와 동일하게 summary_type이다.
                has_rep = e.get("summary_type") != "no_representative"
                return (
                    e["keyword"], e.get("rank"), e.get("score"), e.get("summary"),
                    e.get("summary_type"), e.get("display_keyword"), has_rep,
                    e.get("representative_title"),
                    len(e.get("display_articles") or []),
                    len(e.get("articles") or []),
                    json.dumps(e.get("signals"), sort_keys=True),
                )

            def from_decision(d):
                # bool()로 감싸지 않는다 — 필드 누락(None)과 명시적 False를 구분해야 한다.
                return (
                    d["keyword"], d.get("rank"), d.get("score"), d.get("summary"),
                    d.get("summary_type"), d.get("display_keyword"),
                    d["has_representative"], d.get("representative_title"),
                    d.get("display_article_count"), d.get("article_count"),
                    json.dumps(d.get("signals"), sort_keys=True),
                )

            expected = sorted(from_entry(e) for e in entries)
            got = sorted(
                from_decision(d) for d in decisions
                if d["result_status"] in (diagnostics.STATUS_SELECTED,
                                          diagnostics.STATUS_SELECTED_NO_REP)
            )
            self.assertEqual(expected, got)
            # 값이 전부 None이면 위양성이다 — 실제 값이 실렸는지 확인한다.
            self.assertTrue(all(row[1] is not None for row in got))   # rank
            self.assertTrue(all(row[8] > 0 for row in got))           # display_article_count

    def test_representative_path_is_recorded(self):
        """대표가 실제로 뽑히는 실행에서 has_representative=True와 대표 필드가 기록된다.

        harness 기본 fixture는 전부 no_representative라, 이 경로가 없으면
        has_representative=True / representative_url 채움 로직이 한 번도 검증되지 않는다.
        """
        published = []
        with _BriefingHarness(self) as h, \
             patch.object(main_module.cand, "build_news_signals",
                          return_value=_rep_signals()["news"]), \
             patch.object(main_module.cand, "collect_candidates",
                          return_value=_candidates(*_REP_KEYWORDS)), \
             patch.object(main_module, "upsert_news_issues",
                          side_effect=lambda i, source="news_top": published.append(i) or True), \
             patch.object(main_module, "record_news_diagnostics", return_value=True) as rpc:
            main_module.run_news_briefing()

            entries = {e["keyword"]: e for e in published[0]["keywords"]}
            _, decisions = rpc.call_args[0]
            rows = {d["keyword"]: d for d in decisions}

            with_rep = [k for k, e in entries.items()
                        if e.get("summary_type") != "no_representative"]
            self.assertTrue(with_rep, "대표가 뽑힌 항목이 없어 이 경로를 검증하지 못한다")
            for k in with_rep:
                self.assertTrue(rows[k]["has_representative"])
                self.assertEqual(rows[k]["result_status"], diagnostics.STATUS_SELECTED)
                self.assertEqual(rows[k]["reason_code"], "SELECTED")
                self.assertEqual(rows[k]["summary_type"], entries[k].get("summary_type"))
                # 대표 필드가 실제로 채워졌는지까지 본다(has_rep만 보면 URL 회귀를 놓친다).
                self.assertEqual(rows[k]["representative_title"],
                                 entries[k].get("representative_title"))
                expected_url = (entries[k].get("representative_article") or {}).get("url")
                self.assertEqual(rows[k]["representative_url"], expected_url)
                self.assertTrue(rows[k]["representative_url"])

    def test_news_top_payload_identical_with_diagnostics_on_and_off(self):
        """PR B-1 필수 테스트: 진단 RPC가 성공/실패해도 news_top 발행 payload는
        동일해야 한다(_safe_diag 경계, PR B-1은 이 경계를 약화하지 않는다).

        builder.py가 issues["generated_at"]에 실행 시각(datetime.now())을 매번 새로
        찍으므로 그 필드만 제외하고 비교한다 — 이건 진단과 무관한 기존 동작이라
        diag ON/OFF 비교 대상이 아니다.
        """
        published_with_rpc, published_without_rpc = [], []

        with _BriefingHarness(self), \
             patch.object(main_module, "upsert_news_issues",
                          side_effect=lambda i, source="news_top": published_with_rpc.append(i) or True), \
             patch.object(main_module, "record_news_diagnostics", return_value=True):
            main_module.run_news_briefing()

        with _BriefingHarness(self), \
             patch.object(main_module, "upsert_news_issues",
                          side_effect=lambda i, source="news_top": published_without_rpc.append(i) or True), \
             patch.object(main_module, "record_news_diagnostics", side_effect=RuntimeError("rpc down")):
            main_module.run_news_briefing()

        def without_generated_at(issues):
            return {k: v for k, v in issues.items() if k != "generated_at"}

        self.assertEqual(
            json.dumps(without_generated_at(published_with_rpc[0]), sort_keys=True, default=str),
            json.dumps(without_generated_at(published_without_rpc[0]), sort_keys=True, default=str),
        )

    def test_all_three_fields_populate_on_real_run(self):
        """PR B-1 필수 테스트: rules_version/duration_ms/article.source가 실제
        run_news_briefing() 경로에서 함께 적재되는지(단위 테스트가 아닌 통합 경로)."""
        with _BriefingHarness(self), \
             patch.object(main_module, "upsert_news_issues", return_value=True), \
             patch.object(main_module, "record_news_diagnostics", return_value=True) as rpc:
            main_module.run_news_briefing()
            payload, decisions = rpc.call_args[0]

            self.assertEqual(payload["rules_version"], diagnostics.RULES_VERSION)
            self.assertIsInstance(payload["duration_ms"], int)
            self.assertGreaterEqual(payload["duration_ms"], 0)

            articles_with_source = [
                a for d in decisions for a in (d.get("articles") or []) if a.get("source")
            ]
            self.assertTrue(articles_with_source, "articles에 source가 채워진 항목이 없다")
            for a in articles_with_source:
                self.assertTrue(a["source"].startswith("press"))  # _candidates fixture 값

    def test_count_invariant_holds_on_real_run(self):
        with _BriefingHarness(self), \
             patch.object(main_module, "upsert_news_issues", return_value=True), \
             patch.object(main_module, "record_news_diagnostics", return_value=True) as rpc:
            main_module.run_news_briefing()
            payload, decisions = rpc.call_args[0]
            self.assertEqual(payload["candidate_count"], len(decisions))
            self.assertEqual(
                payload["candidate_count"],
                payload["selected_count"] + payload["not_selected_count"]
                + payload["rule_excluded_count"],
            )
            self.assertEqual(len({d["keyword"] for d in decisions}), len(decisions))

    def test_upsert_false_records_failure_not_success(self):
        with _BriefingHarness(self), \
             patch.object(main_module, "upsert_news_issues", return_value=False), \
             patch.object(main_module, "record_news_diagnostics", return_value=True) as rpc:
            main_module.run_news_briefing()
            payload, _ = rpc.call_args[0]
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["skip_reason"], "NEWS_TOP_UPSERT_FAILED")

    def test_recent_guard_skip_preserves_snapshot_decisions(self):
        """후랭킹 skip은 판정이 실재하므로 0으로 밀지 않는다(§8-1-1)."""
        with _BriefingHarness(self), \
             patch.object(main_module, "_count_recent_keywords", return_value=0), \
             patch.object(main_module, "_backfill_pass", return_value=(None, None)), \
             patch.object(main_module, "upsert_news_issues", return_value=True) as upsert, \
             patch.object(main_module, "record_news_diagnostics", return_value=True) as rpc:
            main_module.run_news_briefing()

            upsert.assert_not_called()          # 랭킹은 skip(last good 유지)
            payload, decisions = rpc.call_args[0]
            self.assertEqual(payload["status"], "skipped")
            self.assertEqual(payload["skip_reason"], "RECENT_GUARD_FAILED")
            self.assertGreater(payload["candidate_count"], 0)
            self.assertEqual(payload["candidate_count"], len(decisions))

    def test_pre_ranking_skip_has_zero_counts(self):
        """랭킹 진입 전 skip은 candidate_count=0, decisions=0(§8-1)."""
        with _BriefingHarness(self), \
             patch.object(main_module.cand, "count_source_families", return_value=1), \
             patch.object(main_module, "upsert_news_issues") as upsert, \
             patch.object(main_module, "record_news_diagnostics", return_value=True) as rpc:
            main_module.run_news_briefing()

            upsert.assert_not_called()
            payload, decisions = rpc.call_args[0]
            self.assertEqual(payload["skip_reason"], "SOURCE_DIVERSITY_FAILED")
            self.assertEqual(payload["candidate_count"], 0)
            self.assertEqual(decisions, [])
            self.assertEqual(payload["selected_count"], 0)
            self.assertEqual(payload["not_selected_count"], 0)
            self.assertEqual(payload["rule_excluded_count"], 0)
            # 수집 원시 후보 수는 버리지 않는다(합계에는 포함하지 않음)
            self.assertEqual(
                payload["thresholds"]["collected_candidate_count"],
                len(_BriefingHarness.PASS1),
            )

    def test_diagnostic_failure_does_not_change_news_top(self):
        """진단 record가 터져도 news_top upsert 인자가 완전히 동일하고, 부분 이력도 없다."""
        def capture():
            calls = []
            def wrapped(issues, source="news_top"):
                # generated_at은 실행 시각이라 두 실행 간에 항상 다르다(진단과 무관) —
                # 이걸 비교에 넣으면 무엇을 검증하는 테스트인지 무의미해진다.
                comparable = {k: v for k, v in issues.items() if k != "generated_at"}
                calls.append(json.dumps(comparable, sort_keys=True, default=str))
                return True
            wrapped.calls = calls
            return wrapped

        healthy = capture()
        with _BriefingHarness(self), \
             patch.object(main_module, "upsert_news_issues", side_effect=healthy), \
             patch.object(main_module, "record_news_diagnostics", return_value=True):
            main_module.run_news_briefing()

        class Exploding(diagnostics.PassSnapshot):
            def record(self, *a, **kw):
                raise RuntimeError("record 폭발")

        broken = capture()
        with _BriefingHarness(self), \
             patch.object(main_module, "upsert_news_issues", side_effect=broken), \
             patch.object(main_module.diagnostics, "PassSnapshot", Exploding), \
             patch.object(main_module, "record_news_diagnostics") as rpc:
            main_module.run_news_briefing()
            # 채택 snapshot이 degraded → 부분 이력을 저장하지 않는다
            rpc.assert_not_called()

        # 진단이 죽어도 news_top에 올라간 payload는 문자 단위로 동일해야 한다
        self.assertEqual(healthy.calls, broken.calls)
        self.assertEqual(len(healthy.calls), 1)

    def test_snapshot_construction_failure_publishes_but_saves_nothing(self):
        """snapshot 생성 실패 → 발행은 정상, 진단은 빈 success 대신 저장 생략(run 전역 degraded)."""
        with _BriefingHarness(self), \
             patch.object(main_module, "upsert_news_issues", return_value=True) as upsert, \
             patch.object(main_module.diagnostics, "PassSnapshot",
                          side_effect=RuntimeError("snapshot 생성 폭발")), \
             patch.object(main_module, "record_news_diagnostics") as rpc:
            main_module.run_news_briefing()
            upsert.assert_called_once()      # 랭킹/발행은 영향 없음
            rpc.assert_not_called()          # candidate_count=0 'success' 위조 이력 금지

    def test_payload_build_failure_blocks_save(self):
        """payload 조립 오류는 run 전역 degraded → RPC 미호출."""
        run = diagnostics.RunDiagnostics()
        snap = diagnostics.PassSnapshot("pass1")
        snap.record("a", diagnostics.STATUS_SELECTED, "SELECTED")
        run.commit(snap)
        with patch.object(run, "build_payload", side_effect=RuntimeError("조립 실패")), \
             patch.object(main_module, "record_news_diagnostics") as rpc:
            main_module._finalize_diagnostics(run)
            rpc.assert_not_called()
        self.assertTrue(run.degraded)

    def test_run_type_matches_deployed_check_constraint(self):
        """run_type은 배포 SQL CHECK 허용값이어야 한다(RPC mock으로는 안 잡히는 계약)."""
        with _BriefingHarness(self), \
             patch.object(main_module, "upsert_news_issues", return_value=True), \
             patch.object(main_module, "record_news_diagnostics", return_value=True) as rpc:
            main_module.run_news_briefing()
            payload, _ = rpc.call_args[0]
            self.assertIn(payload["run_type"], diagnostics.RUN_TYPES)
            self.assertEqual(payload["run_type"], "full")

    def test_news_top_only_mode_uses_its_own_run_type(self):
        with _BriefingHarness(self), \
             patch.object(main_module, "upsert_news_issues", return_value=True), \
             patch.object(main_module, "record_news_diagnostics", return_value=True) as rpc:
            main_module.run_news_briefing(run_type="news_top_only")
            payload, _ = rpc.call_args[0]
            self.assertEqual(payload["run_type"], "news_top_only")

    def test_invalid_run_type_rejected(self):
        with self.assertRaises(ValueError):
            diagnostics.RunDiagnostics(run_type="scheduled")


class FreshnessCompareTest(unittest.TestCase):
    """freshness guard 순수 helper — _is_stale_news_top_write / _parse_generated_at.

    new < previous 일 때만 stale(True). 그 외(같음/최신/결측/비문자열/파싱실패)는 전부
    write 허용(False, fail-open) — 정상 신선 실행을 막지 않는다.
    """

    @staticmethod
    def _p(gen):
        return {"generated_at": gen}

    def test_1_new_older_than_previous_is_stale(self):
        self.assertTrue(main_module._is_stale_news_top_write(
            self._p("2026-07-20T05:00:00+00:00"), self._p("2026-07-20T04:00:00+00:00")))

    def test_2_new_newer_allows_write(self):
        self.assertFalse(main_module._is_stale_news_top_write(
            self._p("2026-07-20T04:00:00+00:00"), self._p("2026-07-20T05:00:00+00:00")))

    def test_3_equal_instant_allows_write(self):
        self.assertFalse(main_module._is_stale_news_top_write(
            self._p("2026-07-20T04:00:00+00:00"), self._p("2026-07-20T04:00:00+00:00")))

    def test_4_z_and_offset_mixed(self):
        # previous=Z(=+00:00) 05:00, new=+00:00 04:00 → new older → stale
        self.assertTrue(main_module._is_stale_news_top_write(
            self._p("2026-07-20T05:00:00Z"), self._p("2026-07-20T04:00:00+00:00")))

    def test_5_different_offset_same_absolute_instant(self):
        # 04:00+00:00 == 13:00+09:00 (동일 절대시각) → 같음 → 허용
        self.assertFalse(main_module._is_stale_news_top_write(
            self._p("2026-07-20T04:00:00+00:00"), self._p("2026-07-20T13:00:00+09:00")))

    def test_6_previous_naive_treated_as_utc(self):
        # previous naive 05:00(=UTC), new 04:00Z → new older → stale
        self.assertTrue(main_module._is_stale_news_top_write(
            self._p("2026-07-20T05:00:00"), self._p("2026-07-20T04:00:00Z")))

    def test_6b_new_naive_treated_as_utc(self):
        # new naive 06:00(=UTC), previous 05:00Z → new newer → 허용
        self.assertFalse(main_module._is_stale_news_top_write(
            self._p("2026-07-20T05:00:00Z"), self._p("2026-07-20T06:00:00")))

    def test_7_missing_generated_at_allows_write(self):
        self.assertFalse(main_module._is_stale_news_top_write({}, {}))
        self.assertFalse(main_module._is_stale_news_top_write(
            self._p("2026-07-20T05:00:00Z"), {}))
        self.assertFalse(main_module._is_stale_news_top_write(
            {}, self._p("2026-07-20T04:00:00Z")))

    def test_8_non_string_allows_write(self):
        for bad in (12345, {"a": 1}, ["x"], object()):
            self.assertFalse(main_module._is_stale_news_top_write(
                self._p(bad), self._p("2026-07-20T04:00:00Z")))
            self.assertFalse(main_module._is_stale_news_top_write(
                self._p("2026-07-20T05:00:00Z"), self._p(bad)))

    def test_8b_previous_not_dict_allows_write(self):
        for bad in (None, "x", 5, ["l"]):
            self.assertFalse(main_module._is_stale_news_top_write(
                bad, self._p("2026-07-20T04:00:00Z")))

    def test_9_parse_failure_allows_write(self):
        self.assertFalse(main_module._is_stale_news_top_write(
            self._p("not-a-date"), self._p("2026-07-20T04:00:00Z")))
        self.assertFalse(main_module._is_stale_news_top_write(
            self._p("2026-07-20T05:00:00Z"), self._p("garbage")))


def _future_issues_previous(minutes_ahead=30):
    """harness 의 news_top 발행 payload(generated_at=now)보다 미래인 previous 를 만든다.

    builder 가 issues.generated_at 에 datetime.now(UTC) 를 찍으므로, 그보다 미래의
    previous 를 fetch_news_issues 가 반환하면 새 실행은 stale 로 판정된다.
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    future = (_dt.now(_tz.utc) + _td(minutes=minutes_ahead)).isoformat()
    return {"generated_at": future, "keywords": []}


class FreshnessGuardBriefingTest(unittest.TestCase):
    """run_news_briefing 통합 — stale skip 계약 + 정상 계약 (fetch/upsert/record mock).

    '실제 read→write 경합'이 아니라 '이미 관측된 최신값(previous)보다 오래된 실행의
    후행 write 차단'을 검증한다(TOCTOU 는 별도 후속 과제).
    """

    def test_stale_run_skips_upsert_but_records_skipped(self):
        """stale: news_top upsert 0회, 진단 1회, status=skipped/STALE_WRITE_SKIPPED,
        decisions·근거 보존, NEWS_TOP_UPSERT_FAILED 오분류 아님."""
        prev = _future_issues_previous(30)
        with _BriefingHarness(self), \
             patch.object(main_module, "fetch_news_issues", return_value=prev), \
             patch.object(main_module, "upsert_news_issues", return_value=True) as up, \
             patch.object(main_module, "record_news_diagnostics", return_value=True) as rpc:
            main_module.run_news_briefing(run_type="news_top_only")

            up.assert_not_called()                 # (10) upsert 0회
            rpc.assert_called_once()               # (11) 진단 1회
            payload, decisions = rpc.call_args[0]
            self.assertEqual(payload["status"], "skipped")               # (12)
            self.assertEqual(payload["skip_reason"], "STALE_WRITE_SKIPPED")  # (13)
            self.assertNotEqual(payload["status"], "success")            # (17/21)
            self.assertNotEqual(payload["skip_reason"], "NEWS_TOP_UPSERT_FAILED")  # (19)
            # (14~16) candidates·decisions·rejection 보존
            self.assertGreater(payload["candidate_count"], 0)
            self.assertGreater(len(decisions), 0)
            # (18) selection_diagnostics_v1 보존 + (17) stale_write_v1 근거
            thr = payload["thresholds"]
            self.assertIn("selection_diagnostics_v1", thr)
            self.assertIn("stale_write_v1", thr)
            sw = thr["stale_write_v1"]
            self.assertEqual(sw["action"], "skip_stale_write")
            self.assertEqual(sw["mode"], "news_top_only")
            self.assertEqual(sw["comparison"], "new_older_than_previous")
            self.assertEqual(sw["previous_generated_at"], prev["generated_at"])
            self.assertTrue(sw["new_generated_at"])
            # (20) 근거에 secret/payload 전문 없음 — 키 집합이 고정 화이트리스트뿐
            self.assertEqual(set(sw.keys()), {
                "action", "previous_generated_at", "new_generated_at",
                "mode", "run_id", "comparison", "result"})

    def test_stale_run_id_from_github_env(self):
        """run_id: GITHUB_RUN_ID 있으면 그 값, 없으면 None(로컬)."""
        prev = _future_issues_previous(30)
        # (a) 환경값 있음
        with _BriefingHarness(self), \
             patch.object(main_module, "fetch_news_issues", return_value=prev), \
             patch.object(main_module, "upsert_news_issues", return_value=True), \
             patch.object(main_module, "record_news_diagnostics", return_value=True) as rpc, \
             patch.dict(main_module.os.environ, {"GITHUB_RUN_ID": "987654"}):
            main_module.run_news_briefing(run_type="news_top_only")
            payload, _ = rpc.call_args[0]
            self.assertEqual(payload["thresholds"]["stale_write_v1"]["run_id"], "987654")
        # (b) 환경값 없음 → None
        with _BriefingHarness(self), \
             patch.object(main_module, "fetch_news_issues", return_value=prev), \
             patch.object(main_module, "upsert_news_issues", return_value=True), \
             patch.object(main_module, "record_news_diagnostics", return_value=True) as rpc:
            with patch.dict(main_module.os.environ, {}, clear=False):
                main_module.os.environ.pop("GITHUB_RUN_ID", None)
                main_module.run_news_briefing(run_type="news_top_only")
            payload, _ = rpc.call_args[0]
            self.assertIsNone(payload["thresholds"]["stale_write_v1"]["run_id"])

    def test_stale_skip_logs_without_secret(self):
        """(20) stale skip 로그가 남고, payload 전문·secret 없이 generated_at/mode 만."""
        prev = _future_issues_previous(30)
        with _BriefingHarness(self), \
             patch.object(main_module, "fetch_news_issues", return_value=prev), \
             patch.object(main_module, "upsert_news_issues", return_value=True), \
             patch.object(main_module, "record_news_diagnostics", return_value=True):
            with self.assertLogs("main", level="WARNING") as cm:
                main_module.run_news_briefing(run_type="news_top_only")
        joined = "\n".join(cm.output)
        self.assertIn("skip_stale_write", joined)
        self.assertNotIn("apikey", joined.lower())
        self.assertNotIn("bearer", joined.lower())

    def test_fresh_run_upserts_and_records_success(self):
        """(22~24) 최신 실행: news_top upsert 1회, status=success, skip_reason 없음."""
        # previous 없음(None) → fresh 로 간주 → 정상 발행
        with _BriefingHarness(self), \
             patch.object(main_module, "upsert_news_issues", return_value=True) as up, \
             patch.object(main_module, "record_news_diagnostics", return_value=True) as rpc:
            main_module.run_news_briefing(run_type="news_top_only")
            up.assert_called_once()                # (22)
            payload, _ = rpc.call_args[0]
            self.assertEqual(payload["status"], "success")     # (23)
            self.assertIsNone(payload["skip_reason"])          # (24)
            self.assertNotIn("stale_write_v1", payload["thresholds"])

    def test_older_previous_allows_write(self):
        """(25) previous.generated_at 이 새 payload 보다 과거면 write 허용(정상 발행).

        ※ 동일 절대시각(equal instant) 자체는 helper 단위(FreshnessCompareTest.test_3/5)에서
          정밀 검증한다. 이 통합 테스트는 briefing 경로에서 '과거 previous → 정상 upsert'만 본다.
        """
        published = []
        past = {"generated_at": "2000-01-01T00:00:00+00:00", "keywords": []}
        with _BriefingHarness(self), \
             patch.object(main_module, "fetch_news_issues", return_value=past), \
             patch.object(main_module, "upsert_news_issues",
                          side_effect=lambda i, source="news_top": published.append(i) or True) as up, \
             patch.object(main_module, "record_news_diagnostics", return_value=True):
            main_module.run_news_briefing(run_type="news_top_only")
            up.assert_called_once()

    def test_reversed_order_older_run_blocked_newer_preserved(self):
        """(27~29) 이미 저장된 최신값 B(previous) 이후 오래된 실행 A 가 저장 시도 →
        A 의 upsert 차단, A 진단은 skipped 1회, B 의 news_top 유지(덮이지 않음).

        ※ 실제 동시성이 아니라 '관측된 최신값보다 오래된 후행 write 차단' 검증.
        """
        b_generated = _future_issues_previous(60)   # B = 미래(=이미 저장된 더 최신)
        with _BriefingHarness(self), \
             patch.object(main_module, "fetch_news_issues", return_value=b_generated), \
             patch.object(main_module, "upsert_news_issues", return_value=True) as up, \
             patch.object(main_module, "record_news_diagnostics", return_value=True) as rpc:
            main_module.run_news_briefing(run_type="full")   # A 실행(new=now < B)
            up.assert_not_called()                            # (27) A upsert 차단
            rpc.assert_called_once()                          # (28) A 진단 1회
            payload, _ = rpc.call_args[0]
            self.assertEqual(payload["status"], "skipped")
            self.assertEqual(payload["skip_reason"], "STALE_WRITE_SKIPPED")
            self.assertEqual(payload["thresholds"]["stale_write_v1"]["mode"], "full")
        # (29) B(previous)는 upsert 가 호출되지 않았으므로 그대로 유지됨(덮이지 않음).


if __name__ == "__main__":
    unittest.main()


class CompactObservabilityTest(unittest.TestCase):
    """2026-09 관측성 PR — 비선정 후보 compact 근거 / merge 위상 / funnel.

    계약: 저장이 늘어날 뿐 **판정은 전혀 바뀌지 않는다**. 기사 본문/제목/URL 전문은
    후보마다 복제하지 않고 정수 카운트와 짧은 enum 만 남긴다.
    """

    def _run(self, backfill=None):
        """compact 필드를 켠 상태로 구동한다 — 배포 후 상태의 계약을 검증한다.
        (OFF 기본값 자체는 test_compact_fields_withheld_until_sql_applied 가 지킨다.)"""
        published = []
        with patch.dict(os.environ, {"NEWS_DIAG_COMPACT_FIELDS": "1"}, clear=False), \
             _BriefingHarness(self), \
             patch.object(main_module, "upsert_news_issues",
                          side_effect=lambda i, source="news_top": published.append(i) or True), \
             patch.object(main_module, "record_news_diagnostics", return_value=True) as rpc:
            main_module.run_news_briefing()
            payload, decisions = rpc.call_args[0]
        return payload, decisions, (published[0] if published else None)

    # -- (5) RANK_CUTOFF 의 pre-cut score/rank 보존 -------------------------

    def test_rank_cutoff_keeps_precut_rank_and_score(self):
        """컷 탈락 후보도 score 와 컷 이전 순위를 남긴다(과거엔 전부 NULL 이었다)."""
        # TOP_N 보다 후보를 많이 넣어 RANK_CUTOFF 를 강제한다.
        with patch.object(ranker, "select_top", side_effect=lambda r, top_n=2: r[:top_n]):
            payload, decisions, _ = self._run()
        cut = [d for d in decisions if d.get("reason_code") == "RANK_CUTOFF"]
        self.assertTrue(cut, "RANK_CUTOFF 후보가 생성되지 않아 계약을 검증할 수 없다")
        for d in cut:
            self.assertIsNotNone(d.get("score"), f"{d['keyword']}: score 가 비었다")
            self.assertIsNotNone(d.get("pre_cut_rank"), f"{d['keyword']}: pre_cut_rank 가 비었다")
            self.assertGreater(d["pre_cut_rank"], 2, "컷 탈락인데 pre_cut_rank 가 TOP_N 이내다")
            self.assertIsNotNone(d.get("article_count"))
            self.assertIsNotNone(d.get("unique_domain_count"))

    def test_precut_rank_is_dense_and_matches_ordering(self):
        """pre_cut_rank 는 kept 순서와 일치한다 — score 내림차순이어야 한다."""
        with patch.object(ranker, "select_top", side_effect=lambda r, top_n=2: r[:top_n]):
            _, decisions, _ = self._run()
        ranked_rows = [d for d in decisions if d.get("pre_cut_rank") is not None]
        ranked_rows.sort(key=lambda d: d["pre_cut_rank"])
        scores = [d["score"] for d in ranked_rows]
        self.assertEqual(scores, sorted(scores, reverse=True),
                         "pre_cut_rank 순서가 score 내림차순과 어긋난다")
        # selected 행은 finalize_selected 가 발행 rank 로 덮어써 pre_cut_rank 를 갖지 않는다.
        # 따라서 비선정 구간은 TOP_N 다음 순위부터 **구멍 없이** 이어져야 한다.
        got = [d["pre_cut_rank"] for d in ranked_rows]
        self.assertEqual(got, list(range(got[0], got[0] + len(got))),
                         "pre_cut_rank 에 구멍이 있다")
        selected_n = sum(1 for d in decisions if d["result_status"].startswith("selected"))
        self.assertEqual(got[0], selected_n + 1,
                         "컷 직후 순위가 selected 수 다음에서 시작하지 않는다")

    # -- (4) MERGED_INTO_OTHER winner 관계 + 위상 --------------------------

    def test_merged_row_keeps_winner_and_merge_mode(self):
        """흡수된 후보는 winner 와 merge 위상·근거 카운트를 함께 남긴다."""
        a = {"keyword": "코스피", "news_meta": _news_meta("코스피"), "sources": {"daum_home": 1}}
        b = {"keyword": "환율", "news_meta": _news_meta("환율"), "sources": {"nate_home": 2}}
        topo = ranker.merge_evidence_topology(a, b)
        self.assertIn(topo["mode"], {
            ranker.MERGE_MODE_NO_SHARED, ranker.MERGE_MODE_IDENTICAL,
            ranker.MERGE_MODE_SUBSET, ranker.MERGE_MODE_BOTH_RESIDUAL,
        })
        self.assertIsInstance(topo["shared_evidence_count"], int)
        self.assertIsInstance(topo["residual_support_a"], int)
        self.assertIsInstance(topo["residual_support_b"], int)

    def test_merge_topology_is_pure_observation(self):
        """위상 관찰이 입력 item 을 변형하지 않는다(판정 되먹임 금지)."""
        a = {"keyword": "코스피", "news_meta": _news_meta("코스피"), "sources": {"daum_home": 1}}
        b = {"keyword": "환율", "news_meta": _news_meta("환율"), "sources": {"nate_home": 2}}
        before = json.dumps([a, b], sort_keys=True, default=str)
        ranker.merge_evidence_topology(a, b)
        self.assertEqual(json.dumps([a, b], sort_keys=True, default=str), before)

    def test_merge_topology_matches_shared_evidence_split(self):
        """위상 enum 이 _split_shared_evidence 결과와 1:1 이다(규칙 복제가 아니다)."""
        a = {"keyword": "코스피", "news_meta": _news_meta("코스피")}
        b = {"keyword": "코스피", "news_meta": _news_meta("코스피")}   # 완전 동일 근거
        self.assertEqual(ranker.merge_evidence_topology(a, b)["mode"],
                         ranker.MERGE_MODE_IDENTICAL)
        c = {"keyword": "환율", "news_meta": _news_meta("환율")}       # 겹치는 URL 없음
        self.assertEqual(ranker.merge_evidence_topology(a, c)["mode"],
                         ranker.MERGE_MODE_NO_SHARED)

    # -- (3) pass1/pass2 funnel 저장 정확성 --------------------------------

    def test_funnel_counts_saved_and_monotonic(self):
        """funnel 은 단계별 후보 수를 담고, 단계가 진행될수록 늘지 않는다."""
        payload, decisions, _ = self._run()
        counts = payload["thresholds"]["selection_diagnostics_v1"]["counts"]
        for key in ("candidates", "ranked", "merged", "kept", "top"):
            self.assertIn(key, counts, f"funnel 에 {key} 가 없다")
        self.assertGreaterEqual(counts["candidates"], counts["ranked"])
        self.assertGreaterEqual(counts["ranked"], counts["merged"])
        self.assertGreaterEqual(counts["merged"], counts["kept"])
        self.assertGreaterEqual(counts["kept"], counts["top"])
        self.assertEqual(counts["top"], payload["selected_count"])

    def test_funnel_belongs_to_adopted_pass_only(self):
        """폐기된 pass 의 funnel 이 run 레벨로 승격되지 않는다."""
        run = diagnostics.RunDiagnostics()
        discarded = diagnostics.PassSnapshot("pass2")
        discarded.funnel = {"candidates": 999}
        adopted = diagnostics.PassSnapshot("pass1(strict)")
        adopted.funnel = {"candidates": 7}
        run.commit(adopted)
        self.assertEqual(run.funnel, {"candidates": 7})

    # -- (6)(7) selected 결과 동일 + classification invariant ---------------

    def test_selected_rows_unchanged_and_invariant_holds(self):
        """selected 판정/카운트 invariant 가 그대로다 — 관측 필드는 부가일 뿐이다."""
        payload, decisions, published = self._run()
        entries = published["keywords"]
        sel = [d for d in decisions if d["result_status"].startswith("selected")]
        self.assertEqual(len(sel), len(entries))
        self.assertEqual({d["keyword"] for d in sel}, {e["keyword"] for e in entries})
        self.assertEqual(
            payload["candidate_count"],
            payload["selected_count"] + payload["not_selected_count"]
            + payload["rule_excluded_count"],
        )

    # -- (2) diagnostic on/off 가 selection 결과에 영향 0 --------------------

    def test_selection_identical_with_and_without_diagnostics(self):
        """diag=None(진단 미수집)과 diag 수집본의 top 이 완전히 같다."""
        kws = list(_BriefingHarness.PASS1)
        cands, signals = _candidates(*kws), _signals(*kws)

        def snap(top):
            return [(t["keyword"], t.get("score"), t.get("display_keyword"),
                     sorted(t.get("related_keywords") or [])) for t in top]

        without = snap(main_module._rank_and_select(cands, signals, "p", diag=None))
        collected = diagnostics.PassSnapshot("p")
        with_diag = snap(main_module._rank_and_select(cands, signals, "p", diag=collected))
        self.assertEqual(without, with_diag)
        self.assertTrue(collected.decisions, "진단이 실제로 수집되지 않아 대조가 무의미하다")

    # -- (8)(9) backward compatibility --------------------------------------

    def test_missing_new_fields_do_not_break_payload(self):
        """새 필드가 없는 과거 형태 snapshot 도 payload 조립에 실패하지 않는다."""
        run = diagnostics.RunDiagnostics()
        snap = diagnostics.PassSnapshot("pass1(strict)")
        snap.record("옛후보", diagnostics.STATUS_NOT_SELECTED, "RANK_CUTOFF")
        run.commit(snap)
        payload, decisions = run.build_payload("ts:legacy")
        self.assertEqual(len(decisions), 1)
        # 새 필드는 값이 없으면 애초에 실리지 않는다(record 가 None 을 버린다).
        self.assertNotIn("pre_cut_rank", decisions[0])
        self.assertNotIn("merge_mode", decisions[0])
        self.assertEqual(payload["candidate_count"], 1)

    def test_evidence_summary_never_stores_article_text(self):
        """compact 요약에 title/description/url 전문이 실리지 않는다."""
        item = {"keyword": "코스피", "news_meta": _news_meta("코스피"),
                "sources": {"daum_home": 1, "naver_news_aux": True}}
        ev = diagnostics.evidence_summary(item)
        blob = json.dumps(ev, ensure_ascii=False)
        self.assertNotIn("http", blob, "URL 이 compact 요약에 실렸다")
        self.assertNotIn("본문 요약", blob, "기사 본문이 compact 요약에 실렸다")
        self.assertNotIn("속보", blob, "기사 제목이 compact 요약에 실렸다")
        self.assertTrue(all(isinstance(v, int) for v in ev.values()))

    def test_independent_family_count_excludes_naver_aux(self):
        """independent_family_count 는 source_count 와 다르다 — aux 를 세지 않는다."""
        item = {"keyword": "코스피", "news_meta": _news_meta("코스피"),
                "sources": {"daum_home": 1, "nate_home": 2, "naver_news_aux": True}}
        ev = diagnostics.evidence_summary(item)
        self.assertEqual(ev["independent_family_count"], 2)   # aux 는 제외
        self.assertEqual(len(item["sources"]), 3)             # source_count 는 3

    # -- (10) payload size ---------------------------------------------------

    def test_compact_payload_stays_small(self):
        """비선정 행의 compact 보강이 행당 수백 바이트 수준을 넘지 않는다."""
        # 컷을 낮춰 비선정 행을 반드시 만든다 — skip 으로 넘어가면 크기 계약이
        # 한 번도 측정되지 않은 채 통과한다(위양성).
        with patch.object(ranker, "select_top", side_effect=lambda r, top_n=2: r[:top_n]):
            _, decisions, _ = self._run()
        nonsel = [d for d in decisions if d["result_status"] == "not_selected"]
        self.assertTrue(nonsel, "비선정 후보가 없어 크기 계약을 측정할 수 없다")
        self.assertTrue(any("pre_cut_rank" in d for d in nonsel),
                        "compact 보강이 실제로 실리지 않아 측정이 무의미하다")
        new_fields = ("score", "pre_cut_rank", "article_count", "unique_url_count",
                      "unique_domain_count", "independent_family_count", "merge_mode",
                      "shared_evidence_count", "residual_support_winner",
                      "residual_support_self", "merge_reason")
        for d in nonsel:
            extra = {k: d[k] for k in new_fields if k in d}
            self.assertLessEqual(
                len(json.dumps(extra, ensure_ascii=False).encode()), 512,
                f"{d['keyword']}: compact 보강이 512B 를 넘었다",
            )

    def test_evidence_summary_survives_malformed_articles(self):
        """비정상 article 이 있어도 예외를 던지지 않는다.

        여기서 예외가 나면 _diag_record_pass 전체가 중단돼 그 run 의 decisions 가
        통째로 사라진다 — 부가 카운트 때문에 진단 본체를 잃으면 안 된다.
        """
        hostile = {
            "keyword": "x",
            "news_meta": {"articles": [
                {"url": None}, {}, {"url": 123}, "문자열이 섞임", {"url": ""},
                {"url": "https://ok.example.com/a?utm=1"},
            ]},
            "sources": {"daum_home": 1},
        }
        ev = diagnostics.evidence_summary(hostile)
        self.assertEqual(ev["unique_url_count"], 1)
        self.assertEqual(ev["unique_domain_count"], 1)
        self.assertEqual(ev["independent_family_count"], 1)
        # articles 자체가 리스트가 아니어도 살아남는다.
        self.assertEqual(
            diagnostics.evidence_summary({"news_meta": {"articles": "garbage"}})["article_count"], 0
        )

    def test_diag_record_survives_hostile_article_payload(self):
        """비정상 기사 payload 에서도 후보별 decision 이 전부 기록된다(유실 0)."""
        kws = list(_BriefingHarness.PASS1)
        cands, signals = _candidates(*kws), _signals(*kws)
        # 한 후보의 기사 URL 을 정수로 오염시킨다.
        signals["news"][kws[0]]["articles"][0]["url"] = 12345
        snap = diagnostics.PassSnapshot("p")
        main_module._rank_and_select(cands, signals, "p", diag=snap)
        self.assertEqual(len(snap.decisions), len(cands),
                         "오염된 기사 하나 때문에 decisions 가 유실됐다")
        self.assertFalse(snap.degraded, "관측 보강이 snapshot 을 degraded 로 만들었다")

    # -- 배포 순서 안전장치 -------------------------------------------------

    def test_compact_fields_withheld_until_sql_applied(self):
        """기본(OFF)에서는 새 키를 RPC 로 보내지 않는다.

        배포 SQL 이 컬럼/RPC 를 갱신하기 전에 새 키를 보내면 진단 적재가 통째로
        실패할 수 있다(단위 테스트로는 잡히지 않는 부류) — 코드 PR 은 OFF 로 배포한다.
        """
        run = diagnostics.RunDiagnostics()
        snap = diagnostics.PassSnapshot("pass1(strict)")
        snap.record("후보", diagnostics.STATUS_NOT_SELECTED, "RANK_CUTOFF",
                    score=0.5, pre_cut_rank=11, merge_mode="BOTH_RESIDUAL",
                    unique_domain_count=3, article_count=4)
        run.commit(snap)
        with patch.dict(os.environ, {"NEWS_DIAG_COMPACT_FIELDS": ""}, clear=False):
            _, decisions = run.build_payload("ts:off")
        d = decisions[0]
        for f in diagnostics.COMPACT_FIELDS:
            self.assertNotIn(f, d, f"OFF 인데 {f} 가 전송된다")
        # 기존 컬럼은 그대로 나간다 — 비선정 행에 값이 채워지는 것뿐이다.
        self.assertEqual(d.get("score"), 0.5)
        self.assertEqual(d.get("article_count"), 4)

    def test_compact_fields_sent_when_enabled(self):
        """SQL 적용 후 환경변수를 켜면 즉시 전송된다(수집은 항상 되고 있었다)."""
        run = diagnostics.RunDiagnostics()
        snap = diagnostics.PassSnapshot("pass1(strict)")
        snap.record("후보", diagnostics.STATUS_NOT_SELECTED, "RANK_CUTOFF",
                    score=0.5, pre_cut_rank=11, merge_mode="BOTH_RESIDUAL")
        run.commit(snap)
        with patch.dict(os.environ, {"NEWS_DIAG_COMPACT_FIELDS": "1"}, clear=False):
            _, decisions = run.build_payload("ts:on")
        self.assertEqual(decisions[0].get("pre_cut_rank"), 11)
        self.assertEqual(decisions[0].get("merge_mode"), "BOTH_RESIDUAL")

    def test_gate_does_not_mutate_snapshot(self):
        """OFF 필터링이 snapshot 원본을 훼손하지 않는다(켜면 그대로 나가야 한다)."""
        run = diagnostics.RunDiagnostics()
        snap = diagnostics.PassSnapshot("pass1(strict)")
        snap.record("후보", diagnostics.STATUS_NOT_SELECTED, "RANK_CUTOFF", pre_cut_rank=11)
        run.commit(snap)
        with patch.dict(os.environ, {"NEWS_DIAG_COMPACT_FIELDS": ""}, clear=False):
            run.build_payload("ts:off")
        with patch.dict(os.environ, {"NEWS_DIAG_COMPACT_FIELDS": "1"}, clear=False):
            _, decisions = run.build_payload("ts:on")
        self.assertEqual(decisions[0].get("pre_cut_rank"), 11,
                         "OFF 경로가 snapshot 을 파괴해 ON 에서도 값이 사라졌다")
