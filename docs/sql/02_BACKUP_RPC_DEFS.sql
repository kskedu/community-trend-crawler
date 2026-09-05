-- =====================================================================
-- 02_BACKUP_RPC_DEFS — 읽기 전용. 상용 Supabase SQL Editor.
-- APPLY 전에 반드시 실행하고, 결과를 통째로 보관하세요(= rollback 원본).
-- 이 파일 전체를 복사해 실행하면 됩니다.
-- =====================================================================
SELECT p.proname::text AS fn,
       pg_get_functiondef(p.oid) AS definition
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'public'
   AND p.proname IN ('news_diag_list_runs',
                     'news_diag_list_decisions',
                     'news_diag_record_run')
 ORDER BY p.proname;
