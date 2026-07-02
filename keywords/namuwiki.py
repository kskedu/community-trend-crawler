import logging
from typing import List, Dict
from keywords.base import BaseKeywordScraper

logger = logging.getLogger(__name__)

# namu.news(SSR WikiRank JSON) — 2026-06 서비스 종료(브라우저 접속 시 "서비스 종료 안내").
# 복구 시도 금지. namu.wiki 본사이트 우측 "실시간 검색어" 위젯으로 대체를 검토했으나,
# raw HTML(SSR)에 해당 위젯 마크업/데이터가 전혀 포함되지 않고 클라이언트 JS 렌더링으로만
# 노출됨을 확인(2026-07-02). Playwright 등 브라우저 크롤링은 비용 대비 필요성이 낮아 붙이지
# 않으므로, namuwiki source는 raw HTML로 수집 가능한 대체 upstream이 없어 비활성화한다.


class NamuwikiKeywordScraper(BaseKeywordScraper):
    source = "namuwiki"
    active = False  # optional/degraded: upstream 없음 — run() 루프에서 skip

    def scrape(self) -> List[Dict[str, str]]:
        raise RuntimeError(
            "namuwiki source 비활성화 — namu.news 서비스 종료, "
            "namu.wiki 실시간 검색어는 raw HTML에 없음(JS 렌더링 전용)"
        )
