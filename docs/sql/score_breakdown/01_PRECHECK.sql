-- ============================================================================
-- 01_PRECHECK — READ ONLY. production 에 아무 변경도 하지 않는다.
--
-- 목적: score_breakdown_v1 / evidence_article_count 관측을 켜기 전에
--       (1) 컬럼이 실제로 존재하는지  (2) read RPC 가 그 컬럼을 돌려주는지
--       (3) 현재 채움 현황  (4) 롤백용 함수 원본 정의
--       를 실측한다.
--
-- ⚠️ Supabase SQL Editor 는 **마지막 결과셋만** 표시하므로 UNION ALL 로 합친다.
-- ⚠️ 02_APPLY 는 12번 정의 안에서 read RPC 의 projection 에
--    `'article_count', n.article_count` 가 **정확히 1곳** 존재함을 전제로 한다(8번).
--
-- ⚠️ 배포된 RPC 는 저장소의 baseline SQL 보다 최신이다(실측: baseline recordset 에
--    없는 pre_cut_rank/merge_mode/shared_evidence_count 등이 live 에 저장돼 있다).
--    따라서 write RPC 가 신규 두 필드를 받는지는 **10/11 번으로 실측해야** 하며
--    저장소 파일로 판단하면 안 된다.
-- ============================================================================
WITH cols AS (
  SELECT string_agg(column_name || ':' || data_type, ', ' ORDER BY ordinal_position) AS v
  FROM information_schema.columns
  WHERE table_schema = 'public'
    AND table_name = 'news_keyword_decisions'
    AND column_name IN ('article_count', 'evidence_article_count',
                        'display_article_count', 'signals')
),
recent AS (
  SELECT d.*
  FROM public.news_keyword_decisions d
  JOIN public.news_keyword_runs r ON r.run_id = d.run_id
  WHERE r.started_at >= now() - interval '14 days'
),
fill AS (
  SELECT count(*) AS total,
         count(*) FILTER (WHERE evidence_article_count IS NOT NULL) AS evid,
         count(*) FILTER (WHERE signals IS NOT NULL)                AS sig,
         count(*) FILTER (WHERE signals ? 'score_breakdown_v1')     AS sbv1,
         count(*) FILTER (WHERE result_status = 'selected')         AS sel
  FROM recent
),
sig_keys AS (
  SELECT string_agg(DISTINCT k, ', ' ORDER BY k) AS v
  FROM recent, LATERAL jsonb_object_keys(signals) k
  WHERE signals IS NOT NULL
),
sizes AS (
  SELECT round(avg(pg_column_size(signals)))::text || ' B avg / ' ||
         max(pg_column_size(signals))::text || ' B max' AS v
  FROM recent WHERE signals IS NOT NULL
),
writefn AS (
  SELECT pg_get_functiondef(p.oid) AS v
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
  WHERE n.nspname = 'public' AND p.proname = 'news_diag_record_run'
),
readfn AS (
  SELECT pg_get_functiondef(p.oid) AS v
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
  WHERE n.nspname = 'public' AND p.proname = 'news_diag_list_decisions'
),
anchor AS (
  -- 앵커는 02_APPLY 와 **글자 그대로 동일**해야 한다. read RPC projection 의 alias 는
  -- `n.` 이고(초안의 `d.` 는 0곳이었다), key 이름까지 포함한 완전한 key-value 쌍을 써서
  -- 'display_article_count' 안의 부분 일치를 배제한다.
  SELECT (length(v) - length(replace(v, '''article_count'', n.article_count', '')))
         / length('''article_count'', n.article_count') AS v FROM readfn
),
perms AS (
  SELECT string_agg(grantee || ':' || privilege_type, ', ' ORDER BY grantee) AS v
  FROM information_schema.routine_privileges
  WHERE routine_schema = 'public' AND routine_name = 'news_diag_list_decisions'
)
SELECT '1. 대상 컬럼 존재'            AS section, COALESCE((SELECT v FROM cols), '(없음!)') AS detail
UNION ALL SELECT '2. 14일 행 수',      (SELECT total::text FROM fill)
UNION ALL SELECT '3. evidence_article_count 채움', (SELECT evid::text || ' / ' || total::text FROM fill)
UNION ALL SELECT '4. signals 채움',    (SELECT sig::text || ' (selected ' || sel::text || ')' FROM fill)
UNION ALL SELECT '5. score_breakdown_v1 보유', (SELECT sbv1::text FROM fill)
UNION ALL SELECT '6. signals 키 목록', COALESCE((SELECT v FROM sig_keys), '(없음)')
UNION ALL SELECT '7. signals 크기',    COALESCE((SELECT v FROM sizes), '(없음)')
UNION ALL SELECT '8. read RPC 앵커 d.article_count 출현수', (SELECT v::text FROM anchor)
UNION ALL SELECT '9. read RPC 권한',   COALESCE((SELECT v FROM perms), '(없음)')
-- 10/11: 단순 문자열 포함(position)으로 판정하면 주석·다른 식별자에도 걸려 **위양성**이
-- 난다. `jsonb_to_recordset(...) AS d(...)` 의 **컬럼 타입 선언**과 INSERT 대상 목록
-- 양쪽에 있어야 실제로 값이 저장되므로, 타입까지 붙여 정확히 확인한다.
UNION ALL SELECT '10. write RPC 가 evidence_article_count 를 선언하는가',
       (SELECT CASE
          WHEN position('evidence_article_count integer' in v) > 0
           AND position('d.evidence_article_count' in v) > 0
          THEN 'YES — recordset 선언 + INSERT 목록 모두 있음(값이 저장된다)'
          WHEN position('evidence_article_count' in v) > 0
          THEN 'PARTIAL — 문자열은 있으나 선언/목록 중 하나가 없다. 13번 정의를 직접 확인할 것'
          ELSE 'NO  — 값이 조용히 버려진다(적재 실패는 아님). 07 참조' END
        FROM writefn)
UNION ALL SELECT '11. write RPC 가 signals 를 선언하는가',
       (SELECT CASE
          WHEN position('signals jsonb' in v) > 0
           AND position('d.signals' in v) > 0
          THEN 'YES — recordset 선언 + INSERT 목록 모두 있음(값이 저장된다)'
          WHEN position('signals' in v) > 0
          THEN 'PARTIAL — 문자열은 있으나 선언/목록 중 하나가 없다. 13번 정의를 직접 확인할 것'
          ELSE 'NO  — 값이 조용히 버려진다(적재 실패는 아님). 07 참조' END
        FROM writefn)
UNION ALL SELECT '11b. write RPC 가 살아 있는가(정의 존재)',
       (SELECT CASE WHEN v IS NULL THEN 'NO — news_diag_record_run 이 없다(중대)'
                    ELSE 'YES' END FROM writefn)
UNION ALL SELECT '12. read RPC 정의(롤백용 원본)', (SELECT v FROM readfn)
UNION ALL SELECT '13. write RPC 정의(10/11 이 NO 일 때만 필요)', (SELECT v FROM writefn);
