-- ============================================================
-- [2/3] APPLY — 쓰기. 파일 전체를 복사해 실행하세요. 멱등합니다.
--
-- 목적: news_keyword_decisions.reason_code CHECK 에 CANONICAL_SOURCE_UNGROUNDED 를
--       **추가**한다. 기존 허용값은 하나도 지우지 않는다(과거 row 보존).
--
-- 방식: CHECK 정의를 문자열로 자르지 않는다. 정의 안의 작은따옴표 리터럴을 정규식으로
--       **전부 추출**해 허용값 목록을 얻고, 거기에 새 값을 더해 CHECK 를 새로 만든다.
--       (정의 문자열에 값을 끼워 넣는 방식은 ARRAY[...]::text[] 형태에서 괄호/대괄호를
--        깨뜨린다 — 로컬에서 실제로 깨지는 것을 확인해 이 방식으로 바꿨다.)
--
-- ⚠️ 상용 DB. 실행 전 01_PRECHECK 결과를 확인하세요.
-- ⚠️ 이 SQL 적용 후에도 크롤러는 아직 새 코드를 emit 하지 않습니다
--    (NEWS_DIAG_GROUNDING_REASON 미설정 → OFF). 순서: 02 → 03 → 환경변수.
-- ============================================================

DO $$
DECLARE
  v_conname text;
  v_def     text;
  v_vals    text[];
  v_list    text;
BEGIN
  SELECT con.conname, pg_get_constraintdef(con.oid)
    INTO v_conname, v_def
    FROM pg_constraint con
    JOIN pg_class     rel ON rel.oid = con.conrelid
    JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
   WHERE nsp.nspname = 'public'
     AND rel.relname = 'news_keyword_decisions'
     AND con.contype = 'c'
     AND pg_get_constraintdef(con.oid) ILIKE '%reason_code%'
   LIMIT 1;

  IF v_conname IS NULL THEN
    RAISE NOTICE 'reason_code CHECK 제약이 없습니다 — 자유 text 컬럼이므로 추가 작업 불필요.';
    RETURN;
  END IF;

  IF v_def LIKE '%CANONICAL_SOURCE_UNGROUNDED%' THEN
    RAISE NOTICE '이미 허용됨 — 변경 없음(멱등).';
    RETURN;
  END IF;

  -- 정의 안의 '...' 리터럴을 전부 뽑는다(=현재 허용값 목록).
  SELECT array_agg(DISTINCT m[1] ORDER BY m[1])
    INTO v_vals
    FROM regexp_matches(v_def, '''([A-Z0-9_]+)''', 'g') AS m;

  IF v_vals IS NULL OR array_length(v_vals, 1) IS NULL THEN
    RAISE EXCEPTION '허용값을 추출하지 못했습니다. 수동 처리 필요. 현재 정의: %', v_def;
  END IF;

  -- 안전장치: 지금 저장돼 있는 모든 reason_code 가 추출 목록에 있어야 한다.
  -- (추출이 불완전한 채로 CHECK 를 새로 만들면 과거 row 가 제약 위반이 된다.)
  IF EXISTS (
       SELECT 1 FROM public.news_keyword_decisions d
        WHERE d.reason_code IS NOT NULL
          AND NOT (d.reason_code = ANY (v_vals))
     ) THEN
    RAISE EXCEPTION '추출된 허용값이 기존 저장값을 모두 포함하지 못합니다. 수동 처리 필요. 추출=%', v_vals;
  END IF;

  v_vals := v_vals || 'CANONICAL_SOURCE_UNGROUNDED'::text;

  SELECT string_agg(quote_literal(v) || '::text', ', ' ORDER BY v)
    INTO v_list
    FROM unnest(v_vals) AS v;

  EXECUTE format('ALTER TABLE public.news_keyword_decisions DROP CONSTRAINT %I', v_conname);
  EXECUTE format(
    'ALTER TABLE public.news_keyword_decisions ADD CONSTRAINT %I CHECK (reason_code = ANY (ARRAY[%s]))',
    v_conname, v_list);
  RAISE NOTICE '적용 완료. 허용값 %개: %', array_length(v_vals, 1), v_vals;
END $$;
