# 04_APPLY_rpc — RPC 2개 교체 (상용 Supabase)

RPC 본문은 저장소에 없고 **DB 안에만** 있습니다. 그래서 이 단계만 전체 파일을 미리 못 만듭니다.
`02_BACKUP_RPC_DEFS.sql` 결과의 `definition` 을 붙여넣고 **아래 두 지점만** 바꾼 뒤 실행하세요.

`CREATE OR REPLACE FUNCTION` 그대로 씁니다. DROP 금지, 기존 반환 키 삭제·개명 금지, GRANT 변경 금지.

## [1] news_diag_list_runs — thresholds_display 계산부

지금은 `collected_candidate_count` 만 남기고 나머지를 버립니다. 진단을 함께 보존하도록:

```sql
jsonb_build_object(
  'collected_candidate_count', r.thresholds -> 'collected_candidate_count'
)
||
CASE WHEN r.thresholds ? 'selection_diagnostics_v1'
     THEN jsonb_build_object('selection_diagnostics_v1',
                             r.thresholds -> 'selection_diagnostics_v1')
     ELSE '{}'::jsonb
END
```

기존 키는 그대로 남으므로 하위호환이 유지됩니다.

## [2] news_diag_list_decisions — 반환 row 구성부

기존 21개 키는 그대로 두고 **추가만** 합니다:

```sql
'pre_cut_rank',             d.pre_cut_rank,
'independent_family_count', d.independent_family_count,
'unique_url_count',         d.unique_url_count,
'unique_domain_count',      d.unique_domain_count,
'merge_mode',               d.merge_mode,
'shared_evidence_count',    d.shared_evidence_count,
'residual_support_winner',  d.residual_support_winner,
'residual_support_self',    d.residual_support_self,
'merge_reason',             d.merge_reason,
```

## [3] news_diag_record_run — INSERT 컬럼 목록

`p_decisions` jsonb 를 풀어 INSERT 하는 부분에 위 9개를 추가합니다.
키가 없으면 NULL 이 되어 **구버전 클라이언트와도 호환**됩니다:

```sql
(x ->> 'pre_cut_rank')::int,
(x ->> 'independent_family_count')::int,
(x ->> 'unique_url_count')::int,
(x ->> 'unique_domain_count')::int,
(x ->> 'merge_mode'),
(x ->> 'shared_evidence_count')::int,
(x ->> 'residual_support_winner')::int,
(x ->> 'residual_support_self')::int,
(x ->> 'merge_reason')
```

## rollback

`02_BACKUP_RPC_DEFS.sql` 로 보관한 `definition` 을 그대로 `CREATE OR REPLACE` 하면 원복됩니다.
컬럼은 되돌릴 필요 없습니다(NULL 허용, 안 읽으면 무해).

---

## ⚠️ PRECHECK 실측 반영 (2026-09-06)

**시그니처를 한 글자도 바꾸지 마세요.** `CREATE OR REPLACE FUNCTION` 은 인자 목록이
다르면 교체가 아니라 **새 오버로드**를 만듭니다. 그러면 PostgREST 가 어느 쪽을 부를지
몰라 모호성 오류를 내고, 진단 조회가 통째로 깨집니다.

실측된 현재 시그니처:

```
news_diag_list_runs(p_since timestamptz, p_until timestamptz, p_status text,
                    p_limit integer, p_offset integer)

news_diag_list_decisions(p_run_id uuid, p_since timestamptz, p_until timestamptz,
                         p_category text, p_reason_code text, p_keyword text,
                         p_limit integer, p_offset integer)

news_diag_record_run(p_run jsonb, p_decisions jsonb)
```

`list_decisions` 는 인자가 **8개**입니다(위 [2] 예시보다 많음). 02 백업 정의를
그대로 붙여넣고 본문만 고치면 자연히 지켜집니다.

**셋 다 `SECURITY DEFINER` 입니다.** 이 키워드를 빠뜨리면 INVOKER 로 바뀌어
테이블 접근 권한을 잃습니다(`news_keyword_*` 직접 SELECT 는 막혀 있음).
`GRANT` 는 `postgres` / `service_role` EXECUTE — **건드리지 마세요**.

교체 후 오버로드가 안 생겼는지 확인(1/1/1 이어야 정상):

```sql
SELECT p.proname, count(*) AS overloads
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'public'
   AND p.proname IN ('news_diag_list_runs','news_diag_list_decisions','news_diag_record_run')
 GROUP BY p.proname ORDER BY p.proname;
```
