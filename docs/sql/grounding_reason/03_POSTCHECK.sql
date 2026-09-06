-- ============================================================
-- [3/3] POSTCHECK — 읽기 전용. 파일 전체를 복사해 실행하세요.
--
-- 목적: 새 값이 허용됐고, 기존 값이 하나도 빠지지 않았는지 확인한다.
-- ⚠️ 상용 DB. SELECT 만 한다.
-- ============================================================

WITH def AS (
  SELECT pg_get_constraintdef(con.oid) AS d
    FROM pg_constraint con
    JOIN pg_class     rel ON rel.oid = con.conrelid
    JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
   WHERE nsp.nspname = 'public'
     AND rel.relname = 'news_keyword_decisions'
     AND con.contype = 'c'
     AND pg_get_constraintdef(con.oid) ILIKE '%reason_code%'
   LIMIT 1
)
SELECT '1. 새 값 허용 여부' AS section,
       CASE WHEN (SELECT d FROM def) LIKE '%CANONICAL_SOURCE_UNGROUNDED%'
            THEN 'OK — 허용됨' ELSE 'FAIL — 02 를 다시 실행하세요' END AS result

UNION ALL

-- 기존에 저장된 모든 reason_code 가 여전히 CHECK 를 만족하는지(값 누락 회귀 검사).
SELECT '2. 기존 저장값 중 제약 밖으로 밀려난 것' AS section,
       coalesce(string_agg(DISTINCT x.reason_code, ', '), '없음 — OK') AS result
  FROM (
    SELECT DISTINCT d.reason_code
      FROM public.news_keyword_decisions d
     WHERE d.reason_code IS NOT NULL
       AND position(d.reason_code IN (SELECT d FROM def)) = 0
  ) x

UNION ALL

SELECT '3. CHECK 정의 전문' AS section, (SELECT d FROM def) AS result;
