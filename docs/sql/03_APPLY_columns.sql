-- =====================================================================
-- 03_APPLY_columns — 쓰기. 상용 Supabase SQL Editor.
-- 이 파일 전체를 그대로 복사해 실행하세요.
--
-- 컬럼 추가만 합니다. 전부 NULL 허용 + 기본값 없음 → 기존 행 파괴 없음.
-- IF NOT EXISTS 라 여러 번 실행해도 안전합니다.
--
-- merge_mode 에 CHECK 를 걸지 않습니다 — 걸면 향후 enum 추가 때
-- 진단 적재 전체가 조용히 실패합니다(RUN_TYPES CHECK 전례).
-- =====================================================================
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

-- 확인(마지막 SELECT 하나만 표시됨): 9건이 나와야 정상.
SELECT column_name, data_type, is_nullable
  FROM information_schema.columns
 WHERE table_schema = 'public' AND table_name = 'news_keyword_decisions'
   AND column_name IN ('pre_cut_rank','independent_family_count','unique_url_count',
                       'unique_domain_count','merge_mode','shared_evidence_count',
                       'residual_support_winner','residual_support_self','merge_reason')
 ORDER BY column_name;
