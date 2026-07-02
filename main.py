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


def run_news_briefing():
    """통합 랭킹으로 news_issue_cache(source='news_top') 갱신.

    흐름: 후보수집(daum/danawa/google/보조후보) → News/DataLab/Google 신호 →
          ranker score → Top10 → build_ranked_issues → upsert.
    Daum 순서를 그대로 쓰지 않고 자체 score로 재정렬한다.

    upsert skip 가드(기존 캐시 보존):
    - News 신호 전무(키 없음/전건 실패) → skip
    - 다양성 부족(Daum 비단독 후보 < MIN_NON_DAUM_CANDIDATES) → skip
    - 후보 없음 → skip
    - Top10 중 최근 기사 보유 키워드 < MIN_RECENT_KEYWORDS → skip
    실패해도 커뮤니티/키워드 수집 결과엔 영향 없도록 격리.
    """
    try:
        # 1) 후보 수집
        daum_ranked, daum_fresh = fetch_ranked_seed("daum")
        danawa_ranked, _ = fetch_ranked_seed("danawa")
        if not daum_fresh:
            # daum stale → 후보/신호에서 제외 (seed 단독 후보면 다양성 가드에서 걸림)
            logger.warning("[news] daum seed stale → daum 후보 제외")
            daum_ranked = []
        google_cands = google_adapter.fetch_candidates()
        aux = cand.derive_aux_keywords(daum_ranked, search_news)
        candidates = cand.collect_candidates(daum_ranked, danawa_ranked, google_cands, aux)
        if not candidates:
            logger.warning("[news] 후보 없음 → news_top upsert skip (기존 캐시 보존)")
            return

        # 다양성 hard guard
        non_daum = cand.count_non_daum(candidates)
        if non_daum < cand.MIN_NON_DAUM_CANDIDATES:
            logger.warning(
                "[news] 다양성 부족(Daum 비단독 후보 %d < %d) → skip (Daum 복제 방지)",
                non_daum, cand.MIN_NON_DAUM_CANDIDATES,
            )
            return

        # 2) 신호 산출
        news_signals = cand.build_news_signals(candidates, search_news)
        if not news_signals:
            logger.warning("[news] News 신호 전무 → news_top upsert skip (기존 캐시 보존)")
            return
        kw_list = [c["keyword"] for c in candidates]
        datalab_signals = datalab_adapter.fetch(kw_list)
        google_signals = google_adapter.fetch_signals(kw_list)
        daum_signals = {c["keyword"]: c["sources"].get("daum") for c in candidates}

        signals = {
            "news": news_signals,
            "datalab": datalab_signals,
            "google": google_signals,
            "daum": daum_signals,
        }

        # 3) score → dedupe/same-issue merge → Top10
        #    (dedupe/merge는 score 계산 후, Top10 확정 전에 적용 — 유사 키워드/같은 이슈가
        #    각각 별도 순위를 차지하지 않도록. docs/news-ranking-quality-plan.md §7)
        ranked = ranker.compute_scores(candidates, signals)
        ranked = ranker.dedupe_and_merge(ranked)
        top = ranker.select_top(ranked)
        if not top:
            logger.warning("[news] 랭킹 결과 없음 → skip")
            return

        # Top10 최근성 가드
        recent_kw = sum(
            1 for t in top if (t.get("news_meta") or {}).get("recent_count", 0) >= 1
        )
        if recent_kw < MIN_RECENT_KEYWORDS:
            logger.warning(
                "[news] 최근 기사 보유 키워드 부족(%d < %d) → skip (실시간성 부족)",
                recent_kw, MIN_RECENT_KEYWORDS,
            )
            return

        # 4) build + data_sources
        data_sources = ["naver_news"]
        if datalab_signals:
            data_sources.append("datalab")
        if google_signals:
            data_sources.append("google")
        if any(c["sources"].get("daum") is not None for c in candidates):
            data_sources.append("daum")
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
