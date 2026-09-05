-- =====================================================================
-- news 진단 관측성 — read-only RPC 노출 확장 (2026-09-06)
--
-- 목적: 이미 **저장되고 있는** 진단 데이터를 Admin/분석이 볼 수 있게 한다.
--       새 테이블·컬럼 추가 없음. 저장 구조는 그대로다.
--
-- 배경(실측):
--   - selection_diagnostics_v1 은 news_keyword_runs.thresholds JSONB 에 이미
--     저장돼 있으나, news_diag_list_runs 가 thresholds_display 로 접을 때
--     collected_candidate_count 만 남기고 **버린다**. 그래서 underfill_reason /
--     source_status / rejection_counts / funnel 을 과거 run 에서 볼 수 없었다.
--   - news_keyword_decisions 의 신규 compact 필드(pre_cut_rank, merge_mode 등)는
--     코드 PR 로 저장되기 시작하지만, list RPC 가 고정 컬럼만 반환해 보이지 않는다.
--
-- 안전 조건:
--   - CREATE OR REPLACE 만 사용한다. DROP / 데이터 변경 없음.
--   - 기존 반환 키를 **제거하거나 이름을 바꾸지 않는다** → 기존 consumer 호환.
--   - 권한 범위를 넓히지 않는다: 기존과 동일한 GRANT 대상만 유지한다.
--   - 기사 본문/description 은 애초에 저장되지 않으므로 노출 대상이 아니다.
--     articles 는 기존에도 반환되던 allowlist(title/url/source/published_at/…) 그대로다.
--
-- ⚠️ 실행 환경: **상용(production) Supabase**. 아래 PRECHECK 는 읽기 전용이다.
--    APPLY 는 함수 정의를 교체한다(데이터 불변).
-- =====================================================================


-- ---------------------------------------------------------------------
-- PRECHECK (읽기 전용) — 현재 정의와 권한을 먼저 확인한다.
--   Supabase SQL Editor 는 **마지막 SELECT 결과 하나만** 표시하므로 UNION ALL 로 묶는다.
-- ---------------------------------------------------------------------
SELECT 'proc' AS kind,
       p.proname::text AS name,
       pg_get_function_identity_arguments(p.oid) AS detail,
       p.prosecdef::text AS extra
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'public'
   AND p.proname IN ('news_diag_list_runs', 'news_diag_list_decisions')
UNION ALL
SELECT 'grant',
       routine_name::text,
       grantee::text,
       privilege_type::text
  FROM information_schema.routine_privileges
 WHERE routine_schema = 'public'
   AND routine_name IN ('news_diag_list_runs', 'news_diag_list_decisions')
UNION ALL
-- 저장은 되고 있는데 노출만 안 되는지 확인: 최근 run 의 thresholds 키 목록.
SELECT 'stored_keys',
       r.run_key::text,
       (SELECT string_agg(k, ',' ORDER BY k) FROM jsonb_object_keys(r.thresholds) AS k),
       r.started_at::text
  FROM public.news_keyword_runs r
 ORDER BY 1, 4 DESC NULLS LAST
 LIMIT 50;


-- ---------------------------------------------------------------------
-- APPLY — 아래 두 블록은 **현재 배포된 정의를 기준으로 채워 넣어야 한다**.
--
-- 저장소에는 RPC 정의 SQL 이 없다(DB 안에만 존재). 그래서 여기에 전체 본문을
-- 추측해 적으면 기존 필터/정렬/상한 계약을 조용히 깨뜨릴 수 있다.
-- PRECHECK 로 얻은 현재 정의(pg_get_functiondef)를 그대로 복사한 뒤,
-- **아래 두 지점만** 바꾼다.
--
--   [1] news_diag_list_runs — thresholds_display 계산부
--       현재: 사실상 collected_candidate_count 만 남긴다.
--       변경: selection_diagnostics_v1 을 함께 보존한다.
--
--         thresholds_display := jsonb_build_object(
--           'collected_candidate_count', r.thresholds -> 'collected_candidate_count'
--         ) || COALESCE(
--           jsonb_build_object('selection_diagnostics_v1',
--                              r.thresholds -> 'selection_diagnostics_v1')
--             FILTER (WHERE r.thresholds ? 'selection_diagnostics_v1'),
--           '{}'::jsonb
--         )
--
--       ※ 기존 키(collected_candidate_count)는 그대로 남는다 → 하위호환 유지.
--
--   [2] news_diag_list_decisions — 반환 row 구성부
--       현재: 고정 컬럼 21개만 반환.
--       변경: 새 compact 컬럼을 **추가만** 한다(기존 키 삭제·개명 금지).
--
--         'pre_cut_rank',             d.pre_cut_rank,
--         'independent_family_count', d.independent_family_count,
--         'unique_url_count',         d.unique_url_count,
--         'unique_domain_count',      d.unique_domain_count,
--         'merge_mode',               d.merge_mode,
--         'shared_evidence_count',    d.shared_evidence_count,
--         'residual_support_winner',  d.residual_support_winner,
--         'residual_support_self',    d.residual_support_self,
--         'merge_reason',             d.merge_reason
--
--       ⚠️ 이 컬럼들이 news_keyword_decisions 에 **없다면** 먼저 아래 컬럼 추가가
--          필요하다. 전부 NULL 허용이라 기존 행에 영향이 없다.
-- ---------------------------------------------------------------------

-- [2-a] 컬럼 추가(멱등). 전부 NULL 허용 → 기존 행 파괴 없음, 기본값 없음.
ALTER TABLE public.news_keyword_decisions
  ADD COLUMN IF NOT EXISTS pre_cut_rank             integer,
  ADD COLUMN IF NOT EXISTS independent_family_count integer,
  ADD COLUMN IF NOT EXISTS unique_url_count         integer,
  ADD COLUMN IF NOT EXISTS unique_domain_count      integer,
  ADD COLUMN IF NOT EXISTS merge_mode               text,
  ADD COLUMN IF NOT EXISTS shared_evidence_count    integer,
  ADD COLUMN IF NOT EXISTS residual_support_winner  integer,
  ADD COLUMN IF NOT EXISTS residual_support_self    integer,
  ADD COLUMN IF NOT EXISTS merge_reason             text;

-- merge_mode 는 관찰 enum 이다. CHECK 를 걸면 향후 분기 추가 때 진단 적재 전체가
-- 조용히 실패한다(RUN_TYPES 전례) — 제약을 걸지 않고 애플리케이션 상수로만 관리한다.

-- [2-b] news_diag_record_run 이 새 키를 decisions 에 반영하도록 갱신해야 한다.
--   현재 정의를 PRECHECK 로 뽑아, INSERT ... SELECT 의 컬럼 목록에 위 9개를 추가한다.
--   jsonb 에서 뽑을 때 키가 없으면 NULL 이 되어 과거 클라이언트와도 호환된다:
--     (x ->> 'pre_cut_rank')::int, (x ->> 'merge_mode'), ...


-- ---------------------------------------------------------------------
-- POSTCHECK (읽기 전용) — 노출이 실제로 됐는지 확인한다.
-- ---------------------------------------------------------------------
-- 1) selection_diagnostics_v1 이 RPC 결과에 실리는가
-- SELECT jsonb_object_keys(
--          (news_diag_list_runs(now() - interval '2 days', now(), NULL, 1, 0)
--           -> 'rows' -> 0 -> 'thresholds_display'));
--
-- 2) 새 decision 컬럼이 반환되는가(값은 이 PR 배포 이후 run 부터 채워진다)
-- SELECT jsonb_object_keys(
--          (news_diag_list_decisions('<run_id>'::uuid, 200, 0) -> 'rows' -> 0));


-- ---------------------------------------------------------------------
-- ROLLBACK
--   - RPC: PRECHECK 로 백업해 둔 이전 정의를 CREATE OR REPLACE 로 되돌린다.
--   - 컬럼: 되돌릴 필요가 없다(전부 NULL 허용, 기존 경로가 읽지 않음).
--     굳이 제거하려면 DROP COLUMN 이지만, 그 시점 이후 저장된 진단이 소실되므로
--     권장하지 않는다.
--   - 코드 PR 만 revert 해도 DB 는 안전하다: 새 컬럼은 NULL 로 남을 뿐이다.
-- ---------------------------------------------------------------------


-- =====================================================================
-- 배포 순서 (중요)
--
--   1. 코드 PR merge/배포        — 새 키는 **전송되지 않는다**(기본 OFF).
--                                   수집·계산은 이미 돌지만 payload 에서 제거된다.
--   2. 위 APPLY 실행(상용 DB)     — 컬럼 추가 + record/list RPC 갱신.
--   3. POSTCHECK 로 노출 확인.
--   4. 크롤러 환경변수 설정:
--          NEWS_DIAG_COMPACT_FIELDS=1
--      (GitHub Actions workflow env 또는 저장소 variable)
--      → 이 시점부터 새 필드가 저장되기 시작한다.
--
-- 이 순서를 지키면 어느 단계에서 멈춰도 진단 적재가 깨지지 않는다.
-- 2번을 건너뛰고 4번만 켜면 RPC 가 알 수 없는 키를 받게 되므로 켜지 말 것.
--
-- 되돌리기: NEWS_DIAG_COMPACT_FIELDS 를 지우면 즉시 이전 동작으로 돌아간다
--          (DB 변경을 되돌릴 필요 없음).
-- =====================================================================
