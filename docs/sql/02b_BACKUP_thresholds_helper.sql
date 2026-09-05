-- =====================================================================
-- 02b_BACKUP_thresholds_helper — 읽기 전용. 상용 Supabase SQL Editor.
--
-- 02 결과에서 news_diag_list_runs 가 thresholds_display 를 직접 만들지 않고
-- _news_diag_thresholds_display(n.thresholds) 헬퍼를 부르는 것이 확인됐다.
-- 04 에서 고쳐야 할 대상은 list_runs 가 아니라 **이 헬퍼**다.
--
-- 파일 전체를 복사해 실행하고, definition 을 rollback 원본으로 보관하세요.
-- =====================================================================
SELECT p.proname::text AS fn,
       pg_get_function_identity_arguments(p.oid) AS args,
       CASE WHEN p.prosecdef THEN 'SECURITY DEFINER' ELSE 'INVOKER' END AS security,
       pg_get_functiondef(p.oid) AS definition
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'public'
   AND p.proname LIKE '%thresholds_display%'
 ORDER BY p.proname;
