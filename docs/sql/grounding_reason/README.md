# grounding reason_code 오귀속 수정 — 상용 DB 실행 순서

canonical grounding fail-closed 로 탈락한 후보가 `DISPLAY_ARTICLE_INCONSISTENT`
(= display 문구 불일치)로 기록되던 문제를 고칩니다. 새 코드는
`CANONICAL_SOURCE_UNGROUNDED` 입니다.

**코드는 기본 OFF 로 배포됩니다** — `NEWS_DIAG_GROUNDING_REASON` 미설정이면 지금과
완전히 동일한 `DISPLAY_ARTICLE_INCONSISTENT` 를 기록합니다.

| # | 파일 | 성격 |
|---|---|---|
| 1 | `01_PRECHECK.sql` | 읽기 전용 — 파일 전체 복사 실행 |
| 2 | `02_APPLY_reason_check.sql` | 쓰기(CHECK 갱신) — 파일 전체 복사 실행, 멱등 |
| 3 | `03_POSTCHECK.sql` | 읽기 전용 — 파일 전체 복사 실행 |
| 4 | `NEWS_DIAG_GROUNDING_REASON=1` | GitHub Repository variable → 이때부터 새 코드 기록 |

⚠️ **순서 엄수.** 1~3 없이 4만 켜면 RPC INSERT 가 CHECK 위반으로 실패해
**그 run 의 진단 적재 전체가 조용히 사라집니다**(`STALE_WRITE_SKIPPED`·
`UNSAFE_CRIME_ATTRIBUTION` 선례). 랭킹/선정에는 영향이 없습니다.

되돌리기는 4번 환경변수 제거만으로 즉시 — DB 롤백은 필요 없습니다
(CHECK 는 값을 **추가**만 하므로 과거 row 와 기존 코드 경로에 영향이 없습니다).

`01_PRECHECK` 의 3번 섹션에서 최근 7일 `DISPLAY_ARTICLE_INCONSISTENT` 건수를
적어 두면, 4번 적용 후 그 일부가 `CANONICAL_SOURCE_UNGROUNDED` 로 갈라지는 것을
확인할 수 있습니다.
