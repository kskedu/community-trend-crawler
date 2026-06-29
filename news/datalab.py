"""네이버 DataLab 검색어트렌드 adapter.

설계 계약 (docs/news-ranking-plan.md §4-3):
- 인기검색어 발굴용 아님. 후보 키워드의 '추이(상승률)' 보강 신호.
- batch 간 비교 금지: ratio는 요청 단위 상대값이라 batch 간 직접 비교 불가.
  → relative_interest(절대 관심도)는 쓰지 않고, batch 내부 자기 시계열인
    recent_delta(최근 구간 vs 직전 구간 상승률)만 신호로 사용한다.
- recent_delta 0-division 방어: 직전 구간 0/누락이면 '신호 부재'(None) 처리,
  양 구간 모두 0이면 delta 0. 비율은 상한 clamp.
- 실패/쿼터초과/응답이상은 전체 skip → 빈 dict 반환(상위에서 datalab weight 0 재정규화).
- credential은 네이버 News와 동일 NAVER_CLIENT_ID/SECRET 재사용. 값 로그 출력 금지.
- 프론트는 이 모듈을 호출하지 않는다(crawler 전용).
"""
import logging
import os
from datetime import date, timedelta
from typing import Dict, List

import requests

logger = logging.getLogger(__name__)

DATALAB_API = "https://openapi.naver.com/v1/datalab/search"
REQUEST_TIMEOUT = 10
GROUP_MAX = 5            # keywordGroups 최대 5개/요청
DELTA_CLAMP = 3.0       # recent_delta 상한 (이상치 억제)


def _credentials():
    return os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET")


def is_enabled() -> bool:
    cid, secret = _credentials()
    return bool(cid and secret)


def _compute_delta(points: List[dict]):
    """일별 ratio points([{period, ratio}, ...]) → recent_delta.

    최근 절반 평균 vs 직전 절반 평균의 상승률.
    직전 구간 평균 0/누락 → None(신호 부재). 양쪽 0 → 0. clamp 적용.
    """
    ratios = []
    for p in points or []:
        try:
            ratios.append(float(p.get("ratio")))
        except (TypeError, ValueError):
            continue
    if len(ratios) < 2:
        return None
    mid = len(ratios) // 2
    prev = ratios[:mid]
    recent = ratios[mid:]
    if not prev or not recent:
        return None
    prev_avg = sum(prev) / len(prev)
    recent_avg = sum(recent) / len(recent)
    if prev_avg <= 1e-9:
        if recent_avg <= 1e-9:
            return 0.0
        return None  # 직전 0인데 최근 양수 → 비율 불가, 신호 부재
    delta = (recent_avg - prev_avg) / prev_avg
    return max(-DELTA_CLAMP, min(DELTA_CLAMP, delta))


def fetch(keywords: List[str], days: int = 14) -> Dict[str, dict]:
    """후보 키워드들의 recent_delta 신호 dict 반환.

    반환: {keyword: {"recent_delta": float}}  (delta 없으면 해당 키워드 생략)
    실패/키없음/응답이상 → 전체 빈 dict (전체 skip).
    """
    cid, secret = _credentials()
    if not (cid and secret):
        logger.warning("[news] DataLab: NAVER 키 미설정 → 전체 skip")
        return {}
    if not keywords:
        return {}

    end = date.today()
    start = end - timedelta(days=days)
    headers = {
        "X-Naver-Client-Id": cid,
        "X-Naver-Client-Secret": secret,
        "Content-Type": "application/json",
    }

    result: Dict[str, dict] = {}
    # 5개씩 batch. 계약: 하나라도 실패/응답이상이면 DataLab 전체 skip(빈 dict).
    #   부분 신호가 weight 재정규화에 섞여 순위를 왜곡하는 것을 막기 위함.
    for i in range(0, len(keywords), GROUP_MAX):
        batch = keywords[i:i + GROUP_MAX]
        groups = [{"groupName": kw, "keywords": [kw]} for kw in batch]
        body = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "timeUnit": "date",
            "keywordGroups": groups,
        }
        try:
            resp = requests.post(
                DATALAB_API, headers=headers, json=body, timeout=REQUEST_TIMEOUT
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            # 키 값은 로그에 노출하지 않는다. 한 batch라도 실패 → 전체 skip.
            logger.warning("[news] DataLab batch 실패 → DataLab 전체 skip: %s", e)
            return {}

        # 응답 구조 검증: results 리스트 여야 하고, 길이가 요청 그룹 수와 일치해야 함.
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list) or len(results) != len(groups):
            logger.warning("[news] DataLab 응답 이상(results 형식/개수 불일치) → 전체 skip")
            return {}
        for res in results:
            if not isinstance(res, dict):
                logger.warning("[news] DataLab 응답 이상(result 형식) → 전체 skip")
                return {}
            title = res.get("title")
            delta = _compute_delta(res.get("data"))
            # delta 산출 불가(None)는 '해당 키워드 신호 부재'로 허용(전체 skip 아님).
            if title and delta is not None:
                result[title] = {"recent_delta": delta}
    return result


def fetch_from_fixture(fixture: dict, keywords: List[str]) -> Dict[str, dict]:
    """dry-run용: fixture({keyword: {recent_delta}})에서 후보만 필터."""
    if not isinstance(fixture, dict):
        return {}
    out = {}
    for kw in keywords:
        entry = fixture.get(kw)
        if entry and entry.get("recent_delta") is not None:
            out[kw] = {"recent_delta": float(entry["recent_delta"])}
    return out
