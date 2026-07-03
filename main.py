"""
커뮤니티 트렌드 크롤러 진입점
GitHub Actions에서 주기적으로 실행됨
"""
import logging
import sys
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
from db.supabase import upsert_posts, upsert_keywords, upsert_news_issues, fetch_news_issues
from news.movement import apply_movement
from news.thumbnail import enrich_issue_thumbnails
from news.seed import fetch_ranked_seed
from news.naver_news import search_news
from news import candidates as cand
from news import datalab as datalab_adapter
from news import google as google_adapter
from news import ranker
from news.builder import build_ranked_issues

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

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

    # 검색엔진 키워드 수집
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


# 통합 랭킹 가드 임계 (docs/news-ranking-plan.md §10)
MIN_RECENT_KEYWORDS = 5  # Top10 중 최근 기사 보유 키워드 최소 수


def _count_recent_keywords(top):
    """Top 항목 중 최근(12h) 기사 보유 키워드 수(MIN_RECENT_KEYWORDS 가드용)."""
    return sum(1 for t in top if (t.get("news_meta") or {}).get("recent_count", 0) >= 1)


def _rank_and_select(candidates, signals, pass_name):
    """score → dedupe/merge → generic singleton 제외 → Top10 + pass별 단계 카운트 로그.

    final이 TOP_N 미만일 때 부족 사유(어느 단계에서 몇 개가 줄었는지)를 재구성할 수
    있도록 단계별 수를 항상 남긴다(품질 기준 완화 없이 개수만 관찰).
    """
    ranked = ranker.compute_scores(candidates, signals)
    gate_passed = len(ranked)
    # PR/광고성 클러스터 hard exclude — merge 이전 per-keyword 기준(문제 B).
    ranked, pr_excluded = ranker.exclude_pr_clusters(ranked)
    if pr_excluded:
        logger.warning("[news] %s: PR/광고성 클러스터 제외 %s", pass_name, pr_excluded)
    merged = ranker.dedupe_and_merge(ranked)
    # display_keyword/articles 정합성 invariant — merge 후, generic guard 전(문제 A).
    merged = ranker.enforce_display_article_consistency(merged)
    kept, generic_excluded = ranker.exclude_generic_singletons(merged)
    if generic_excluded:
        logger.warning("[news] %s: generic singleton 제외 %s", pass_name, generic_excluded)
    top = ranker.select_top(kept)
    logger.info(
        "[news] %s: candidates=%d gate통과=%d PR제외=%d merge후=%d generic제외=%d final=%d",
        pass_name, len(candidates), gate_passed, len(pr_excluded), len(merged),
        len(generic_excluded), len(top),
    )
    return top


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

    반환: {family: ranked(list, 최대 20)}. fresh 하지 않거나 조회 실패한 family는
    제외하고 drop_reason 로그를 남긴다(전체 pipeline은 죽이지 않는다).
    """
    fulls = {}
    for source, family, short in _HOME_SEED_SPEC:
        ranked, fresh = fetch_ranked_seed(source)
        if not ranked:
            logger.warning("[news] %s seed 없음/조회 실패 → drop(%s_fetch_failed)", family, short)
            continue
        if not fresh:
            logger.warning("[news] %s seed stale → drop(stale_only)", family)
            continue
        fulls[family] = ranked
    return fulls


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


def _backfill_pass(
    pass1_top, pass1_aux, home_fulls, google_cands, daum_full,
    cached_search_news, news_signals, datalab_signals, google_signals,
):
    """pass2(backfill): 후보 발굴 확장 후 동일 gate/merge로 전체 재계산.

    - 신규 후보 3경로: 홈 seed Top20 확장 + aux 확장(daum_home 전체 기반, 상한 12)
      + 뉴스 title 기반 phrase 후보 + Google related terms(있으면).
    - 신규 후보만 뉴스 실호출(cached_search_news 메모이즈), datalab/google 신호는
      pass1 것을 재사용(추가 API 호출 없음).
    - 증분 방식은 min-max 집합 정규화와 충돌하므로 전체 재계산을 채택한다.
    - 안전장치: 재계산 결과가 pass1보다 못하면 채택하지 않고 pass1 결과를 유지(rollback).

    반환: (top2, candidates2). 채택하지 않으면 (None, None).
    """
    try:
        aux_expanded = cand.derive_aux_keywords(
            daum_full, cached_search_news,
            top=cand.AUX_SEED_TOP_BACKFILL, aux_max=cand.AUX_MAX_BACKFILL,
        )
        # pass1 aux는 top=5 기준이라 top=10 확장 결과의 subset이 아니다 → union으로 보존.
        aux2 = list(dict.fromkeys((pass1_aux or []) + aux_expanded))
        # pass1 생존 이슈(canonical + alias)와 유사한 phrase는 재발굴하지 않는다.
        survived = []
        for t in pass1_top:
            survived.append(t["keyword"])
            survived.extend(t.get("related_keywords") or [])
        phrases = cand.derive_phrase_candidates(news_signals, survived)
        phrases = list(dict.fromkeys(phrases + _google_related_terms(google_cands)))
        # 홈 seed Top20 확장분(pass1 Top10 대비 늘어난 후보)이 있는지도 새 후보로 본다.
        seed_sources2 = _seed_sources_from(home_fulls, google_cands, HOME_PASS2_TOP)
        seed_sources1 = _seed_sources_from(home_fulls, google_cands, HOME_PASS1_TOP)
        home_expanded = any(
            len(seed_sources2.get(f, [])) > len(seed_sources1.get(f, []))
            for f in seed_sources2
        )
        if not aux_expanded and not phrases and not home_expanded:
            logger.info("[news] pass2: 신규 후보 없음 → pass1 결과 유지")
            return None, None

        candidates2 = cand.collect_candidates(
            seed_sources2, aux2,
            phrase_keywords=phrases, limit=cand.BACKFILL_CANDIDATE_MAX,
        )
        # 다양성 guard 재적용
        families = cand.count_source_families(candidates2)
        if families < cand.MIN_SOURCE_FAMILIES:
            logger.warning("[news] pass2: source_diversity_failed(%d) → pass1 결과 유지", families)
            return None, None

        news_signals2 = cand.build_news_signals(candidates2, cached_search_news)
        if not news_signals2:
            return None, None
        signals2 = {
            "news": news_signals2,
            "datalab": datalab_signals,
            "google": google_signals,
        }
        top2 = _rank_and_select(candidates2, signals2, "pass2(backfill)")
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


def run_news_briefing():
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
    try:
        # 1) 홈/트렌드 seed 수집
        home_fulls = _collect_home_seeds()  # {family: ranked(≤20)}
        google_cands = google_adapter.fetch_candidates()  # 비활성/실패 시 [] (내부 로그)
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
        if not candidates:
            logger.warning("[news] 후보 없음 → news_top upsert skip (last good 유지)")
            return

        # 다양성 guard(source family)
        families = cand.count_source_families(candidates)
        if families < cand.MIN_SOURCE_FAMILIES:
            logger.warning(
                "[news] source_diversity_failed(독립 family %d < %d) → skip (last good 유지)",
                families, cand.MIN_SOURCE_FAMILIES,
            )
            return

        # 2) 신호 산출 (홈 rank demand는 ranker가 candidate.sources에서 직접 읽음)
        news_signals = cand.build_news_signals(candidates, cached_search_news)
        if not news_signals:
            logger.warning("[news] no_news_evidence(News 신호 전무) → skip (last good 유지)")
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
        top = _rank_and_select(candidates, signals, "pass1(strict)")
        if not top:
            logger.warning("[news] 랭킹 결과 없음 → skip (last good 유지)")
            return

        # 3-1) pass2(backfill): final 부족 또는 최근성 가드 미달이면 후보 발굴 확장.
        if len(top) < ranker.TOP_N or _count_recent_keywords(top) < MIN_RECENT_KEYWORDS:
            top2, candidates2 = _backfill_pass(
                top, aux, home_fulls, google_cands, daum_full,
                cached_search_news, news_signals, datalab_signals, google_signals,
            )
            if top2 is not None:
                top, candidates = top2, candidates2

        # Top10 최근성 가드
        recent_kw = _count_recent_keywords(top)
        if recent_kw < MIN_RECENT_KEYWORDS:
            logger.warning(
                "[news] recent_guard_failed(최근 기사 보유 키워드 %d < %d) → skip (last good 유지)",
                recent_kw, MIN_RECENT_KEYWORDS,
            )
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

        if upsert_news_issues(issues, source="news_top"):
            logger.info(
                "[news] news_top 저장 완료 (%d개, sources=%s)",
                len(issues["keywords"]), data_sources,
            )
        else:
            logger.warning("[news] news_top 저장 실패")
    except Exception as e:
        logger.error(f"[news] 실시간 이슈 브리핑 실패(커뮤니티/키워드 수집에는 영향 없음): {e}")


if __name__ == "__main__":
    # 실행 모드 분기 (cron 분리용):
    #   full           : 커뮤니티 + 검색엔진 키워드 + news_top (매시 17분)
    #   news_top_only  : 실시간 이슈 news_top 만 (매시 47분, 커뮤니티/키워드 미실행)
    # 기본값 full. 알 수 없는 모드는 fallback 없이 즉시 실패.
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    if mode == "full":
        run()
    elif mode == "news_top_only":
        logger.info("[mode] news_top_only — 커뮤니티/키워드 수집 생략, news_top 만 갱신")
        run_news_briefing()
    else:
        raise SystemExit(f"Unknown mode: {mode!r} (allowed: full, news_top_only)")
