"""순위 변화(movement) 계산 — 순수 함수, I/O 없음.

이전 Top10과 새 Top10을 keyword 기준 비교해 각 item에 optional movement 필드 주입.

설계 (docs/news-ranking-plan.md 후속, Codex 계획 리뷰 P1 반영):
- builder는 현재 Top10 생성까지만. movement 주입은 main.py가 이 모듈로 후처리.
- streak = presence_streak: Top10 연속 잔류 횟수(movement 연속 아님).
  · 이전 Top10에 없으면 presence_streak=1
  · 이전에도 있으면 previous.presence_streak + 1
  · 재진입(과거 이력 미조회)은 new + presence_streak=1
- 기존 row 자체가 없으면(최초 1회) movement 필드를 주입하지 않는다(프론트 배지 미표시).
- 중복 keyword는 첫 항목 기준 dedupe(이전/현재 양쪽).
- DDL 무변경: previous_rank/rank_delta/movement/presence_streak 는 issue item optional 필드.
- rank_delta 는 항상 양수(변화 폭의 절대값). 방향은 movement(up/down)가 가진다.
  (signed delta 아님 — 프론트는 movement 로 색/화살표, rank_delta 로 폭만 표시)
"""
from typing import Dict, List, Optional


def _prev_index(prev_keywords: Optional[List[dict]]) -> Dict[str, dict]:
    """이전 keywords 리스트 → {keyword: {rank, presence_streak}} (첫 항목 기준 dedupe)."""
    index: Dict[str, dict] = {}
    if not isinstance(prev_keywords, list):
        return index
    for item in prev_keywords:
        if not isinstance(item, dict):
            continue
        kw = item.get("keyword")
        if not isinstance(kw, str):
            continue
        kw = kw.strip()
        if not kw or kw in index:
            continue  # 첫 항목 기준 dedupe
        rank = item.get("rank")
        streak = item.get("presence_streak")
        index[kw] = {
            "rank": rank if isinstance(rank, int) else None,
            "presence_streak": streak if isinstance(streak, int) and streak > 0 else 1,
        }
    return index


def apply_movement(previous_issues: Optional[dict], new_issues: dict) -> dict:
    """new_issues.keywords 각 item에 movement 필드를 주입(in-place 후 동일 dict 반환).

    previous_issues: 기존 news_top issues dict({keywords:[...]}) 또는 None/이상값.
    new_issues: 새로 build 된 issues dict({keywords:[...]}).

    기존 row 가 없거나 파싱 불가하면 movement 필드를 주입하지 않는다(필드 생략).
    """
    if not isinstance(new_issues, dict):
        return new_issues
    new_keywords = new_issues.get("keywords")
    if not isinstance(new_keywords, list):
        return new_issues

    prev_index = _prev_index(
        previous_issues.get("keywords") if isinstance(previous_issues, dict) else None
    )
    # 기존 row 존재 판정은 "이전 issues dict 가 있었는가"로 한다(P1).
    #  · 기존 row 자체가 없음(최초 1회) → None → 필드 생략(프론트 배지 미표시)
    #  · row 는 있는데 이전 Top10(keywords)이 비었거나 파싱 불가 → prev_index 비어도 has_previous=True
    #    → 각 item 은 비교 대상이 없으니 모두 'new' 로 처리(신규 진입)
    has_previous = isinstance(previous_issues, dict) and isinstance(
        previous_issues.get("keywords"), list
    )

    # 현재 Top10 중복 keyword dedupe: 첫 항목만 movement 부여, 이후 중복은 건드리지 않음.
    seen = set()
    for item in new_keywords:
        if not isinstance(item, dict):
            continue
        kw = item.get("keyword")
        if not isinstance(kw, str):
            continue
        kw = kw.strip()
        if not kw or kw in seen:
            continue
        seen.add(kw)

        if not has_previous:
            # 기존 row 없음(최초) → 필드 생략(프론트 배지 미표시)
            continue

        cur_rank = item.get("rank")
        prev = prev_index.get(kw)
        if not prev or prev.get("rank") is None or not isinstance(cur_rank, int):
            # 이전 Top10에 없음(신규/재진입) 또는 rank 비교 불가 → new
            item["movement"] = "new"
            item["previous_rank"] = None
            item["rank_delta"] = 0
            item["presence_streak"] = 1
            continue

        prev_rank = prev["rank"]
        prev_streak = prev["presence_streak"]
        item["previous_rank"] = prev_rank
        item["presence_streak"] = prev_streak + 1
        if prev_rank > cur_rank:
            item["movement"] = "up"
            item["rank_delta"] = prev_rank - cur_rank
        elif prev_rank < cur_rank:
            item["movement"] = "down"
            item["rank_delta"] = cur_rank - prev_rank
        else:
            item["movement"] = "same"
            item["rank_delta"] = 0

    return new_issues
