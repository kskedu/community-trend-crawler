"""
커뮤니티 트렌드 크롤러 진입점
GitHub Actions에서 주기적으로 실행됨
"""
import logging
import os
import sys
from datetime import datetime, timezone
from urllib.parse import quote_plus
from scrapers.clien import ClienScraper
from scrapers.ruliweb import RuliwebScraper
from scrapers.ppomppu import PpomppuScraper
from scrapers.mlbpark import MlbparkScraper
from scrapers.bobaedream import BobaedreamScraper
from scrapers.inven import InvenScraper
from scrapers.dcinside import DcinsideScraper
from scrapers.humoruniv import HumorunivScraper
from scrapers.cook82 import Cook82Scraper
from scrapers.fmkorea import FmkoreaScraper
from scrapers.theqoo import TheqooScraper
from scrapers.slrclub import SlrclubScraper
from scrapers.todayhumor import TodayhumorScraper
from scrapers.etoland import EtolandScraper
from scrapers.instiz import InstizScraper
from scrapers.ygosu import YgosuScraper
from scrapers.natepann import NatepannScraper
from keywords.danawa import DanawaKeywordScraper
from keywords.daum import DaumKeywordScraper
from keywords.namuwiki import NamuwikiKeywordScraper
from keywords.daangn import DaangnKeywordScraper
from keywords.nate import NateKeywordScraper
from keywords.msn import MsnKeywordScraper
from processor.dedup import dedup
from processor.filter import filter_notices
from processor.scorer import score_all
from db.supabase import (
    upsert_posts, upsert_keywords, upsert_news_issues, fetch_news_issues,
    record_news_diagnostics,
)
from news.movement import apply_movement
from news.thumbnail import enrich_issue_thumbnails
from news.seed import fetch_ranked_seed, fetch_ranked_seed_status
from news.naver_news import search_news
from news import candidates as cand
from news import datalab as datalab_adapter
from news import diagnostics
from news import google as google_adapter
from news import ranker
from news.builder import build_ranked_issues

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def _parse_generated_at(value):
    """issues['generated_at'] 문자열 → tz-aware datetime(UTC 기준). 실패 시 None.

    - 문자열이 아니면 None(비교 불가 → 상위에서 write 허용).
    - 'Z'는 '+00:00'으로 변환해 fromisoformat 파싱.
    - naive(timezone 없음)면 UTC로 간주(builder 는 항상 UTC ISO 를 넣지만 방어적).
    - ValueError/TypeError 등 파싱 실패는 None(상위에서 write 허용, fail-open).
    """
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_stale_news_top_write(previous, new_issues):
    """새 news_top payload 가 이미 저장된 previous 보다 '명확히 과거'인지 판정.

    True 를 반환할 때만 상위에서 upsert 를 skip 한다(오래된 실행이 최신을 덮는 것 방지).
    fail-open 원칙: 아래 애매/이상 케이스는 전부 False(=write 허용)로, 정상 신선 실행을
    막지 않는다.
      - previous 가 dict 아님 / generated_at 누락·비문자열·파싱실패 → False
      - new 의 generated_at 누락·비문자열·파싱실패 → False
      - 두 시각이 같거나 new 가 더 최신 → False
    오직 두 값이 모두 정상 파싱되고 new < previous 일 때만 True.

    ※ 한계: previous 는 실행 시작 시점(main.py read)의 스냅샷이라, read~write 사이
      다른 run 이 더 최신을 write 하면 이 가드는 그 값을 못 본다(순수 TOCTOU).
      완전 방어가 아니라 '이미 관측한 최신을 덮는 것'만 막는 best-effort 다.
    """
    if not isinstance(previous, dict) or not isinstance(new_issues, dict):
        return False
    prev_dt = _parse_generated_at(previous.get("generated_at"))
    new_dt = _parse_generated_at(new_issues.get("generated_at"))
    if prev_dt is None or new_dt is None:
        return False
    return new_dt < prev_dt

# 활성화된 스크래퍼 목록
SCRAPERS = [
    ClienScraper(),
    RuliwebScraper(),
    PpomppuScraper(),
    MlbparkScraper(),
    BobaedreamScraper(),
    InvenScraper(),
    DcinsideScraper(),
    HumorunivScraper(),
    Cook82Scraper(),
    # FmkoreaScraper(),  # 봇 차단 (430) - 비활성화
    # DdanziScraper(),  # 제거
    TheqooScraper(),
    SlrclubScraper(),
    TodayhumorScraper(),
    EtolandScraper(),
    InstizScraper(),
    YgosuScraper(),
    NatepannScraper(),
]

# 키워드 스크래퍼 (검색엔진 실시간 키워드 → keyword_cache)
KEYWORD_SCRAPERS = [
    DanawaKeywordScraper(),
    DaumKeywordScraper(),
    DaangnKeywordScraper(),
    NamuwikiKeywordScraper(),
    NateKeywordScraper(),
    MsnKeywordScraper(),
]


def run():
    all_posts = []
    source_status = {}  # site_id/source -> "ok" | "failed" | "skipped"

    for scraper in SCRAPERS:
        logger.info(f"[{scraper.site_id}] 크롤링 시작")
        try:
            posts = scraper.scrape()
            logger.info(f"[{scraper.site_id}] {len(posts)}건 수집")
            all_posts.extend(posts)
            # scrape()가 내부에서 예외를 삼키고 빈 리스트를 반환하는 경우(예: todayhumor
            # 403), scraper.last_status로 failed/skipped를 구분한다. BaseScraper 기반이
            # 아닌 scraper가 추가돼도 AttributeError 없이 failed로 방어.
            source_status[scraper.site_id] = "ok" if posts else getattr(scraper, "last_status", "failed")
        except Exception as e:
            logger.error(f"[{scraper.site_id}] 실패: {e}")
            source_status[scraper.site_id] = "failed"

    logger.info(f"총 수집: {len(all_posts)}건")

    # 중복 제거
    all_posts = dedup(all_posts)
    logger.info(f"중복 제거 후: {len(all_posts)}건")

    # 공지/안내 필터
    all_posts = filter_notices(all_posts)
    logger.info(f"공지 필터 후: {len(all_posts)}건")

    # 점수 계산
    all_posts = score_all(all_posts)

    # DB 저장
    saved = upsert_posts(all_posts)
    logger.info(f"저장 완료: {saved}건")

    # 검색엔진 키워드 수집(포털·서비스별 keyword_cache 갱신)
    _collect_keyword_caches(source_status)

    # source별 최종 상태 리포트 — optional/degraded 실패가 전체 실패처럼 보이지 않도록
    # active(ok/failed)와 skipped를 분리 표시. 판단 자체는 개별 except에서 이미 격리됨.
    ok = [s for s, v in source_status.items() if v == "ok"]
    failed = [s for s, v in source_status.items() if v == "failed"]
    skipped = [s for s, v in source_status.items() if v == "skipped"]
    logger.info(
        f"[source 상태] ok={len(ok)} failed={len(failed)} skipped={len(skipped)} "
        f"| failed={failed} skipped={skipped}"
    )

    # 실시간 이슈 브리핑 (P0-2) — 실패해도 위 커뮤니티/키워드 수집 결과에 영향 없도록 격리
    run_news_briefing()


def _collect_keyword_caches(source_status):
    """검색엔진 실시간 키워드(danawa/daum/daangn/nate/msn) 수집 → keyword_cache upsert.

    full(run())과 news_top_only(__main__ 분기) 양쪽에서 재사용한다. news_top_only는
    커뮤니티 스크래퍼를 생략하고 이 함수만 호출해 포털·서비스별 keyword_cache를
    news_top/news_issue_cache 생성 이전에 갱신한다(2026-07 실행 주기 개선).
    source_status(dict)는 caller가 최종 리포트용으로 전달 — news_top_only에서는
    빈 dict({})를 넘겨도 무방(리포트 미사용).
    """
    for ks in KEYWORD_SCRAPERS:
        if not ks.active:
            logger.info(f"[{ks.source}] 비활성(skipped) — upstream 없음, 크롤링 생략")
            source_status[ks.source] = "skipped"
            continue
        logger.info(f"[{ks.source}] 키워드 크롤링 시작")
        try:
            items = ks.scrape()
            if upsert_keywords(ks.source, items):
                logger.info(f"[{ks.source}] 키워드 {len(items)}개 저장")
                source_status[ks.source] = "ok"
            else:
                logger.warning(f"[{ks.source}] 키워드 저장 실패")
                source_status[ks.source] = "failed"
        except Exception as e:
            logger.error(f"[{ks.source}] 키워드 실패: {e}")
            source_status[ks.source] = "failed"


# 통합 랭킹 가드 임계 (docs/news-ranking-plan.md §10)
MIN_RECENT_KEYWORDS = 5  # Top10 중 최근 기사 보유 키워드 최소 수


def _count_recent_keywords(top):
    """Top 항목 중 최근(12h) 기사 보유 키워드 수(MIN_RECENT_KEYWORDS 가드용)."""
    return sum(1 for t in top if (t.get("news_meta") or {}).get("recent_count", 0) >= 1)


def _safe_diag(target, thunk):
    """진단 호출 전체(인자 계산 포함)를 격리한다. 어떤 예외도 랭킹으로 전파하지 않는다.

    thunk는 **무인자 callable**이어야 한다 — Python은 호출 전에 인자를 평가하므로
    `_safe_diag(t, snap.record, _norm_key(c["keyword"]))` 형태로는 인자 계산 예외가
    이 경계 밖에서 터진다(Codex plan review P1). 반드시 lambda로 감싼다.
    """
    if target is None:
        return
    try:
        thunk()
    except Exception as e:                # noqa: BLE001 — 순수 관찰이므로 전파 금지가 계약
        try:
            target.mark_degraded(e)
        except Exception:                 # noqa: BLE001 — 이것마저 터져도 랭킹은 보호한다
            logger.warning("[news-diag] mark_degraded 실패 — 진단 포기(랭킹 영향 없음)")


def _finalize_selected(run_diag, issues):
    """발행 payload로 selected 판정을 확정한다(thunk 안에서만 호출된다)."""
    snap = run_diag.final_snapshot
    if snap is not None:
        snap.finalize_selected((issues or {}).get("keywords") or [])


# selection_diagnostics_v1 underfill_reason 우선순위(위일수록 지배적). 제외 reason_code →
# 기계 판독 가능한 단일 underfill 사유. candidate 부족과 게이트 탈락을 구분한다(개선목표 A).
_UNDERFILL_REASON_PRIORITY = (
    ("NO_NEWS_EVIDENCE", "source_or_parse_failure"),
    ("PR_CLUSTER", "pr_excluded"),
    ("LOW_QUALITY_NEWS", "quality_gate"),          # cohesion 미달·B2 no_representative 포함
    ("STALE_ONLY", "stale_only"),
    ("HOROSCOPE_CONTENT", "horoscope"),
    ("INSUFFICIENT_DISPLAY_ARTICLES", "display_insufficient"),
    ("GENERIC_SINGLETON", "generic_singleton"),
    ("DISPLAY_GENERIC_ONLY", "generic_singleton"),
    ("DISPLAY_ARTICLE_INCONSISTENT", "display_inconsistent"),
    ("MERGED_INTO_OTHER", "merged"),
    ("RANK_CUTOFF", "rank_cutoff"),
)


def _record_selection_diagnostics(run_diag, issues, seed_status_map, candidate_count):
    """selection_diagnostics_v1 payload를 계산해 run_diag에 mark한다(순수 관찰).

    - counts: raw(수집 원시 후보), deduped(선정 후보 수), selected(발행 개수),
      eligible(제외 전 판정된 후보 수 근사). clusters는 run 단위로 단일값이 아니라 생략
      대신 keyword별 primary_cluster_size는 decisions에 이미 있음 → run 레벨엔 안 넣는다.
    - source_status: _collect_home_seeds가 넘긴 family별 fetch 상태.
    - rejection_counts: 채택 snapshot decisions의 not_selected reason_code별 집계.
    - underfill_reason: selected < TOP_N 일 때만, 지배적 제외 사유(우선순위) 1개. 충족 시 'none'.
    """
    snap = run_diag.final_snapshot
    decisions = snap.payload_decisions() if snap else []
    selected = sum(1 for d in decisions if d.get("result_status") in diagnostics._SELECTED_STATUSES)

    rejection_counts = {}
    for d in decisions:
        if d.get("result_status") == diagnostics.STATUS_NOT_SELECTED:
            rc = d.get("reason_code") or "UNKNOWN"
            rejection_counts[rc] = rejection_counts.get(rc, 0) + 1
    # B2(no_representative) 제외는 decisions에서 LOW_QUALITY_NEWS로 기록돼 cohesion 탈락과
    # 섞인다. selection_diagnostics에는 별도 세부 키로 분리 노출한다(Codex 최종리뷰 P3).
    no_rep_count = getattr(run_diag, "no_representative_excluded_count", 0)
    if no_rep_count:
        rejection_counts["no_representative"] = no_rep_count

    published = len((issues or {}).get("keywords") or [])
    if published >= ranker.TOP_N:
        underfill_reason = "none"
    else:
        underfill_reason = "candidate_shortage"  # 제외가 전혀 없으면 애초 후보 부족.
        for code, reason in _UNDERFILL_REASON_PRIORITY:
            if rejection_counts.get(code):
                underfill_reason = reason
                break

    counts = {
        "raw": run_diag.collected_candidate_count,
        "deduped": candidate_count,
        "eligible": selected + rejection_counts.get("RANK_CUTOFF", 0),
        "selected": published,
    }
    run_diag.mark_selection_diagnostics(
        underfill_reason=underfill_reason,
        counts=counts,
        source_status=dict(seed_status_map or {}),
        rejection_counts=rejection_counts,
    )


def _new_snapshot(run_diag, pass_name):
    """PassSnapshot 생성 실패는 run 전역 degraded다 — 빈 성공 이력을 남기지 않는다.

    None으로 계속 진행하면 랭킹은 정상이지만 진단이 candidate_count=0인 'success' 행을
    저장해, 후보가 없었던 실행과 구분되지 않는다(Codex diff review P1). 랭킹은 그대로
    진행하되 진단은 저장하지 않는 쪽을 택한다.
    """
    try:
        return diagnostics.PassSnapshot(pass_name)
    except Exception as e:                # noqa: BLE001
        logger.warning("[news-diag] snapshot 생성 실패(랭킹 영향 없음): %s", type(e).__name__)
        _safe_diag(run_diag, lambda: run_diag.mark_degraded(e))
        return None


def _finalize_diagnostics(run_diag):
    """진단 이력을 RPC 1회로 저장한다. 랭킹/news_top 결과에 절대 영향을 주지 않는다.

    - 채택 snapshot 또는 run 전역이 degraded면 저장하지 않는다(부분 이력 금지).
    - payload 조립 오류는 run 전역 degraded로 처리한다(§3-1 계약 5).
    - 예외 메시지는 남기지 않는다 — 타입명만(§10-1).
    """
    if run_diag is None:
        return
    try:
        if run_diag.is_degraded():
            errs = list(run_diag.errors)
            snap = run_diag.final_snapshot
            if snap is not None:
                errs += list(snap.errors)
            logger.warning(
                "[news-diag] degraded — 진단 저장 생략 (오류 %d건, 최초 유형=%s)",
                len(errs), errs[0] if errs else "unknown",
            )
            return
        try:
            run, decisions = run_diag.build_payload(
                diagnostics.build_run_key(),
                git_sha=diagnostics.resolve_git_sha(),
                rules_version=diagnostics.RULES_VERSION,
            )
        except Exception as e:            # noqa: BLE001
            # payload 조립 오류 = run 전역 degraded(§3-1 계약 5) → 저장하지 않는다.
            run_diag.mark_degraded(e)
            logger.warning(
                "[news-diag] payload 조립 실패 — 진단 저장 생략: %s", type(e).__name__
            )
            return
        if record_news_diagnostics(run, decisions):
            logger.info(
                "[news-diag] 진단 저장 완료 (status=%s, 후보 %d, decisions %d)",
                run.get("status"), run.get("candidate_count", 0), len(decisions),
            )
    except Exception as e:                # noqa: BLE001 — 진단 저장 실패는 랭킹과 무관
        logger.warning("[news-diag] 진단 저장 실패(랭킹 영향 없음): %s", type(e).__name__)


_GATE_REASON_CODES = {
    "horoscope_content": "HOROSCOPE_CONTENT",
    "low_quality_news": "LOW_QUALITY_NEWS",
    "stale_only": "STALE_ONLY",
    # crime-attribution safety(G, 2026-07-21) — 이름+범죄어 오귀속 fail-closed drop.
    # ⚠️ 선행조건: StartHub news_keyword_decisions.reason_code CHECK 에
    # 'UNSAFE_CRIME_ATTRIBUTION' 이 등록돼 있어야 한다(migration
    # supabase-news-diag-reason-crime-*.sql). 이 CHECK 없이 이 코드를 emit 하면 진단
    # INSERT 가 CHECK 위반으로 조용히 실패한다(STALE_WRITE_SKIPPED 선례). 그래서
    # migration 을 먼저 운영 적용한 뒤 이 크롤러 변경을 merge 하는 순서 게이트를 지킨다.
    # LOW_QUALITY_NEWS 로 왜곡 emit 하지 않는다 — 실제 drop 사유를 정확히 남긴다.
    "unsafe_crime_attribution": "UNSAFE_CRIME_ATTRIBUTION",
}


def _diag_gate_reason_code(keyword, signals):
    """compute_scores 내부 continue의 실제 사유를 판정 함수 그대로 호출해 얻는다.

    차집합만으로는 quality gate(HOROSCOPE/LOW_QUALITY/STALE)와 NO_NEWS_EVIDENCE를 구분할 수
    없다. 진단이 판정과 어긋나지 않도록 로직을 복제하지 않고 _quality_gate_reason을 재사용한다
    (순수 함수 — 부작용 없음).
    """
    news_map = signals.get("news") or {}
    nm = news_map.get(keyword)
    if nm is None:
        return "NO_NEWS_EVIDENCE"
    reason = ranker._quality_gate_reason(keyword, nm)
    if reason:
        return _GATE_REASON_CODES.get(reason, "LOW_QUALITY_NEWS")
    return "NO_NEWS_EVIDENCE"


def _diag_record_pass(diag, candidates, signals, ranked, pr_excluded, merged,
                      generic_excluded, selected_pre_display, top, display_excluded,
                      no_rep_excluded=None):
    """pass의 최종 판정을 후보별 1건씩 기록한다(순수 관찰).

    _rank_and_select의 단계별 지역변수만으로 재구성하므로 ranker 시그니처를 바꾸지 않는다.
    식별자는 _norm_key(candidates._merge의 pool 키와 동일) — display_keyword나 객체
    identity는 merge 후 변형/복사되어 식별자로 쓸 수 없다.

    모든 후보는 정확히 1개의 decision을 갖는다:
      candidate_count = selected + not_selected + rule_excluded
    """
    nk = diagnostics._norm_key
    top_by_key = {nk(t["keyword"]): t for t in top}
    ranked_keys = {nk(r["keyword"]) for r in ranked}
    pr_keys = {nk(k) for k in pr_excluded}
    generic_keys = {nk(k) for k in generic_excluded}
    display_keys = {nk(k) for k in display_excluded}
    no_rep_keys = {nk(k) for k in (no_rep_excluded or [])}
    # B2 제외 수를 snapshot에 기록(진단에서 cohesion 탈락과 분리, Codex 최종리뷰 P3).
    diag.no_representative_excluded_count = len(no_rep_keys)
    pre_display_keys = {nk(t["keyword"]) for t in selected_pre_display}
    merged_keys = {nk(m["keyword"]) for m in merged}
    # merge로 흡수된 후보 → canonical 대표. related_keywords가 원본 keyword를 보존한다.
    absorbed = {}
    for m in merged:
        for rel in (m.get("related_keywords") or []):
            absorbed[nk(rel)] = m["keyword"]

    for c in candidates:
        kw = c["keyword"]
        key = nk(kw)
        item = top_by_key.get(key)
        if item is not None:
            # 이 시점에는 rank/summary/representative/display_articles가 아직 없다 —
            # builder가 나중에 만든다. selected 행의 실제 값은 발행 직전
            # RunDiagnostics.finalize_selected()가 issues["keywords"]로 확정한다.
            diag.record(
                kw, diagnostics.STATUS_SELECTED, "SELECTED",
                display_keyword=item.get("display_keyword"),
                score=item.get("score"),
                merge_reason=item.get("merge_reason"),
                related_keywords=item.get("related_keywords"),
                source_count=len(c.get("sources") or {}),
            )
            continue

        if key in display_keys:
            status, reason = diagnostics.STATUS_NOT_SELECTED, "INSUFFICIENT_DISPLAY_ARTICLES"
        elif key in no_rep_keys:
            # B2 제외(정제 후 대표 사건 없음). 배포 read RPC canonical 13개에 전용 코드가
            # 없어(REPRESENTATIVE_MISSING 미존재 → Admin에서 'unknown'으로 빠짐) 의미상
            # 가장 가까운 canonical LOW_QUALITY_NEWS('제외' 카테고리)로 기록한다. 세부
            # 구분은 selection_diagnostics_v1.rejection_counts.no_representative에 별도 집계.
            status, reason = diagnostics.STATUS_NOT_SELECTED, "LOW_QUALITY_NEWS"
        elif key in pre_display_keys:
            # select_top을 통과했지만 display 제외도 아닌데 top에 없다 = 이론상 도달 불가.
            status, reason = diagnostics.STATUS_NOT_SELECTED, "RANK_CUTOFF"
        elif key in generic_keys:
            status, reason = diagnostics.STATUS_NOT_SELECTED, "GENERIC_SINGLETON"
        elif key in absorbed:
            status, reason = diagnostics.STATUS_NOT_SELECTED, "MERGED_INTO_OTHER"
        elif key in merged_keys:
            # merge 후 생존했으나 select_top에서 탈락 = 순위 컷.
            status, reason = diagnostics.STATUS_NOT_SELECTED, "RANK_CUTOFF"
        elif key in pr_keys:
            status, reason = diagnostics.STATUS_NOT_SELECTED, "PR_CLUSTER"
        elif key in ranked_keys:
            # gate/PR/merge 어디에도 없는데 사라짐 → display 정합성 invariant 단계에서 탈락.
            # 이 단계는 제외 목록을 반환하지 않으므로(반환 계약 불변 유지) 두 reject 분기를
            # 실제 판정 함수로 되짚어 구분한다 — 안 그러면 DISPLAY_GENERIC_ONLY가 영구
            # 도달 불가 코드가 된다(Codex diff review P1).
            status = diagnostics.STATUS_NOT_SELECTED
            reason = ("DISPLAY_GENERIC_ONLY" if ranker._is_generic_only_display(kw)
                      else "DISPLAY_ARTICLE_INCONSISTENT")
        else:
            # compute_scores 내부 continue — 실제 판정 함수로 사유를 확정한다(추측 금지).
            status = diagnostics.STATUS_NOT_SELECTED
            reason = _diag_gate_reason_code(kw, signals)

        diag.record(
            kw, status, reason,
            source_count=len(c.get("sources") or {}),
            canonical_keyword=absorbed.get(key),
        )


def _rank_and_select(candidates, signals, pass_name, diag=None):
    """score → dedupe/merge → generic singleton 제외 → Top10 + pass별 단계 카운트 로그.

    final이 TOP_N 미만일 때 부족 사유(어느 단계에서 몇 개가 줄었는지)를 재구성할 수
    있도록 단계별 수를 항상 남긴다(품질 기준 완화 없이 개수만 관찰).

    diag: PassSnapshot(호출자가 생성해 명시 전달). None이면 진단 미수집 — 랭킹 동작 동일.
    """
    ranked = ranker.compute_scores(candidates, signals)
    gate_passed = len(ranked)
    # PR/광고성 클러스터 hard exclude — merge 이전 per-keyword 기준(문제 B).
    ranked, pr_excluded = ranker.exclude_pr_clusters(ranked)
    if pr_excluded:
        logger.warning("[news] %s: PR/광고성 클러스터 제외 %s", pass_name, pr_excluded)
    merged = ranker.dedupe_and_merge(ranked)
    # singleton sense-mixing display 보정("위홀 뜻" 사례) — merge 후, invariant 검증 전.
    merged = ranker.resolve_singleton_displays(merged)
    # display_keyword/articles 정합성 invariant — merge 후, generic guard 전(문제 A).
    merged = ranker.enforce_display_article_consistency(merged)
    kept, generic_excluded = ranker.exclude_generic_singletons(merged)
    if generic_excluded:
        logger.warning("[news] %s: generic singleton 제외 %s", pass_name, generic_excluded)
    # display 부족 / no_representative 제외를 select_top *이전* 전체 리스트에 적용한다
    # (2026-07 변경, Codex 계획리뷰 P1-4). 이전엔 select_top 이후라 Top10 통과분이 빠지면
    # 하위 backfill 없이 개수가 줄었다. 이제 제외 후 select_top이 슬라이스만 하므로 하위
    # 정상후보가 자연히 그 자리를 채운다. 순서: generic → display부족 → no_rep → select_top.
    kept, display_excluded = ranker.exclude_insufficient_display_articles(kept)
    if display_excluded:
        logger.warning("[news] %s: display_articles 부족 제외 %s", pass_name, display_excluded)
    kept, no_rep_excluded = ranker.exclude_no_representative(kept)
    if no_rep_excluded:
        logger.warning("[news] %s: no_representative 제외 %s", pass_name, no_rep_excluded)
    # selected_pre_display = 모든 제외 완료 후 리스트(진단 재구성 호환용 이름 유지).
    selected_pre_display = kept
    top = ranker.select_top(kept)
    logger.info(
        "[news] %s: candidates=%d gate통과=%d PR제외=%d merge후=%d generic제외=%d "
        "display부족제외=%d no_rep제외=%d final=%d",
        pass_name, len(candidates), gate_passed, len(pr_excluded), len(merged),
        len(generic_excluded), len(display_excluded), len(no_rep_excluded), len(top),
    )
    # source family diversity 관찰 로깅(2026-07, 순수 관찰 — ranking 결과에 영향 없음).
    # 각 단계에서 어느 source family가 후보를 만들고 어느 단계에서 사라지는지 추적한다.
    # category 분류기는 이번 범위에 없어 "미분류"로 고정한다(사전/룰셋 신규 추가 없음).
    _log_source_family_diversity(
        pass_name, candidates, ranked, merged, selected_pre_display, top,
    )
    # broad category generic singleton 탐지(2026-07-09, 1차: logging first — 순수 관찰).
    # final(top)에 진입한 "건설"/"게임"류 순수 한글 업종/분야어 단독 후보를 shadow로
    # 탐지해 로그만 남긴다. 제외/강등/순위 변경 없음 — 반환값 top은 그대로 유지한다.
    # 운영 1~2회 로그로 dispersion 판정을 검증한 뒤 제외/강등 기준을 별도 PR에서 확정한다.
    broad_diags = ranker.detect_broad_category_singletons(top)
    if broad_diags:
        logger.warning(
            "[news] %s: broad category singleton 관찰(제외 아님) %s", pass_name, broad_diags
        )
    # 단일 토큰 keyword 동음이의 sense 탐지(issue #2 후속, 1차: logging first — 순수 관찰).
    # "워홀"처럼 동일 문자열 토큰을 공유하는 다른 의미 클러스터(앤디 워홀 전시)의 표시
    # 기사 혼입 가능성을 shadow로 탐지해 로그만 남긴다. 제외/강등/순위 변경 없음 —
    # 반환값 top은 그대로 유지한다. 운영 로그로 오탐률/would_* 수치를 관찰한 뒤
    # _display_anchor_allowed 단일 토큰 예외 자격 조건 소비를 별도 PR에서 판단한다.
    homonym_diags = ranker.detect_homonym_entity_singletons(top)
    if homonym_diags:
        logger.warning(
            "[news] %s: homonym entity sense 관찰(제외 아님) %s", pass_name, homonym_diags
        )
    # 진단 기록(순수 관찰) — 인자 계산까지 thunk 안에서 일어나야 격리된다.
    _safe_diag(diag, lambda: _diag_record_pass(
        diag, candidates, signals, ranked, pr_excluded, merged,
        generic_excluded, selected_pre_display, top, display_excluded,
        no_rep_excluded,
    ))
    return top


def _log_source_family_diversity(pass_name, candidates, gate_ranked, merged,
                                 selected_pre_display, final_after_display):
    """_rank_and_select 각 단계의 source family 분포를 로깅한다(관찰 전용).

    단계: initial(candidates) → gate통과+PR제외후(gate_ranked) → merge후(merged)
          → selected_pre_display(select_top 직후) → final_after_display(display 제외 이후).
    gate_ranked는 compute_scores(quality/fresh/relevance gate) + exclude_pr_clusters를
    통과한 뒤의 리스트라, gate/PR 두 관문 통과분을 함께 관측한다.
    category는 분류기 부재로 "미분류(unknown)"만 남긴다(이번 범위 정책).
    """
    stages = (
        ("initial", candidates),
        ("gate+pr", gate_ranked),
        ("merged", merged),
        ("selected_pre_display", selected_pre_display),
        ("final_after_display", final_after_display),
    )
    for stage_name, items in stages:
        dist = cand.source_family_distribution(items)
        logger.info(
            "[news] %s source_family[%s]: %s (category=미분류)",
            pass_name, stage_name, dict(sorted(dist.items())),
        )


# 홈/트렌드 seed 확장 단계 상한: pass1은 홈 Top10, pass2(backfill)는 홈 Top20.
# google_trends는 두 pass 모두 provider 상한(최대 20) 그대로 사용한다.
HOME_PASS1_TOP = 10
HOME_PASS2_TOP = 20

# (source, family, 로그용 short) 매핑. bing_home은 keyword_cache source='msn'(bing 검색 연결).
_HOME_SEED_SPEC = (
    ("daum", "daum_home", "daum"),
    ("nate", "nate_home", "nate"),
    ("msn", "bing_home", "bing"),
)


def _collect_home_seeds():
    """홈 seed(daum/nate/bing)를 keyword_cache에서 read-only 조회.

    반환: (fulls, status_map).
    - fulls: {family: ranked(list, 최대 20)}. fresh 하지 않거나 조회 실패한 family는 제외.
    - status_map: {family: 'ok'|'stale'|'empty'|'fetch_failed'} — source_status 진단(H)용.
      fetch 실패와 empty/stale을 구분한다(Codex 계획리뷰 P1-6).
    """
    fulls = {}
    status_map = {}
    for source, family, short in _HOME_SEED_SPEC:
        ranked, fresh, status = fetch_ranked_seed_status(source)
        status_map[family] = status
        if not ranked:
            logger.warning("[news] %s seed 없음/조회 실패 → drop(%s)", family, status)
            continue
        if not fresh:
            logger.warning("[news] %s seed stale → drop(stale_only)", family)
            continue
        fulls[family] = ranked
    return fulls, status_map


def _seed_sources_from(home_fulls, google_cands, home_top):
    """home_fulls(family→최대20) 를 home_top으로 절단 + google_trends(상한 그대로) 결합."""
    seed_sources = {fam: ranked[:home_top] for fam, ranked in home_fulls.items()}
    if google_cands:
        seed_sources["google_trends"] = google_cands  # provider 상한(최대 20) 유지
    return seed_sources


def _google_related_terms(google_cands):
    """google_trends 후보의 related_terms(있으면)를 pass2 phrase 후보로 수집."""
    terms = []
    for c in google_cands or []:
        for t in c.get("related_terms") or []:
            if isinstance(t, str) and t.strip():
                terms.append(t.strip())
    return list(dict.fromkeys(terms))


def _cache_google_keywords(google_cands):
    """Google Trends 원천 후보를 keyword_cache(source='google_trends')에 저장.

    StartHub "출처별 보기" 팝업이 keyword_cache를 직접 읽어 source별 Top10을 노출하는데,
    google_trends는 news_top pipeline seed로만 쓰이고 keyword_cache엔 없어 팝업에서 빠졌다.
    이미 fetch한 google_cands를 다른 source(daum/nate 등)와 동일한 {keyword, url} 형태로
    upsert한다. url은 keyword_cache 공통 규약(검색 결과 URL)에 맞춰 Google 검색 URL을 생성.

    - 랭킹 로직과 무관(seed_sources 전달은 그대로 유지). 저장 실패는 news_top에 영향 없게 격리.
    - google 비활성/실패 시 google_cands=[] → upsert 안 함(빈 값으로 last-good 덮지 않음).
    """
    if not google_cands:
        return
    items = []
    for c in google_cands:
        kw = (c.get("keyword") or "").strip()
        if not kw:
            continue
        items.append({
            "keyword": kw,
            "url": "https://www.google.com/search?q=" + quote_plus(kw),
        })
    if not items:
        return
    try:
        if upsert_keywords("google_trends", items):
            logger.info("[news] google_trends 키워드 %d개 keyword_cache 저장", len(items))
        else:
            logger.warning("[news] google_trends keyword_cache 저장 실패")
    except Exception as e:
        logger.warning("[news] google_trends keyword_cache 저장 예외(무시): %s", e)


def _backfill_pass(
    pass1_top, pass1_aux, home_fulls, google_cands, daum_full,
    cached_search_news, news_signals, datalab_signals, google_signals,
    diag=None,
):
    """pass2(backfill): 후보 발굴 확장 후 동일 gate/merge로 전체 재계산.

    - 신규 후보 3경로: 홈 seed Top20 확장 + aux 확장(daum_home 전체 기반, 상한 12)
      + 뉴스 title 기반 phrase 후보 + Google related terms(있으면).
    - 신규 후보만 뉴스 실호출(cached_search_news 메모이즈), datalab/google 신호는
      pass1 것을 재사용(추가 API 호출은 신규 phrase 키워드 캐시 미스 시에만).
    - 증분 방식은 min-max 집합 정규화와 충돌하므로 전체 재계산을 채택한다.
    - 안전장치: 재계산 결과가 pass1보다 못하면 채택하지 않고 pass1 결과를 유지(rollback).

    phrase 원천 확장(2026-07, Codex 계획 리뷰 반영): 기존엔 phrase를 pass1 news_signals
    에서만 뽑아 pass1 후보 집합 바깥의 이슈를 놓쳤다. pass2에서 새로 발굴된 aux2/
    Google related terms 키워드의 기사까지 phrase 원천으로 쓰기 위해, 아래 5단계로
    순환 의존(phrase→candidates2→news_signals2)을 끊는다:
      1) phrase 없이 pre-candidates(seed Top20 + aux2 + related_terms) 조립
      2) pre-candidates로 pre-signals fetch(cached_search_news 경유, 대부분 캐시 히트)
      3) pass1 news_signals ∪ pre-signals 를 원천으로 phrase 재추출
      4) phrase까지 합쳐 최종 candidates2 재조립
      5) 최종 news_signals2 fetch 후 rank/select
    diversity/improved/rollback guard는 전부 최종 candidates2 조립 이후에 둔다(위치 불변).

    반환: (top2, candidates2). 채택하지 않으면 (None, None).

    diag: pass2 PassSnapshot(호출자 생성·전달). 여기선 채택 여부를 모르므로 commit하지 않고,
          finally에서 seal만 한다 — 채택 시 호출자가 final_snapshot으로 지정한다.
    """
    try:
        aux_expanded = cand.derive_aux_keywords(
            daum_full, cached_search_news,
            top=cand.AUX_SEED_TOP_BACKFILL, aux_max=cand.AUX_MAX_BACKFILL,
        )
        # pass1 aux는 top=5 기준이라 top=10 확장 결과의 subset이 아니다 → union으로 보존.
        aux2 = list(dict.fromkeys((pass1_aux or []) + aux_expanded))
        related_terms = _google_related_terms(google_cands)

        # 홈 seed Top20 확장분(pass1 Top10 대비 늘어난 후보)이 있는지도 새 후보로 본다.
        seed_sources2 = _seed_sources_from(home_fulls, google_cands, HOME_PASS2_TOP)
        seed_sources1 = _seed_sources_from(home_fulls, google_cands, HOME_PASS1_TOP)
        home_expanded = any(
            len(seed_sources2.get(f, [])) > len(seed_sources1.get(f, []))
            for f in seed_sources2
        )

        # --- (1) phrase 없이 pre-candidates 조립: seed Top20 + aux2 + related_terms.
        #     related_terms는 pass1 후보 집합엔 없던 신규 키워드일 수 있어 여기서 seed의
        #     aux(비독립 family)로 합류시켜 pre-signals 원천을 pass1 바깥까지 넓힌다.
        aux2_pre = list(dict.fromkeys(aux2 + related_terms))
        candidates2_pre = cand.collect_candidates(
            seed_sources2, aux2_pre, limit=cand.BACKFILL_CANDIDATE_MAX,
        )

        # --- (2) pre-candidates로 pre-signals fetch(캐시 경유 — 대부분 pass1에서 이미 fetch됨).
        news_signals2_pre = cand.build_news_signals(candidates2_pre, cached_search_news)

        # --- (3) phrase 재추출: pass1 news_signals ∪ pre-signals 를 원천으로.
        #     pass1 생존 이슈(canonical + alias)와 유사한 phrase는 재발굴하지 않는다.
        survived = []
        for t in pass1_top:
            survived.append(t["keyword"])
            survived.extend(t.get("related_keywords") or [])
        phrase_source_signals = dict(news_signals or {})
        phrase_source_signals.update(news_signals2_pre or {})
        phrase_source_article_count = sum(
            len(sig.get("articles") or []) for sig in phrase_source_signals.values()
        )
        phrases = cand.derive_phrase_candidates(phrase_source_signals, survived)
        # related_terms는 phrase 후보로도 직접 합류(기존 동작 유지).
        phrases = list(dict.fromkeys(phrases + related_terms))
        logger.info(
            "[news] pass2 phrase: source_articles=%d raw_candidates=%d",
            phrase_source_article_count, len(phrases),
        )

        if not aux_expanded and not phrases and not home_expanded:
            logger.info("[news] pass2: 신규 후보 없음 → pass1 결과 유지")
            return None, None

        # --- (4) 최종 candidates2 조립: seed Top20 + aux2 + phrase.
        #     phrase_reserve: seed가 상한(BACKFILL_CANDIDATE_MAX)을 가득 채워도 순수 phrase
        #     후보가 통째로 잘리지 않도록 최소 예약분을 둔다(Codex diff 리뷰 P1). 4안의
        #     phrase 원천 확장 효과가 seed 포화 상황에서 무력화되는 것을 막는다.
        candidates2 = cand.collect_candidates(
            seed_sources2, aux2,
            phrase_keywords=phrases, limit=cand.BACKFILL_CANDIDATE_MAX,
            phrase_reserve=cand.PHRASE_RESERVE_BACKFILL,
        )
        # 다양성 guard 재적용(최종 candidates2 기준 — pre가 아니라 최종에서 판정).
        families = cand.count_source_families(candidates2)
        if families < cand.MIN_SOURCE_FAMILIES:
            logger.warning("[news] pass2: source_diversity_failed(%d) → pass1 결과 유지", families)
            return None, None

        # --- (5) 최종 news_signals2 fetch(신규 phrase 키워드는 캐시 미스 시 여기서 검색 호출).
        news_signals2 = cand.build_news_signals(candidates2, cached_search_news)
        if not news_signals2:
            return None, None
        signals2 = {
            "news": news_signals2,
            "datalab": datalab_signals,
            "google": google_signals,
        }
        top2 = _rank_and_select(candidates2, signals2, "pass2(backfill)", diag=diag)
        # phrase 후보가 최종 Top에 몇 개 생존했는지(diversity 관찰용) — canonical sources에
        # naver_news_phrase 키가 있는 항목 수.
        # 한계(Codex diff 리뷰 P2, 관측 로그 전용이라 기록만): phrase 후보가 non-phrase
        # canonical에 same-issue merge로 흡수되면 canonical의 sources만 실려(§7-3)
        # related_keywords로만 남으므로 이 카운트에서 빠진다 → 실제 phrase 기여의 하한이다.
        # ranking 결과에는 영향 없고, 과소집계는 "phrase 효과를 낮게 보는" 안전한 방향이라
        # 관측 목적(원천 확장이 실제로 독립 그룹을 늘리는가)에는 하한만으로도 충분하다.
        phrase_final_survivors = sum(
            1 for t in top2 if (t.get("sources") or {}).get("naver_news_phrase")
        )
        logger.info(
            "[news] pass2 phrase: selected=%d final_survivors=%d",
            len(phrases), phrase_final_survivors,
        )
        improved = len(top2) > len(pass1_top) or (
            len(top2) == len(pass1_top)
            and _count_recent_keywords(top2) > _count_recent_keywords(pass1_top)
        )
        if not improved:
            logger.info(
                "[news] pass2: 개선 없음(final %d→%d) → pass1 결과 유지",
                len(pass1_top), len(top2),
            )
            return None, None
        return top2, candidates2
    except Exception as e:
        logger.warning("[news] pass2 backfill 실패(무시하고 pass1 결과 유지): %s", e)
        return None, None
    finally:
        # seal — 추가 기록만 막고 수집분은 보존한다. 폐기든 채택이든 수명을 여기서 확정하고,
        # 채택 시 호출자가 이 객체를 final_snapshot으로 지정한다(소유권은 호출자).
        if diag is not None:
            _safe_diag(diag, diag.close)


def run_news_briefing(run_type="full"):
    """통합 랭킹으로 news_issue_cache(source='news_top') 갱신.

    흐름: 홈/트렌드 seed(google_trends/daum_home/nate_home/bing_home) + 보조후보 수집 →
          News/DataLab/Google 신호 → ranker 4축 score → Top10 → build → upsert.
    seed 순서를 그대로 쓰지 않고 자체 score로 재정렬한다. 모든 후보는 Naver News
    quality/fresh gate 통과 필수.

    2-pass backfill(품질 기준 유지형 최소 10개 확보):
    - pass1(strict): google_trends Top20 + daum/nate/bing Top10.
    - pass1 final이 TOP_N 미만이거나 최근성 가드에 미달하면 pass2(backfill):
      홈 seed Top20 + aux 확장 + phrase + Google related terms로 동일 gate/merge 재계산.
      개선 없으면 pass1로 rollback.
    - 그래도 부족하면 품질 기준을 낮추지 않고 부족 사유 로그만 남긴 채 진행(filler 미삽입).

    upsert skip 가드(last good snapshot 유지):
    - 후보 없음 → skip
    - source_diversity_failed(독립 family < MIN_SOURCE_FAMILIES) → skip
    - no_news_evidence(News 신호 전무) → skip
    - recent_guard_failed(Top10 중 최근 기사 보유 키워드 < MIN_RECENT_KEYWORDS) → skip
    실패해도 커뮤니티/키워드 수집 결과엔 영향 없도록 격리.
    """
    # 진단 수집기(순수 관찰). 어떤 오류도 아래 랭킹 흐름에 영향을 주지 않는다.
    run_diag = None
    try:
        run_diag = diagnostics.RunDiagnostics(run_type=run_type)
    except Exception as de:               # noqa: BLE001 — 수집기 자체가 없어도 랭킹은 돈다
        logger.warning("[news-diag] 수집기 초기화 실패(랭킹 영향 없음): %s", type(de).__name__)

    try:
        # 1) 홈/트렌드 seed 수집
        home_fulls, seed_status_map = _collect_home_seeds()  # ({family: ranked(≤20)}, status)
        google_cands = google_adapter.fetch_candidates()  # 비활성/실패 시 [] (내부 로그)
        # google_trends 원천 Top을 출처별 보기(keyword_cache)에도 노출 — 랭킹과 무관, 저장만.
        _cache_google_keywords(google_cands)
        daum_full = home_fulls.get("daum_home", [])  # aux 추출 원천

        # 뉴스 fetch 메모이즈 — pass2 backfill에서 같은 키워드 재호출 방지(쿼터 보호).
        news_fetch_cache = {}

        def cached_search_news(keyword):
            if keyword not in news_fetch_cache:
                news_fetch_cache[keyword] = search_news(keyword)
            return news_fetch_cache[keyword]

        aux = cand.derive_aux_keywords(daum_full, cached_search_news)
        seed_sources = _seed_sources_from(home_fulls, google_cands, HOME_PASS1_TOP)
        candidates = cand.collect_candidates(seed_sources, aux)
        # 수집 원시 후보 수 — 카운트 invariant 합계에 넣지 않는 별도 메타값(§8-1).
        _safe_diag(run_diag, lambda: run_diag.mark_collected(len(candidates)))
        if not candidates:
            logger.warning("[news] 후보 없음 → news_top upsert skip (last good 유지)")
            _safe_diag(run_diag, lambda: run_diag.mark_skipped("NO_CANDIDATES"))
            return

        # 다양성 guard(source family)
        families = cand.count_source_families(candidates)
        if families < cand.MIN_SOURCE_FAMILIES:
            logger.warning(
                "[news] source_diversity_failed(독립 family %d < %d) → skip (last good 유지)",
                families, cand.MIN_SOURCE_FAMILIES,
            )
            _safe_diag(run_diag, lambda: run_diag.mark_skipped("SOURCE_DIVERSITY_FAILED"))
            return

        # 2) 신호 산출 (홈 rank demand는 ranker가 candidate.sources에서 직접 읽음)
        news_signals = cand.build_news_signals(candidates, cached_search_news)
        if not news_signals:
            logger.warning("[news] no_news_evidence(News 신호 전무) → skip (last good 유지)")
            _safe_diag(run_diag, lambda: run_diag.mark_skipped("NO_NEWS_SIGNALS"))
            return
        kw_list = [c["keyword"] for c in candidates]
        datalab_signals = datalab_adapter.fetch(kw_list)
        google_signals = google_adapter.fetch_signals(kw_list)

        signals = {
            "news": news_signals,
            "datalab": datalab_signals,
            "google": google_signals,
        }

        # 3) pass1(strict): score → dedupe/same-issue merge → generic singleton 제외 → Top10
        # snapshot 소유권은 호출자에 있다 — 여기서 만들어 명시적으로 전달한다(전역 상태 없음).
        snap1 = _new_snapshot(run_diag, "pass1(strict)")
        top = _rank_and_select(candidates, signals, "pass1(strict)", diag=snap1)
        # 채택 확정 시점에만 final_snapshot을 지정한다.
        _safe_diag(run_diag, lambda: run_diag.commit(snap1))
        if not top:
            logger.warning("[news] 랭킹 결과 없음 → skip (last good 유지)")
            # 랭킹을 이미 통과했으므로 판정이 실재한다 → snapshot decisions를 보존한다.
            _safe_diag(run_diag, lambda: run_diag.mark_skipped("NO_RANKING_RESULT"))
            return

        # 3-1) pass2(backfill): final 부족 또는 최근성 가드 미달이면 후보 발굴 확장.
        if len(top) < ranker.TOP_N or _count_recent_keywords(top) < MIN_RECENT_KEYWORDS:
            snap2 = _new_snapshot(run_diag, "pass2(backfill)")
            top2, candidates2 = _backfill_pass(
                top, aux, home_fulls, google_cands, daum_full,
                cached_search_news, news_signals, datalab_signals, google_signals,
                diag=snap2,
            )
            if top2 is not None:
                top, candidates = top2, candidates2
                # pass2 채택 → 최종 snapshot 교체(pass1 판정은 최종 결과가 아니므로 폐기).
                _safe_diag(run_diag, lambda: run_diag.commit(snap2))
            elif snap2 is not None and snap2.degraded:
                # 폐기된 pass의 degraded는 정상 pass1 저장을 막지 않는다 — warning만.
                logger.warning(
                    "[news-diag] pass2(폐기됨) 진단 오류 %d건, 최초 유형=%s "
                    "— 최종 pass1 진단 저장에는 영향 없음",
                    len(snap2.errors), snap2.errors[0] if snap2.errors else "unknown",
                )

        # Top10 최근성 가드
        recent_kw = _count_recent_keywords(top)
        if recent_kw < MIN_RECENT_KEYWORDS:
            logger.warning(
                "[news] recent_guard_failed(최근 기사 보유 키워드 %d < %d) → skip (last good 유지)",
                recent_kw, MIN_RECENT_KEYWORDS,
            )
            # 채택 pass의 전체 판정이 실재한다 → 보존한다(0으로 밀지 않는다).
            _safe_diag(run_diag, lambda: run_diag.mark_skipped("RECENT_GUARD_FAILED"))
            return
        if len(top) < ranker.TOP_N:
            # publish 정책(사용자 확정 2026-07-04):
            #  - hard guard 실패(no news / source diversity / recent guard)는 위에서 upsert skip +
            #    last-good 유지로 이미 처리된다.
            #  - 여기 도달했다는 건 recent guard(최근 기사 보유 키워드 >= MIN_RECENT_KEYWORDS=5)를
            #    통과했다는 뜻이고, recent_kw <= len(top)이므로 발행분은 최소 5개이며 전부 quality/
            #    fresh gate 통과분이다("빈 값 덮어쓰기" 구조적으로 불가).
            #  - 따라서 Top10 미만이어도 저품질 filler를 넣지 않고 partial snapshot으로 publish한다.
            #    오래된 last-good 10개를 유지하는 것보다 신선한 5~9개를 발행하는 쪽을 우선한다.
            logger.warning(
                "[news] partial snapshot publish: 품질 통과 후보 %d개(<%d) → filler 없이 발행"
                "(신선한 부분 결과 우선, last-good 대체)",
                len(top), ranker.TOP_N,
            )

        # 4) build + data_sources (실제 참여 family)
        data_sources = ["naver_news"]
        if datalab_signals:
            data_sources.append("datalab")
        participating = set()
        for c in candidates:
            participating |= set(c["sources"].keys()) & cand._INDEPENDENT_SEARCH_FAMILIES
        data_sources.extend(sorted(participating))
        candidate_map = {c["keyword"]: c for c in candidates}
        issues = build_ranked_issues(top, candidate_map, data_sources)

        # 5) movement 주입 — upsert 직전 기존 news_top 을 read-only 비교(공식 순위변화).
        #    기존 row 없으면 movement 필드 생략(최초 화면 NEW 도배 방지).
        previous = fetch_news_issues(source="news_top")
        issues = apply_movement(previous, issues)

        # 6) 썸네일 enrich — 같은 previous(추가 DB 조회 없음)로 이전 thumbnail 재사용 +
        #    캐시 미스 URL 만 신규 og:image 수집. movement 와 분리된 thumbnail 전용 후처리.
        #    실패해도 issues/ upsert 에 영향 없음(내부에서 조용히 생략).
        try:
            issues = enrich_issue_thumbnails(issues, previous)
        except Exception as te:
            logger.warning("[news] 썸네일 enrich 실패(무시하고 진행): %s", te)

        # selected 행의 rank/summary/대표/display_articles는 여기서야 확정된다
        # (_rank_and_select 시점에는 builder가 아직 안 돌아 존재하지 않는다).
        # 실제 발행되는 payload 그대로 진단에 반영해 둘이 어긋나지 않게 한다.
        _safe_diag(run_diag, lambda: _finalize_selected(run_diag, issues))

        # selection_diagnostics_v1(H) 적재 — 실행단위 관측 정보(순수 관찰, 랭킹 무관).
        _safe_diag(run_diag, lambda: _record_selection_diagnostics(
            run_diag, issues, seed_status_map, len(candidates),
        ))

        # freshness guard(best-effort last-write-wins 완화): 이미 저장된 previous 보다
        # 새 payload 의 generated_at 이 '명확히 과거'면 upsert 를 건너뛴다. 큐 정체 등으로
        # 오래된 실행이 뒤늦게 완료돼 최신 news_top 을 덮는 것을 막는다.
        # fail-open: 값 누락/비문자열/파싱실패/동시각/최신 은 전부 정상 write(정상 신선 실행
        # 을 막지 않는다). 완전 원자성 아님(read~write 사이 TOCTOU 는 후속 RPC 과제).
        if _is_stale_news_top_write(previous, issues):
            # 추적용 비-secret 값만 뽑는다(generated_at·mode·run_id 등 관측 메타뿐, 기사
            # 본문/payload 전문/토큰은 남기지 않는다).
            prev_gen = previous.get("generated_at") if isinstance(previous, dict) else None
            new_gen = issues.get("generated_at") if isinstance(issues, dict) else None
            run_id = os.getenv("GITHUB_RUN_ID")
            # secret/payload 전문 없이 추적용 정보만 구조화 로그로 남긴다.
            logger.warning(
                "[news] source=news_top action=skip_stale_write "
                "previous_generated_at=%s new_generated_at=%s run_type=%s "
                "→ 오래된 실행이 최신을 덮지 않도록 upsert 생략",
                prev_gen, new_gen, run_type,
            )
            # 진단: 발행 성공(success)이 아니라 skipped 로 남긴다(Admin 성공 집계에서 제외).
            # skip_reason 은 배포 SQL CHECK 에 등록된 STALE_WRITE_SKIPPED(단일 권위 상수).
            # candidates/decisions/selection_diagnostics 는 이미 위에서 확정돼 보존된다.
            _safe_diag(run_diag, lambda: run_diag.mark_skipped(
                diagnostics.SKIP_REASON_STALE_WRITE))
            # 추적 근거를 thresholds JSONB 의 별도 최상위 namespace 로 적재(selection_
            # diagnostics_v1 등 기존 키를 덮지 않는다). 고정 소수 필드라 8KB 상한 무관.
            _safe_diag(run_diag, lambda: run_diag.thresholds.__setitem__(
                "stale_write_v1", {
                    "action": "skip_stale_write",
                    "previous_generated_at": prev_gen,
                    "new_generated_at": new_gen,
                    "mode": run_type,
                    "run_id": run_id,
                    "comparison": "new_older_than_previous",
                    "result": "skipped",
                }))
        # upsert_news_issues는 예외가 아니라 False를 반환한다(db/supabase.py) —
        # False를 성공으로 기록하지 않는다.
        elif upsert_news_issues(issues, source="news_top"):
            logger.info(
                "[news] news_top 저장 완료 (%d개, sources=%s)",
                len(issues["keywords"]), data_sources,
            )
        else:
            logger.warning("[news] news_top 저장 실패")
            _safe_diag(run_diag, lambda: run_diag.mark_failed("NEWS_TOP_UPSERT_FAILED"))
    except Exception as e:
        logger.error(f"[news] 실시간 이슈 브리핑 실패(커뮤니티/키워드 수집에는 영향 없음): {e}")
        # skip_reason CHECK에 실행 예외용 값이 없다(배포 스키마) → status=failed만 남기고
        # 사유는 error_summary(예외 타입명)로 기록한다. 새 코드를 임의로 만들지 않는다.
        _safe_diag(run_diag, lambda: run_diag.mark_failed(None, e))
    finally:
        # 진단 저장은 랭킹 흐름 밖에서, 결과와 무관하게 마지막에 1회 시도한다.
        _finalize_diagnostics(run_diag)


if __name__ == "__main__":
    # 실행 모드 분기 (cron 분리용):
    #   full           : 커뮤니티 + 포털·서비스별 keyword_cache + news_top (매시 17분)
    #   news_top_only  : 커뮤니티 생략, 포털·서비스별 keyword_cache + news_top (매시 47분)
    #     2026-07 개선: keyword_cache가 정각/17분 full에서만 갱신돼 news_top(30분 주기)
    #     체감과 어긋나던 문제 해결 — news_top_only 에도 keyword_cache 갱신을 포함해
    #     "포털 키워드 수집 → keyword_cache upsert → news_top 생성" 순서를 동일하게 맞춘다.
    # 기본값 full. 알 수 없는 모드는 fallback 없이 즉시 실패.
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    if mode == "full":
        run()
    elif mode == "news_top_only":
        logger.info("[mode] news_top_only — 커뮤니티 수집 생략, 포털 키워드+news_top 갱신")
        _collect_keyword_caches({})
        run_news_briefing(run_type="news_top_only")
    else:
        raise SystemExit(f"Unknown mode: {mode!r} (allowed: full, news_top_only)")
