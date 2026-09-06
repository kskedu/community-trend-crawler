-- ============================================================
-- [4/4] 활성화 후 검증 — 읽기 전용. 파일 전체를 복사해 실행하세요.
--
-- 목적: NEWS_DIAG_GROUNDING_REASON=1 설정 이후 실제로 새 reason_code 가
--       저장되고 있는지 확인한다. crawler 로그에는 reason_code 가 남지 않아
--       (drop 로그만 있음) DB 로만 확인할 수 있다.
--
-- ⚠️ 상용 Production DB. 이 파일은 SELECT 만 한다 — 쓰기 0건.
--
-- 컬럼명은 01_PRECHECK 섹션 4 실측 기준이다(추측 아님):
--   news_keyword_runs.run_id (uuid) / .started_at (timestamptz)
--   news_keyword_decisions.run_id (uuid) / .reason_code (text) / .keyword (text)
-- ============================================================

WITH activation AS (
  -- Repository variable 설정 시각(UTC). 이 시각 이후 run 만 본다.
  SELECT timestamptz '2026-09-06T11:28:30Z' AS t
),
scoped AS (
  SELECT d.reason_code, d.keyword, r.run_key, r.started_at
    FROM public.news_keyword_decisions d
    JOIN public.news_keyword_runs      r ON r.run_id = d.run_id
   WHERE r.started_at >= (SELECT t FROM activation)
)
SELECT '1. 활성화 이후 run 수' AS section,
       ''                       AS item,
       count(DISTINCT run_key)::text AS value
  FROM scoped

UNION ALL

SELECT '2. 활성화 이후 reason_code 분포' AS section,
       reason_code                       AS item,
       count(*)::text                    AS value
  FROM scoped
 GROUP BY reason_code

UNION ALL

-- 핵심: 새 코드가 실제로 저장됐는가.
SELECT '3. 판정' AS section,
       'CANONICAL_SOURCE_UNGROUNDED 저장 여부' AS item,
       CASE WHEN EXISTS (SELECT 1 FROM scoped WHERE reason_code = 'CANONICAL_SOURCE_UNGROUNDED')
            THEN 'OK — 새 코드가 저장되고 있습니다'
            ELSE 'NONE — 아직 0건(해당 탈락이 없었을 수도, flag 미전달일 수도 있음)'
       END AS value

UNION ALL

-- 새 코드로 기록된 실제 후보 전체. 오귀속이 고쳐졌는지 눈으로 확인용
-- (활성화 이후 20 run 기준 grounding drop 은 11건이라 행 수는 적다).
SELECT '4. 새 코드로 기록된 후보' AS section,
       keyword                     AS item,
       to_char(started_at AT TIME ZONE 'Asia/Seoul', 'MM-DD HH24:MI') AS value
  FROM scoped
 WHERE reason_code = 'CANONICAL_SOURCE_UNGROUNDED'
 ORDER BY section, item, value;
