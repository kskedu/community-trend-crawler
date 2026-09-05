# 04_APPLY_rpc — 상용 Supabase, 쓰기

02 / 02b 백업을 먼저 확보하세요. 아래는 **실제 배포 정의를 읽고 확정한** 내용입니다.

셋 다 `SECURITY DEFINER` + `SET search_path TO 'pg_catalog', 'public'` 입니다.
`CREATE OR REPLACE` 할 때 이 두 줄을 그대로 유지하세요. 빠뜨리면 권한/해석이 바뀝니다.
`GRANT`(postgres / service_role EXECUTE)는 건드리지 마세요.

시그니처도 한 글자도 바꾸지 마세요 — 다르면 교체가 아니라 **새 오버로드**가 생겨
PostgREST 가 모호성 오류를 냅니다. 백업 정의를 붙여넣고 본문만 고치면 자연히 지켜집니다.

---

## [1] `_news_diag_thresholds_display` — 진단 노출 (헬퍼)

✅ **전체 파일로 준비됨 → `04a_APPLY_thresholds_helper.sql` 을 그대로 복사해 실행하세요.**
이 문서에서 손으로 고칠 것은 없습니다.

⚠️ `news_diag_list_runs` 는 **수정 대상이 아닙니다.** 그 함수는
`_news_diag_thresholds_display(n.thresholds)` 를 부를 뿐이라 헬퍼만 고치면 됩니다.

바뀐 동작 한 가지: 진단만 있고 `collected_candidate_count` 가 없던 run 은 지금까지
`thresholds_display` 가 통째로 NULL 이었는데, 이제 진단이 실린 객체를 반환합니다.
기존 Admin 은 이 키를 읽지 않으므로 영향 없습니다.

---

## [2] `news_diag_list_decisions` — 새 컬럼 반환

`jsonb_build_object(...)` 안의 기존 21개 키는 그대로 두고 **뒤에 추가만** 합니다.
`'created_at', n.created_at` 앞이나 뒤 어디든 됩니다:

```sql
'pre_cut_rank',             n.pre_cut_rank,
'independent_family_count', n.independent_family_count,
'unique_url_count',         n.unique_url_count,
'unique_domain_count',      n.unique_domain_count,
'merge_mode',               n.merge_mode,
'shared_evidence_count',    n.shared_evidence_count,
'residual_support_winner',  n.residual_support_winner,
'residual_support_self',    n.residual_support_self,
'merge_reason',             n.merge_reason,
```

⚠️ 별칭은 `d.` 가 아니라 **`n.`** 입니다(`numbered` CTE 기준). `d.*` 로 흘러오므로
03 을 먼저 실행해 컬럼이 있어야 합니다.

`p_reason_code` 화이트리스트는 그대로 두세요 — 13개 canonical 은 이번에 안 바뀝니다.

---

## [3] `news_diag_record_run` — 새 컬럼 저장

두 군데를 같이 고쳐야 합니다. **하나만 고치면 컬럼 수가 안 맞아 실패합니다.**

INSERT 컬럼 목록 끝에:

```sql
, pre_cut_rank, independent_family_count, unique_url_count, unique_domain_count
, merge_mode, shared_evidence_count, residual_support_winner, residual_support_self
, merge_reason
```

SELECT 목록 끝에(`d.articles` 뒤):

```sql
, d.pre_cut_rank, d.independent_family_count, d.unique_url_count, d.unique_domain_count
, d.merge_mode, d.shared_evidence_count, d.residual_support_winner, d.residual_support_self
, d.merge_reason
```

`jsonb_to_recordset(p_decisions) AS d(...)` 컬럼 정의 목록 끝에:

```sql
, pre_cut_rank integer, independent_family_count integer
, unique_url_count integer, unique_domain_count integer
, merge_mode text, shared_evidence_count integer
, residual_support_winner integer, residual_support_self integer
, merge_reason text
```

`jsonb_to_recordset` 은 **정의 목록에 없는 키를 조용히 무시**합니다.
그래서 이 단계 전에 새 키가 도착해도 에러 없이 버려질 뿐입니다(유실이지 장애 아님).

---

## 교체 후 확인

오버로드가 안 생겼는지(전부 1 이어야 정상):

```sql
SELECT p.proname, count(*) AS overloads
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'public'
   AND p.proname IN ('news_diag_list_runs','news_diag_list_decisions',
                     'news_diag_record_run','_news_diag_thresholds_display')
 GROUP BY p.proname ORDER BY p.proname;
```

그 다음 `05_POSTCHECK.sql` 을 실행하세요.

## rollback

02 / 02b 로 보관한 `definition` 을 그대로 `CREATE OR REPLACE` 하면 원복됩니다.
컬럼은 되돌릴 필요 없습니다(NULL 허용, 안 읽으면 무해).
