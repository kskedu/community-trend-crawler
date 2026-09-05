# news 진단 관측성 — 상용 DB 실행 순서

코드는 이미 배포됨(main `fa6439b`). 새 필드는 **아직 저장되지 않습니다**
(`NEWS_DIAG_COMPACT_FIELDS` 미설정 → OFF).

| # | 파일 | 성격 |
|---|---|---|
| 1 | `01_PRECHECK.sql` | 읽기 전용 — 파일 전체 복사 실행 |
| 2 | `02_BACKUP_RPC_DEFS.sql` | 읽기 전용 — 결과를 rollback 원본으로 보관 |
| 2b | `02b_BACKUP_thresholds_helper.sql` | 읽기 전용 — 진단 노출은 이 헬퍼가 담당 |
| 3 | `03_APPLY_columns.sql` | 쓰기(컬럼 추가) — 파일 전체 복사 실행, 멱등 |
| 4 | `04_APPLY_rpc.md` | 쓰기(RPC 교체) — 02 결과에 두 지점만 반영 |
| 5 | `05_POSTCHECK.sql` | 읽기 전용 — 파일 전체 복사 실행 |
| 6 | `NEWS_DIAG_COMPACT_FIELDS=1` | GitHub Actions env/variable → 이때부터 저장 시작 |

⚠️ 3~4 없이 6만 켜면 RPC 가 모르는 키를 받습니다. 순서 엄수.
되돌리기는 6번 환경변수 제거만으로 즉시(DB 롤백 불필요).

`2026-09-06_news_diag_observability.sql` 은 설계 근거·배경 기록용입니다(실행 대상 아님).
