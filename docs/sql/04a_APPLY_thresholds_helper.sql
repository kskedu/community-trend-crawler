-- =====================================================================
-- 04a_APPLY_thresholds_helper — 쓰기. 상용 Supabase SQL Editor.
-- 이 파일 전체를 그대로 복사해 실행하세요.
--
-- 배포된 정의(02b)를 기준으로 만들었습니다. 바뀐 것은 딱 하나:
--   selection_diagnostics_v1 을 승인 목록에 추가한다.
--
-- 보존한 것:
--   - 시그니처(p_thresholds jsonb) / IMMUTABLE / SECURITY DEFINER / search_path
--   - collected_candidate_count 의 기존 승인 규칙(number + 정수 + int4 범위)
--   - 승인된 키가 하나도 없으면 NULL 반환(기존 계약)
--
-- 바뀐 동작: 진단만 있고 collected_candidate_count 가 없던 run 은 지금까지
--   NULL 이었으나 이제 진단이 실린 객체를 반환한다. 기존 Admin 은 이 키를
--   읽지 않으므로 영향 없다(키 추가일 뿐, 기존 키의 의미/유무는 불변).
-- =====================================================================
CREATE OR REPLACE FUNCTION public._news_diag_thresholds_display(p_thresholds jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 IMMUTABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
AS $function$
DECLARE
  v_out jsonb := '{}'::jsonb;
  v_val jsonb;
  v_num numeric;
BEGIN
  IF p_thresholds IS NULL OR jsonb_typeof(p_thresholds) IS DISTINCT FROM 'object' THEN
    RETURN NULL;
  END IF;

  v_val := p_thresholds -> 'collected_candidate_count';
  IF v_val IS NOT NULL AND jsonb_typeof(v_val) = 'number' THEN
    v_num := (v_val #>> '{}')::numeric;
    -- 정수형(소수부 0) + 안전범위(int4)만 승인.
    IF v_num = trunc(v_num) AND v_num BETWEEN -2147483648 AND 2147483647 THEN
      v_out := jsonb_build_object('collected_candidate_count', v_num::bigint);
    END IF;
  END IF;

  -- selection_diagnostics_v1(2026-09 관측성): 실행단위 진단.
  -- 저장은 계속 되고 있었으나 여기서 버려져 Admin/분석이 볼 수 없었다.
  -- object 일 때만 승인한다(기존 fail-closed 방침과 동일). 내부 구조는 검사하지
  -- 않는다 — 클라이언트가 8KB 상한과 직렬화를 이미 검증하고 보낸다.
  v_val := p_thresholds -> 'selection_diagnostics_v1';
  IF v_val IS NOT NULL AND jsonb_typeof(v_val) = 'object' THEN
    v_out := v_out || jsonb_build_object('selection_diagnostics_v1', v_val);
  END IF;

  IF v_out = '{}'::jsonb THEN
    RETURN NULL;
  END IF;
  RETURN v_out;
END $function$;

-- 확인: 최근 5 run 에서 두 키가 다 나오는지(마지막 SELECT 만 표시됨).
SELECT r.run_key,
       public._news_diag_thresholds_display(r.thresholds) ? 'collected_candidate_count'
         AS has_collected,
       public._news_diag_thresholds_display(r.thresholds) ? 'selection_diagnostics_v1'
         AS has_diag
  FROM public.news_keyword_runs r
 ORDER BY r.started_at DESC
 LIMIT 5;
