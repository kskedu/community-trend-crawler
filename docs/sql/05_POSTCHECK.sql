-- =====================================================================
-- 05_POSTCHECK — 읽기 전용. 상용 Supabase SQL Editor.
-- 04(RPC 교체) 까지 끝난 뒤 이 파일 전체를 복사해 실행하세요.
--
-- 판정:
--   selection_diag_exposed = true   → thresholds_display 에 진단이 실린다
--   decision_new_cols      = 9      → 새 컬럼이 list RPC 로 반환된다
-- (값 자체는 NEWS_DIAG_COMPACT_FIELDS=1 을 켠 이후 run 부터 채워집니다)
-- =====================================================================
WITH r AS (
  SELECT public.news_diag_list_runs(now() - interval '2 days', now(), NULL, 1, 0) AS j
), one AS (
  SELECT j -> 'rows' -> 0 AS run FROM r
), d AS (
  SELECT public.news_diag_list_decisions(
           ((SELECT run FROM one) ->> 'run_id')::uuid, 200, 0) AS j
)
SELECT
  ((SELECT run FROM one) -> 'thresholds_display') ? 'selection_diagnostics_v1'
    AS selection_diag_exposed,
  (SELECT count(*)
     FROM jsonb_object_keys((SELECT j FROM d) -> 'rows' -> 0) AS k
    WHERE k IN ('pre_cut_rank','independent_family_count','unique_url_count',
                'unique_domain_count','merge_mode','shared_evidence_count',
                'residual_support_winner','residual_support_self','merge_reason'))
    AS decision_new_cols,
  (SELECT run FROM one) ->> 'run_key' AS checked_run;
