-- ============================================================================
-- 02_APPLY — read RPC projection 보강 (idempotent, fail-closed)
--
-- 하는 일: news_diag_list_decisions 가 돌려주는 row 에
--          evidence_article_count / signals 두 필드를 **추가**한다.
--          기존 필드는 하나도 제거·변경하지 않는다.
--
-- 안 하는 일:
--   - ALTER TABLE 없음. 두 컬럼은 배포 스키마에 **이미 존재**한다(01_PRECHECK 1번).
--   - 권한/보안 속성 변경 없음(정의를 통째로 읽어 문자열 치환 후 재생성).
--   - 랭킹/선정 로직 무관.
--
-- 안전장치: 앵커 'd.article_count' 가 정확히 1곳이 아니면 EXCEPTION 으로 중단한다
--           (함수 구조가 예상과 다르면 아무것도 바꾸지 않는다).
-- ============================================================================
DO $apply$
DECLARE
  v_def    text;
  v_new    text;
  -- ⚠️ 앵커는 live 정의 실측으로 확정했다(2026-09-14 PRECHECK).
  --    read RPC 의 projection 은 CTE alias 가 `n.` 이다(`d.` 아님) — 초안이 'd.article_count'
  --    를 찾다가 0곳으로 중단됐다. key 이름까지 포함한 **완전한 key-value 쌍**을 앵커로
  --    써서 부분 일치("display_article_count" 안의 "article_count")를 배제한다.
  v_anchor text := '''article_count'', n.article_count';
  v_hits   int;
BEGIN
  SELECT pg_get_functiondef(p.oid) INTO v_def
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
  WHERE n.nspname = 'public' AND p.proname = 'news_diag_list_decisions';

  IF v_def IS NULL THEN
    RAISE EXCEPTION 'news_diag_list_decisions 가 없습니다 — 중단(변경 없음).';
  END IF;

  -- 이미 적용됐으면 아무것도 하지 않는다(멱등).
  IF position('evidence_article_count' in v_def) > 0
     AND position('''signals''' in v_def) > 0 THEN
    RAISE NOTICE '이미 적용됨 — 변경 없음.';
    RETURN;
  END IF;

  v_hits := (length(v_def) - length(replace(v_def, v_anchor, ''))) / length(v_anchor);
  IF v_hits <> 1 THEN
    RAISE EXCEPTION
      '앵커(%)가 1곳이어야 하는데 %곳입니다 — 함수 구조가 예상과 다릅니다. 중단(변경 없음). 01_PRECHECK 8번을 확인하세요.',
      v_anchor, v_hits;
  END IF;

  -- projection 에 두 필드만 덧붙인다. 기존 키는 그대로 둔다.
  -- alias 는 live 정의와 동일하게 n. 을 쓴다.
  v_new := replace(
    v_def,
    v_anchor,
    v_anchor || ','
      || E'\n          ''evidence_article_count'', n.evidence_article_count,'
      || E'\n          ''signals'', n.signals'
  );

  IF v_new = v_def THEN
    RAISE EXCEPTION '치환이 적용되지 않았습니다 — 중단(변경 없음).';
  END IF;

  EXECUTE v_new;
  RAISE NOTICE '적용 완료 — evidence_article_count / signals 가 projection 에 추가됨.';
END
$apply$;
