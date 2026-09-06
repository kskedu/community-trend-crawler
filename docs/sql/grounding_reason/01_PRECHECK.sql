-- ============================================================
-- [1/3] PRECHECK — 읽기 전용. 파일 전체를 복사해 실행하세요.
--
-- 목적: news_keyword_decisions.reason_code CHECK 제약의 현재 허용값을 확인한다.
--       CANONICAL_SOURCE_UNGROUNDED 가 이미 있으면 02(APPLY)는 건너뛴다.
--
-- ⚠️ 상용 DB. 이 파일은 SELECT 만 한다 — 아무것도 바꾸지 않는다.
-- ============================================================

-- Supabase SQL Editor 는 **마지막 SELECT 하나만** 표시하므로 UNION ALL 로 합친다.
SELECT '1. reason_code CHECK 제약 정의' AS section,
       con.conname                      AS name,
       pg_get_constraintdef(con.oid)    AS detail
  FROM pg_constraint con
  JOIN pg_class     rel ON rel.oid = con.conrelid
  JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
 WHERE nsp.nspname = 'public'
   AND rel.relname = 'news_keyword_decisions'
   AND con.contype = 'c'
   AND pg_get_constraintdef(con.oid) ILIKE '%reason_code%'

UNION ALL

SELECT '2. CANONICAL_SOURCE_UNGROUNDED 등록 여부' AS section,
       'already_allowed'                          AS name,
       CASE WHEN EXISTS (
              SELECT 1
                FROM pg_constraint con
                JOIN pg_class     rel ON rel.oid = con.conrelid
                JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
               WHERE nsp.nspname = 'public'
                 AND rel.relname = 'news_keyword_decisions'
                 AND con.contype = 'c'
                 AND pg_get_constraintdef(con.oid) LIKE '%CANONICAL_SOURCE_UNGROUNDED%')
            THEN 'YES — 02_APPLY 를 건너뛰고 03_POSTCHECK 로 가세요'
            ELSE 'NO — 02_APPLY 를 실행해야 합니다'
       END                                        AS detail

UNION ALL

-- 참고: 현재 저장된 reason_code 분포(오귀속 규모 파악용).
-- ⚠️ runs 와 join 하지 않는다 — decisions 의 FK 컬럼명을 추측하면 42703 이 난다
--    (실제로 r.id 로 썼다가 실패했다). 기간 필터 없이 decisions 만 집계한다.
SELECT '3. reason_code 분포(전체 보존분)' AS section,
       d.reason_code                     AS name,
       count(*)::text                    AS detail
  FROM public.news_keyword_decisions d
 GROUP BY d.reason_code

UNION ALL

-- 다음 단계(기간 필터 등)를 위해 두 테이블의 실제 컬럼명을 같이 뽑는다(추측 금지).
SELECT '4. 테이블 컬럼 실측' AS section,
       c.table_name || '.' || c.column_name AS name,
       c.data_type                          AS detail
  FROM information_schema.columns c
 WHERE c.table_schema = 'public'
   AND c.table_name IN ('news_keyword_decisions', 'news_keyword_runs')

 ORDER BY section, name;
