-- =====================================================================
-- 01_PRECHECK — 읽기 전용. 상용(production) Supabase SQL Editor.
-- 이 파일 전체를 그대로 복사해 실행하세요. 아무것도 바꾸지 않습니다.
-- =====================================================================
WITH proc AS (
  SELECT 'proc'::text AS kind,
         p.proname::text AS name,
         pg_get_function_identity_arguments(p.oid) AS detail,
         CASE WHEN p.prosecdef THEN 'SECURITY DEFINER' ELSE 'INVOKER' END AS extra
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
   WHERE n.nspname = 'public'
     AND p.proname IN ('news_diag_list_runs',
                       'news_diag_list_decisions',
                       'news_diag_record_run')
), grants AS (
  SELECT 'grant'::text, routine_name::text, grantee::text, privilege_type::text
    FROM information_schema.routine_privileges
   WHERE routine_schema = 'public'
     AND routine_name IN ('news_diag_list_runs',
                          'news_diag_list_decisions',
                          'news_diag_record_run')
), cols AS (
  -- APPLY 가 추가할 9개 컬럼이 이미 있는지(멱등 확인).
  SELECT 'column'::text, column_name::text, data_type::text, is_nullable::text
    FROM information_schema.columns
   WHERE table_schema = 'public' AND table_name = 'news_keyword_decisions'
     AND column_name IN ('pre_cut_rank','independent_family_count','unique_url_count',
                         'unique_domain_count','merge_mode','shared_evidence_count',
                         'residual_support_winner','residual_support_self','merge_reason')
), stored AS (
  -- 저장은 되는데 노출만 안 되는지: 최근 run 의 thresholds 키.
  SELECT 'stored_keys'::text, r.run_key::text,
         (SELECT string_agg(k, ',' ORDER BY k)
            FROM jsonb_object_keys(r.thresholds) AS k),
         r.started_at::text
    FROM public.news_keyword_runs r
   ORDER BY r.started_at DESC
   LIMIT 5
)
SELECT * FROM proc
UNION ALL SELECT * FROM grants
UNION ALL SELECT * FROM cols
UNION ALL SELECT * FROM stored;
