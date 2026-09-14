# 01_PRECHECK 10/11 번이 NO 일 때

## 무슨 뜻인가

배포된 `news_diag_record_run` 의 `jsonb_to_recordset(...) AS d(...)` 선언에
`evidence_article_count` 또는 `signals` 가 없다는 뜻이다.

`jsonb_to_recordset` 은 **선언되지 않은 JSON 키를 조용히 무시**한다(예외 아님).
따라서 이 경우에도:

- 진단 적재는 **정상 동작**한다. run 전체가 사라지지 않는다.
- 다만 두 필드의 값이 **버려진다**. 컬럼은 NULL 로 남는다.

`reason_code` / `run_type` 같은 CHECK 제약 위반과는 성격이 **완전히 다르다**.
그쪽은 INSERT 자체가 실패해 그 run 의 decisions 전부가 사라진다(STALE_WRITE_SKIPPED·
UNSAFE_CRIME_ATTRIBUTION 선례). 이번 두 필드는 **기존 컬럼에 들어가는 값**이지
새 enum 값이 아니므로 CHECK 를 건드리지 않는다.

## 그래서 위험한가

PR #35 선배포는 **안전하다**. 최악의 경우가 "값이 안 채워짐"이고, 그건 지금과 같은
상태(두 컬럼 NULL)다. 되돌릴 것도 없다.

## 그럼 무엇을 해야 하나

10/11 이 NO 이면 `02_APPLY.sql` 만으로는 부족하다 — read projection 을 열어도 저장된
값이 없기 때문이다. write RPC 의 recordset 선언에 두 필드를 추가해야 한다.

그 작업은 **이 패키지 범위 밖**이다. 01_PRECHECK 13번이 출력한 write RPC 원본 정의를
가지고 별도로 준비한다(같은 guarded-replace 방식). 임의로 진행하지 말 것.

## 순서

- 10/11 이 **YES** → 02_APPLY → 03_POSTCHECK 로 그대로 진행.
- 10/11 이 **NO**  → write RPC 보강이 먼저다. 그 전까지 02_APPLY 를 실행해도
  해는 없지만(projection 만 열림) 화면에는 NULL 만 보인다.
