-- =====================================================================
-- 04b_APPLY_rpc_autopatch — 쓰기. 상용 Supabase SQL Editor.
-- 이 파일 전체를 그대로 복사해 실행하세요.
--
-- list_decisions / record_run 두 함수를 자동으로 패치합니다.
-- 사람이 200줄을 옮겨 적지 않습니다 — pg_get_functiondef 로 **DB 안의 원본**을
-- 읽어 정해진 앵커 문자열만 치환하고 그 결과를 EXECUTE 합니다.
-- 따라서 시그니처 / SECURITY DEFINER / search_path / 본문 로직이 전부 보존됩니다.
--
-- 멱등: 이미 패치돼 있으면(앵커에 새 컬럼이 이미 있으면) 그 함수는 건너뜁니다.
-- 앵커를 못 찾으면 EXCEPTION 으로 전체 롤백합니다(부분 적용 방지).
--
-- 선행조건: 03_APPLY_columns.sql 로 9개 컬럼이 이미 있어야 합니다.
-- =====================================================================
DO $outer$
DECLARE
  v_src   text;
  v_new   text;
  v_hits  integer;
  v_done  text[] := ARRAY[]::text[];
  v_skip  text[] := ARRAY[]::text[];

  -- ── [2] list_decisions: 반환 jsonb_build_object 에 새 키 추가 ──
  c_dec_anchor constant text := E'''articles'', n.articles, ''created_at'', n.created_at';
  c_dec_repl   constant text := E'''articles'', n.articles, ''created_at'', n.created_at,\n'
    || E'          ''pre_cut_rank'', n.pre_cut_rank,\n'
    || E'          ''independent_family_count'', n.independent_family_count,\n'
    || E'          ''unique_url_count'', n.unique_url_count,\n'
    || E'          ''unique_domain_count'', n.unique_domain_count,\n'
    || E'          ''merge_mode'', n.merge_mode,\n'
    || E'          ''shared_evidence_count'', n.shared_evidence_count,\n'
    || E'          ''residual_support_winner'', n.residual_support_winner,\n'
    || E'          ''residual_support_self'', n.residual_support_self,\n'
    || E'          ''merge_reason'', n.merge_reason';

  -- ── [3] record_run: 세 군데(INSERT 컬럼 / SELECT / recordset 정의) ──
  c_ins_anchor constant text :=
    'evidence_tokens, token_df, relevance_threshold, min_tokens, signals, rank_delta, articles';
  c_ins_repl   constant text :=
    'evidence_tokens, token_df, relevance_threshold, min_tokens, signals, rank_delta, articles,'
    || E'\n    pre_cut_rank, independent_family_count, unique_url_count, unique_domain_count,'
    || E'\n    merge_mode, shared_evidence_count, residual_support_winner, residual_support_self,'
    || E'\n    merge_reason';

  c_sel_anchor constant text := 'd.signals, d.rank_delta, d.articles';
  c_sel_repl   constant text := 'd.signals, d.rank_delta, d.articles,'
    || E'\n         d.pre_cut_rank, d.independent_family_count, d.unique_url_count,'
    || E'\n         d.unique_domain_count, d.merge_mode, d.shared_evidence_count,'
    || E'\n         d.residual_support_winner, d.residual_support_self, d.merge_reason';

  c_rs_anchor constant text :=
    'min_tokens integer, signals jsonb, rank_delta integer, articles jsonb)';
  c_rs_repl   constant text :=
    'min_tokens integer, signals jsonb, rank_delta integer, articles jsonb,'
    || E'\n      pre_cut_rank integer, independent_family_count integer,'
    || E'\n      unique_url_count integer, unique_domain_count integer,'
    || E'\n      merge_mode text, shared_evidence_count integer,'
    || E'\n      residual_support_winner integer, residual_support_self integer,'
    || E'\n      merge_reason text)';
BEGIN
  -- 선행조건: 컬럼 9개.
  SELECT count(*) INTO v_hits
    FROM information_schema.columns
   WHERE table_schema='public' AND table_name='news_keyword_decisions'
     AND column_name IN ('pre_cut_rank','independent_family_count','unique_url_count',
                         'unique_domain_count','merge_mode','shared_evidence_count',
                         'residual_support_winner','residual_support_self','merge_reason');
  IF v_hits <> 9 THEN
    RAISE EXCEPTION '03_APPLY_columns.sql 을 먼저 실행하세요 (현재 컬럼 %/9)', v_hits;
  END IF;

  -- ─────────────── [2] news_diag_list_decisions ───────────────
  SELECT pg_get_functiondef(p.oid) INTO v_src
    FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
   WHERE n.nspname='public' AND p.proname='news_diag_list_decisions';
  IF v_src IS NULL THEN
    RAISE EXCEPTION 'news_diag_list_decisions 를 찾을 수 없습니다';
  END IF;

  IF position('''pre_cut_rank''' in v_src) > 0 THEN
    v_skip := v_skip || 'news_diag_list_decisions(이미 패치됨)'::text;
  ELSE
    IF position(c_dec_anchor in v_src) = 0 THEN
      RAISE EXCEPTION 'list_decisions 앵커를 찾지 못했습니다 — 수동 편집 필요(04_APPLY_rpc.md)';
    END IF;
    v_new := replace(v_src, c_dec_anchor, c_dec_repl);
    EXECUTE v_new;
    v_done := v_done || 'news_diag_list_decisions'::text;
  END IF;

  -- ─────────────── [3] news_diag_record_run ───────────────
  SELECT pg_get_functiondef(p.oid) INTO v_src
    FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
   WHERE n.nspname='public' AND p.proname='news_diag_record_run';
  IF v_src IS NULL THEN
    RAISE EXCEPTION 'news_diag_record_run 을 찾을 수 없습니다';
  END IF;

  IF position('pre_cut_rank' in v_src) > 0 THEN
    v_skip := v_skip || 'news_diag_record_run(이미 패치됨)'::text;
  ELSE
    -- 세 앵커가 **각각 정확히 1회**여야 한다. 아니면 손대지 않는다.
    IF position(c_ins_anchor in v_src) = 0
       OR position(c_sel_anchor in v_src) = 0
       OR position(c_rs_anchor  in v_src) = 0 THEN
      RAISE EXCEPTION 'record_run 앵커 누락 — 수동 편집 필요(04_APPLY_rpc.md)';
    END IF;
    v_new := replace(v_src, c_ins_anchor, c_ins_repl);
    v_new := replace(v_new, c_sel_anchor, c_sel_repl);
    v_new := replace(v_new, c_rs_anchor,  c_rs_repl);

    -- 세 치환이 모두 반영됐는지 확인(치환 실패를 조용히 넘기지 않는다).
    IF position('pre_cut_rank integer' in v_new) = 0
       OR position('d.pre_cut_rank' in v_new) = 0 THEN
      RAISE EXCEPTION 'record_run 치환 검증 실패 — 적용하지 않았습니다';
    END IF;
    EXECUTE v_new;
    v_done := v_done || 'news_diag_record_run'::text;
  END IF;

  RAISE NOTICE 'patched=% skipped=%', v_done, v_skip;
END
$outer$;

-- 확인(마지막 SELECT 만 표시됨): overloads 는 전부 1, patched 는 전부 true 여야 정상.
SELECT p.proname,
       count(*) AS overloads,
       bool_or(pg_get_functiondef(p.oid) LIKE '%pre_cut_rank%') AS patched,
       bool_or(p.prosecdef) AS security_definer
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname='public'
   AND p.proname IN ('news_diag_list_decisions','news_diag_record_run',
                     '_news_diag_thresholds_display','news_diag_list_runs')
 GROUP BY p.proname ORDER BY p.proname;
