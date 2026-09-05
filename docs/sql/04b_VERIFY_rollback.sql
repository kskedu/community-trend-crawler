-- =====================================================================
-- 04b_VERIFY_rollback — 읽기 전용. 상용 Supabase SQL Editor.
-- 04b 가 EXCEPTION 으로 실패했을 때, 정말 아무것도 안 바뀌었는지 확인합니다.
--
-- DO 블록은 단일 트랜잭션이라 중간 EXECUTE 도 함께 롤백되어야 정상입니다.
-- 기대: list_decisions / record_run 둘 다 patched=false (아직 미적용)
--       thresholds_display 는 patched=true (04a 로 이미 적용됨)
-- =====================================================================
SELECT p.proname,
       count(*) AS overloads,
       bool_or(pg_get_functiondef(p.oid) LIKE '%pre_cut_rank%') AS patched,
       bool_or(p.prosecdef) AS security_definer
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname='public'
   AND p.proname IN ('news_diag_list_decisions','news_diag_record_run',
                     '_news_diag_thresholds_display','news_diag_list_runs')
 GROUP BY p.proname ORDER BY p.proname;
