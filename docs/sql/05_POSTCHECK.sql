-- =====================================================================
-- 05_POSTCHECK — 읽기 전용. 상용 Supabase SQL Editor.
-- 파일 전체를 그대로 복사해 실행하세요.
--
-- 판정:
--   selection_diag_exposed = true  → thresholds_display 에 진단이 실린다(04a)
--   decision_new_cols      = 9     → 새 컬럼이 list RPC 로 반환된다(04b)
--
-- ⚠️ 값은 아직 전부 null 입니다. NEWS_DIAG_COMPACT_FIELDS=1 을 켠 이후 run 부터
--    채워집니다(과거 run 소급 없음). 여기서는 **키가 반환되는지**만 봅니다.
--
-- 실제 시그니처(PRECHECK 실측):
--   news_diag_list_runs(p_since, p_until, p_status, p_limit, p_offset)
--   news_diag_list_decisions(p_run_id, p_since, p_until, p_category,
--                            p_reason_code, p_keyword, p_limit, p_offset)
--   → 이름 지정 인자(=>)로 호출해 위치 인자 실수를 원천 차단한다.
-- =====================================================================
WITH r AS (
  SELECT public.news_diag_list_runs(
           p_since  => now() - interval '2 days',
           p_until  => now(),
           p_status => NULL,
           p_limit  => 1,
           p_offset => 0) AS j
), one AS (
  SELECT j -> 'rows' -> 0 AS run FROM r
), d AS (
  SELECT public.news_diag_list_decisions(
           p_run_id => ((SELECT run FROM one) ->> 'run_id')::uuid,
           p_limit  => 200,
           p_offset => 0) AS j
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
  jsonb_array_length((SELECT j FROM d) -> 'rows') AS decision_rows,
  (SELECT run FROM one) ->> 'run_key' AS checked_run;
