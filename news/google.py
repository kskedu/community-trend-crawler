"""Google 신호 adapter (1차 stub / optional).

설계 계약 (docs/news-ranking-plan.md §4-4):
- 공식/허용 경로가 불명확하면 직접 크롤링/차단 우회 금지.
- 1차 구현은 인터페이스만 두고 항상 skip + WARNING (google 신호 weight 0 재정규화).
- 별도 키/경로 승인 후 활성화. 활성화 전까지 외부 호출 0.

인터페이스(후속 활성화 시 동일 시그니처 유지):
- fetch_candidates() -> [{keyword, rank}]   : 후보 공급
- fetch_signals(keywords) -> {keyword: {rank|interest}} : 점수 신호
"""
import logging
import os
from typing import Dict, List

logger = logging.getLogger(__name__)

# 명시적 활성화 플래그 (기본 비활성). 승인 후 '실연동 구현이 들어온 뒤'에만 의미가 생긴다.
#   주의: 1차에는 실연동 코드가 없으므로 플래그를 켜도 동작은 동일(skip)하다.
#   이는 의도된 안전장치 — 승인 전 임의 외부호출(차단 우회 등)을 원천 차단한다.
ENABLED_ENV = "GOOGLE_TRENDS_ENABLED"


def is_enabled() -> bool:
    return os.environ.get(ENABLED_ENV, "").lower() in ("1", "true", "yes")


def fetch_candidates() -> List[dict]:
    """후보 키워드 공급. 1차 stub: 항상 빈 리스트 + WARNING."""
    if not is_enabled():
        logger.warning("[news] Google adapter 비활성(stub) → 후보 공급 skip")
        return []
    # 활성화 경로는 별도 승인 후 구현. 그 전까지 안전하게 skip.
    logger.warning("[news] Google adapter 활성 플래그는 켜졌으나 구현 미연동 → skip")
    return []


def fetch_signals(keywords: List[str]) -> Dict[str, dict]:
    """점수 신호. 1차 stub: 항상 빈 dict + WARNING (google weight 0 재정규화)."""
    if not is_enabled():
        logger.warning("[news] Google adapter 비활성(stub) → 신호 skip")
        return {}
    logger.warning("[news] Google adapter 활성 플래그는 켜졌으나 구현 미연동 → skip")
    return {}
