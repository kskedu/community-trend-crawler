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
