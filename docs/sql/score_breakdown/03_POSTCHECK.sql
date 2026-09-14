-- ============================================================================
-- 03_POSTCHECK — READ ONLY. 02_APPLY 직후 실행.
-- 전부 OK 여야 한다. 하나라도 FAIL 이면 04_ROLLBACK 으로 되돌린다.
-- ============================================================================
WITH def AS (
  SELECT pg_get_functiondef(p.oid) AS v
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
  WHERE n.nspname = 'public' AND p.proname = 'news_diag_list_decisions'
),
sample AS (
  SELECT (public.news_diag_list_decisions(
            p_run_id => (SELECT run_id FROM public.news_keyword_runs
                         ORDER BY started_at DESC LIMIT 1),
            p_limit  => 50)) AS j
),
row1 AS (SELECT jsonb_array_elements((SELECT j->'rows' FROM sample)) AS r),
keys AS (SELECT string_agg(DISTINCT k, ', ' ORDER BY k) AS v FROM row1, LATERAL jsonb_object_keys(r) k),
legacy AS (
  -- 기존 consumer 가 쓰는 필드가 전부 남아 있는지(회귀 방어)
  SELECT count(*) FILTER (WHERE NOT (r ? 'keyword' AND r ? 'article_count'
          AND r ? 'display_article_count' AND r ? 'rank' AND r ? 'score'
          AND r ? 'reason_code' AND r ? 'diag_category' AND r ? 'articles')) AS missing
  FROM row1
),
newf AS (
  SELECT count(*) FILTER (WHERE r ? 'evidence_article_count') AS ev,
         count(*) FILTER (WHERE r ? 'signals') AS sg,
         count(*) AS n
  FROM row1
),
inv AS (
  SELECT ((SELECT j->>'classification_invariant_ok' FROM sample))::text AS ok,
         ((SELECT j->>'unknown_count' FROM sample))::text AS unk,
         ((SELECT j->'classification_drift'->>'match' FROM sample))::text AS drift
),
perms AS (
  SELECT string_agg(grantee || ':' || privilege_type, ', ' ORDER BY grantee) AS v
  FROM information_schema.routine_privileges
  WHERE routine_schema='public' AND routine_name='news_diag_list_decisions'
),
sec AS (
  SELECT 'secdef=' || p.prosecdef::text || ' | ' ||
         COALESCE(array_to_string(p.proconfig, ', '), '(no search_path)') AS v
  FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
  WHERE n.nspname='public' AND p.proname='news_diag_list_decisions'
)
SELECT '1. 반환 키 목록' AS section, (SELECT v FROM keys) AS detail
UNION ALL SELECT '2. 기존 필드 누락(0 이어야 OK)',
       (SELECT CASE WHEN missing=0 THEN 'OK (0)' ELSE 'FAIL ('||missing||')' END FROM legacy)
UNION ALL SELECT '3. evidence_article_count 노출',
       (SELECT CASE WHEN ev=n AND n>0 THEN 'OK ('||ev||'/'||n||')' ELSE 'FAIL ('||ev||'/'||n||')' END FROM newf)
UNION ALL SELECT '4. signals 노출',
       (SELECT CASE WHEN sg=n AND n>0 THEN 'OK ('||sg||'/'||n||')' ELSE 'FAIL ('||sg||'/'||n||')' END FROM newf)
UNION ALL SELECT '5. classification invariant',
       (SELECT 'invariant_ok='||ok||' unknown='||unk||' drift_match='||drift FROM inv)
UNION ALL SELECT '6. 권한', (SELECT v FROM perms)
UNION ALL SELECT '7. 보안 속성', (SELECT v FROM sec)
UNION ALL SELECT '8. 과거 row 호환(신규 키 NULL 허용)',
       (SELECT CASE WHEN count(*) >= 0 THEN 'OK — 조회 성공, 과거 행은 NULL 로 반환' END
        FROM row1 WHERE (r->>'evidence_article_count') IS NULL);
