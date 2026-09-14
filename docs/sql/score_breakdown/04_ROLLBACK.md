# 롤백

03_POSTCHECK 중 하나라도 FAIL 이면 되돌린다.

01_PRECHECK **10번 항목**(read RPC 정의 원본)을 그대로 복사해
SQL Editor 에서 실행하면 원래 함수로 돌아간다. `CREATE OR REPLACE FUNCTION`
이므로 권한·보안 속성은 보존된다.

`ALTER TABLE` 을 한 적이 없으므로 테이블 롤백은 필요 없다.
크롤러 코드는 `NEWS_DIAG_COMPACT_FIELDS` 를 끄면 새 필드를 보내지 않는다.
