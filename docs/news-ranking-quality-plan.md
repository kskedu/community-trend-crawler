# 실시간 이슈 랭킹 품질 개선 계획

상태: **완료 (운영 반영 완료, 최신: 2026-07-09)**
관련 원 설계: `docs/news-ranking-plan.md`, `news/ranker.py`, `news/movement.py`, `news/builder.py`

## 0. 최종 결과 요약 (완료 처리, 2026-07-01)

- 커밋: community-trend-crawler `bd9ba3b`, StartHub `f8bfc01` (둘 다 push 완료)
- Codex review-only 검증: 계획 리뷰 2회 + diff 리뷰 9회, 매 회차 P0/P1 즉시 반영, 최종 P0/P1 없음
- 단위 테스트 73개 전체 통과 (`tests/test_news_ranking.py`)
- GitHub Actions `crawl.yml` workflow_dispatch 1회 실행(run id `28511090419`, success, 3m28s) → `news_issue_cache` upsert 성공(10개, `sources=['naver_news','datalab','daum']`)
- 운영/로컬 3001 실데이터로 유사 키워드 dedupe("배재고" ← "배재고 출전정지")와 same-issue merge("모스 탄 명예훼손" ← 관련기사 overlap 기반 5개 키워드 흡수, "비빔밥" ← "단합") 실사례 확인
- 남은 리스크(P2, 실무 투입 지장 없음): incidental mention 판정이 문자열 거리 휴리스틱 기반이라, 구두점 없는 "주체+판촉 이벤트" title 패턴(예: "쿠팡 선풍기 증정 이벤트 진행" — 콤마 없이)은 완벽히 구분되지 않을 수 있음. 무거운 NLP(개체명 인식) 없이는 구조적 한계. 운영 모니터링 대상.
- 후속 개선은 이번 작업과 분리해 별도 계획/리뷰/승인 흐름으로 진행한다(이 문서의 범위는 여기서 종료).

## 0-1. 후속 개선 1차 — same-issue merge 강화 + incidental 필터 강화 (완료, 2026-07-01)

§0 운영 반영 이후 실제 화면에서 확인된 잔여 이슈 2건에 대한 후속 개선.

- 문제: (1) "배재고 출전정지"↔"권오영 감독"처럼 article overlap이 threshold(0.5) 미만이라 놓치는
  same-issue 케이스, (2) "닌텐도 스위치 2" 상세에 경품 나열 기사(BNK경남은행 페스티벌)가 그대로 노출.
- 커밋: community-trend-crawler `290163d`
- Codex review-only 검증: 계획 리뷰 3회 + diff 리뷰 2회, P1 다수 반영(대표적으로 article 그룹 간 공유
  사건 토큰(DF≥2 근사) + keyword anchor 교차 검증 조합으로 merge 신호 추가, `filter_articles_for_display`
  하한 보호 로직 추가) 후 최종 P0/P1 없음
- 단위 테스트 118개 전체 통과
- GitHub Actions workflow_dispatch 1회 실행(run id `28554248444`, success) → `news_top` 저장 10개
- 운영 화면 확인: same-issue merge("배재고 출전정지" ← "권오영 감독" 실사례는 로컬 e2e로 재현 확인,
  운영 화면에서는 사건이 진화해 별도 키워드로 노출된 시점이라 직접 관측은 못 함), incidental 필터는
  "선풍기"/"닌텐도 스위치 2" 기사 relevance_score 정확히 판정되나, `filter_articles_for_display`의
  `min_count` 하한 보호가 진짜 관련 기사 0건인 키워드를 그대로 보충해 화면상 개선 전과 비슷해 보이는
  잔여 이슈 발견 → §0-2로 이어짐.

## 0-2. 후속 개선 2차 — keyword-level quality gate + object/side-mention 필터 (완료, 2026-07-02)

§0-1 운영 반영 이후 실제 화면에서 확인된 잔여 이슈 2건에 대한 추가 후속 개선.

- 문제: (1) "선풍기" 상세 기사 5건이 전부 incidental로 정확히 판정됐지만 keyword 자체를 Top10에서
  거를 gate가 없어 하한 보충으로 여전히 노출, (2) "노트북" 대표 기사가 "쿠팡 국정원 갈등에서 노트북
  회수 조치"만 언급하는 곁가지 기사로 선정(경품 마커로는 못 잡는 별도 패턴).
- 커밋: community-trend-crawler `4c38b0e`
- Codex review-only 검증: 계획 리뷰 5회 + diff 리뷰 2회. 주요 P1: `non_incidental_count`가
  `object_side_mention`(is_incidental=False)에 의해 gate 무력화되는 문제 → relevance 임계값
  기준으로 재정의, quality gate가 `available["news"]` 판정을 꺼서 기존 news-required 로직이
  무력화되는 회귀 → `news_available_before_gate` 사전 확정으로 해결, `_article_overlap`이
  `object_side_mention`을 못 걸러 same-issue merge로 새는 경로 → `_is_same_issue_evidence_article`
  기준 통일, snippet-only side-mention이 `snippet_only_incidental_mention`으로 새는 우회 경로 → 차단.
  최종 P0/P1/P2 없음.
- **스코프 한정(사용자 승인)**: "news 신호 없는 후보가 정규화 입력(rc_raw/delta_raw/g_raw/d_raw)을
  오염시키는" 기존 구조(290163d 이전부터 존재)는 이번 범위 밖으로 분리. `compute_scores()` 정규화
  파이프라인 전체 재작성은 하지 않음. 후속 이슈로 코드 주석(`news/ranker.py` `compute_scores()`)에
  기록만 함.
- 단위 테스트 128개 전체 통과
- GitHub Actions workflow_dispatch 1회 실행(run id `28557093478`, success, 3m7s) → `news_top` 저장
  8개(직전 10개 대비 감소 — quality gate가 저품질 keyword를 실제로 걸러낸 결과로 추정)
- 운영 화면 확인(2026-07-02 09:35 기준 데이터): "선풍기"/"노트북" 모두 이번 Top10에서 완전히 사라짐.
  콘솔 에러 없음, 모바일 overflow 없음. "조희연 수영선수"(배재고 사건이 진화한 후속 인물) 상세 5건
  전부 실제 관련 기사로 정상 노출 확인 — same-issue merge 회귀 징후 없음.

### 후속 관찰 항목 (운영 모니터링, 코드 변경 아님)

- 다음 2~3회 scheduled run에서 `news_top` 저장 개수가 계속 10개 미만(현재 8개)인지 확인한다.
- **계속 8개 이하로 떨어지면** quality gate 완화가 아니라 **후보 pool 확장 또는 backfill 로직
  개선**으로 별도 계획을 세운다(quality gate 자체를 낮추는 방향은 이번 개선의 목적과 상충하므로
  1차 대응에서 제외).
- 지금은 코드 수정 없이 운영 모니터링만 진행한다.

## 0-3. 후속 개선 3차 — sense-mixing(중의적 키워드) 기사 혼입 방어 (완료, 2026-07-09)

운영 화면에서 확인된 새로운 유형의 잔여 이슈: §0~§0-2가 다룬 "완전 무관 기사 혼입"(선풍기 증정,
독일 축구/철학 등)과 달리, 짧고 애매한 keyword가 서로 다른 **의미**의 기사를 함께 흡수하는
sense-mixing 문제.

- 문제 사례: display "위홀 뜻" 아래 이효리/연애전쟁/워홀 커플/조언 기사(실제 이슈)와 앤디워홀/
  미술관/대구/전시 기사(무관 동음이의 콘텐츠)가 함께 노출. "위홀"이라는 짧은 문자열이 두 의미
  모두에 substring 토큰으로 매칭돼 relevance_score가 둘 다 높게 나오는 게 근본 원인. 검색의도
  suffix("뜻"/"의미"/"누구" 등)가 붙은 raw keyword가 그대로 display로 노출되는 문제도 함께 확인.
- PR: community-trend-crawler [#1](https://github.com/kskedu/community-trend-crawler/pull/1)
  (squash merge, main 반영 커밋 `bf7236a`)
- worktree: 별도 worktree(`fix/news-top-sense-mixing` 브랜치)에서 진행, merge 후 원격 브랜치 삭제
- 수정 내용:
  - `news/candidates.py` `mark_off_primary_sense()`(신규) — non-primary cluster 중 keyword/primary
    어느 근거로도 "같은 의미"임을 확인할 수 없는 기사에 `is_off_primary_sense` 플래그 부여
  - `_display_anchor_allowed()` 강화 — 위 플래그로 조기 차단(기존 단일 고유토큰 예외는 먼저
    평가해 보존 — "장동건"류 정상 단일 인물명 케이스 회귀 방지)
  - `news/ranker.py` 검색의도 suffix(뜻/의미/누구/프로필/나이/인스타/결혼/근황/학력/직업) display
    방어 추가(어절 단위 정확 일치, substring 오탐 없음)
  - `resolve_singleton_displays()`(신규) — merge 안 된 단독 keyword의 display를 표시 기사 공통
    토큰 기반으로 재구성(vacuous 재구성 방지 조건 포함)
  - `main.py` / `news/dryrun.py` 파이프라인에 `resolve_singleton_displays()` 호출 연결
- Codex review-only 검증: 계획 리뷰 4회(설계 확정 과정에서 P1 지적 반영 — primary cluster 오염
  자체는 이번 범위 밖으로 명시적 제외) + diff 리뷰 5회(P1 1건 + P2 3건 + P3 1건 순차 반영, 최종
  No findings) + **PR 기준 최종 review-only 1회 추가**(merge 직전, 독립적으로 재검토)
- 단위 테스트 244개 전체 통과(기존 232 + 신규 12, `TestSenseMixingDisplay`)
- GitHub Actions workflow_dispatch 1회 실행(run id `28984705150`, `mode=news_top_only`, success,
  약 11분 소요) → `news_top` 저장 10개(`sources=['naver_news','datalab','bing_home','daum_home',
  'google_trends','nate_home']`)
- 운영 화면 확인(StartHub, 검증용 별도 vercel dev 인스턴스 + Playwright): `.news-brief-row` 10건
  정상 렌더, 콘솔 에러 없음, 모바일(375×812) overflow 없음. "위홀 뜻" 사례 자체는 이번 실행 데이터에
  없었으나(특정 시점 데이터라 매 실행 재현 안 됨), 같은 로직이 "태풍"(무관 사극 기사 1건),
  "꽃게"(등대주간 AR체험 기사 2건), "하이닉스 주가"(삼성전자 실적 비교 기사 2건),
  "스마일게이트"(예술기업 투자 기사 2건) 등 실제 무관 기사 7건을 `is_off_primary_sense`로 정상
  감지해 `display_articles`에서 제외함을 확인 — 로직이 운영 데이터에서 실효성 있게 작동.
  PR/광고 클러스터, "신임"/"수사"/"투자" 단독 display, suffix 단독 display 노출 전부 없음.

**남은 리스크(known limitation)**: keyword가 단일 토큰이고, non-primary cluster가 그 keyword와
**동일한 문자열**을 공유하는 진짜 동음이의 케이스(예: keyword 원문 자체가 "워홀"이고 앤디워홀
기사 원문도 정확히 "워홀"인 경우)에서는 sense-mixing이 남을 수 있다. 이번 PR 최종 검토에서
규칙 기반 수정을 3가지 시도했으나 모두 "장동건"류 정상 단일 고유명사 케이스를 회귀시켜, 순수
토큰 집합 비교만으로는 "같은 개체의 표현 차이"와 "동음이의 문자열"을 안정적으로 구분할 수 없음을
확인하고 되돌렸다. 사용자가 제시한 실제 사례(keyword 원문 "위홀" vs 앤디워홀 원문 "워홀")는
문자열 자체가 달라 이번 PR로 정상 방어된다. 근본 해결(select_primary_cluster 개선, 형태소
분석/개체명 인식 도입)은 후속 이슈로 분리해 유지한다(닫지 않음):
[#2](https://github.com/kskedu/community-trend-crawler/issues/2).

### 후속 과제 — 단일 토큰 동음이의 sense 탐지 (1차 탐지 착수, 2026-07-10)

상태: 1차 = logging first(탐지·로그만) 구현. display 소비(단일 토큰 예외 자격 조건
강화)/primary 선택 보조 신호는 운영 로그 관찰 후 별도 PR로 분리.

- 위 known limitation을 fixture로 재현해 실제 누수를 확인했다: keyword="워홀 뜻"/"워홀"에서
  앤디워홀 클러스터 공통 토큰에 동일 문자열 "워홀"이 들어가 `mark_off_primary_sense`의
  keyword 매칭이 same_sense로 오판하고(`off_primary_sense_count=0`), 앤디 기사도
  relevance 0.9(keyword_main_topic)라 `_display_anchor_allowed`의 단일 토큰 예외(장동건류
  보존, off-sense 체크보다 먼저 평가)까지 통과해 display_articles에 혼입된다.
- 판별 신호는 토큰 집합이 아니라 **dominant collocation**: 앤디워홀 클러스터에서 "워홀"의
  exact 토큰 등장은 전부 "앤디" 바로 뒤(합성 고유명의 일부)이고 partner("앤디")가 primary
  기사에 미등장. "장동건"은 인접 토큰이 기사마다 달라 일관 partner가 없다(§0-4 prev-token
  modifier와 같은 신호 계열).
- 1차 구현: `ranker.detect_homonym_entity_singletons()`가 final(top)의 단일 토큰 core
  후보에 대해 표시 기사 집합(실제 노출 파이프라인과 동일 산출)의 non-primary 묶음별
  collocation partner(prev/next 양방향, 전 등장 일치+2회 이상+non-generic·비역할명+
  primary 미등장, primary 등장 시 same-sense 증거로 클러스터 veto)를 shadow 탐지하고
  `main._rank_and_select`가 로그만 남긴다. 진단에 `would_exclude_display_count`/
  `would_drop_candidate_by_display_min`(후속 hard exclude 판단용)과 `primary_suspect`
  (primary가 뒤집힌 케이스 관찰)를 포함한다. **final 결과/저장 payload 완전 불변**
  (진단이 별도 리스트로만 존재 — article/news_meta 무오염, 테스트로 no-op 동치 및
  builder payload 동일성 검증).
- Codex 계획 review-only 3라운드: 1차 P1 3건(역할명 접두 오탐/next 미탐/primary 뒤집힘)
  → logging-first 하향, 2차 P1 2건(payload 누출 경로/would_* 산출 위치) → 탐지를
  ranker detect + final(top) 호출로 이동, 3차 P0/P1 없음("구현 진행 가능").

### 후속 관찰 항목 (운영 모니터링, 코드 변경 아님)

- 다음 2~3회 scheduled/workflow_dispatch run에서 sense-mixing(다른 의미 기사 혼입) 재발 여부를
  운영 화면에서 관찰한다.
- PR/광고성 클러스터 노출 여부, "신임"/"수사"/"투자" 등 generic 단독 display 노출 여부를 함께
  관찰한다(이번 개선이 기존 방어를 약화시키지 않았는지 확인).
- news_top Top10 유지 여부(개수 감소 없이 10개 유지되는지)를 확인한다 — off_primary_sense 필터가
  과도하게 작동해 정상 기사까지 걸러내면 개수가 줄 수 있다.
- 이상 징후가 지속되면 issue #2의 "근본 해결 방향" 섹션에 관찰 내용을 추가하고, 코드 수정이
  필요한지는 별도 계획으로 판단한다. 지금은 코드 수정 없이 운영 모니터링만 진행한다.

## 0-4. 후속 개선 4차 — 짧은 일반 생활명사 단독 display 보강 (완료, 2026-07-09)

§0-3 운영 반영 이후 실제 화면에서 확인된 잔여 이슈: rank 8에 display/canonical="안경"
단독이 노출됐는데, 실제 표시 기사는 "AI 안경 체험"/"AI 안경 시스템"처럼 더 구체적인 phrase로
반복됐다. singleton display 재구성(`_resolve_singleton_display`)이 검색의도 suffix(뜻/의미/
누구) 케이스만 다뤄, "안경" 같은 짧은 일반 생활명사 단독은 보강 사각지대였다.

- 처리: `_boost_short_generic_singleton_display` 신규(news/ranker.py). singleton keyword가
  단일 토큰·3자 이하·non-generic이고, 표시 기사(dedup→filter_articles_for_display→
  [:ARTICLES_MAX]) title에서 keyword 바로 앞에 오는 **영문/숫자 포함 modifier**가 keyword
  등장 기사 절반 이상 **AND 최소 DISPLAY_ARTICLES_MIN(2)건**에서 반복되면 display를
  "{modifier} {keyword}"로 보강한다("안경"→"AI 안경").
- 방어: canonical keyword 불변(display_keyword만 변경), 뒤 사건어 미부착("AI 안경"까지만),
  순수 한글 문맥어("제주 태풍"/"은행 금리") 제외, 중복형("오픈AI AI", casefold) 차단,
  18자 초과 원형 유지, 절대 근거 부족 시 미보강(Top10 개수 감소 방지). 기존 suffix 경로 보존.
- 검증: 전체 316개 테스트 통과(신규 23개). Codex 계획 리뷰 3라운드 + diff 리뷰 3라운드에서
  P0/P1 전부 해소. gate/merge threshold/PR hard exclude/sense-mixing/generic 방어 불변.
- 커밋: `fix: 짧은 일반 생활명사 단독 display를 기사 공통 수식어로 보강`.

### 후속 과제 — broad category(업종/분야) generic singleton 방어 (1차 탐지 착수, 2026-07-09)

**상태: 1차 = logging first(탐지·로그만) 구현. hard exclude/강등은 후속 PR로 분리.**

§0-4 hotfix는 "짧은 단일 생활명사 + 앞 영문/숫자 수식어 반복"만 다룬다. 이와 별개로,
**순수 한글 업종/분야어("건설" 등)가 서로 다른 주체의 기사를 하나로 묶어 단독 display로
노출되는 문제**가 관측됐다. 이번 안경 작업 범위에 섞지 않고 후속 과제로만 기록했다가,
1차로 **탐지·관찰 로그만** 추가했다.

- 1차 구현(news/ranker.py, main.py): `_BROAD_CATEGORY_WORDS` 상수 +
  `detect_broad_category_singletons(items)` 신규. singleton(merge X)·단일 토큰·사전 포함·
  `display_keyword==keyword`(§0-4 보강분 제외)인 후보를 잡아, 표시 기사(dedup→filter→
  [:ARTICLES_MAX]) 기준 **subject dispersion(주체 분산)을 shadow로 계산**해
  `logger.warning`으로만 남긴다(keyword/display/기사 수/주체 분포/shadow_dispersed).
  `_rank_and_select`에서 final(top) 확정 뒤 호출 — **제외/강등/순위/저장 개수 전부 불변**.
- 1차에서 제외/강등하지 않는 이유(Codex 계획 review-only P1): "title 첫 토큰 = 주체"
  추출이 [속보]/인용/날짜/기관어 접두 등에 취약해 hard exclude 오탐 위험이 큼. 운영
  1~2회 로그로 dispersion 판정이 실제로 "건설/게임"을 잡고 "태풍/금리"를 안 건드리는지
  검증한 뒤, 제외/강등 기준을 후속 PR에서 확정한다(false positive가 false negative보다 위험).
- 후속 PR(미착수): shadow dispersion 로그 검증 → 제외/강등 기준 확정, dominant phrase
  fallback(고도화). 아래 원 문제 정의/개선 방향은 후속 PR 설계 근거로 보존한다.

#### 원 문제 정의(보존)

- 문제 사례(2026-07-09 운영 관찰):
  - display/canonical="건설", 상세 기사:
    - "현대건설, 건설안전 스타트업 12개사와 협업 성과 공유"
    - "대우건설, 이라크 국가전략사업서 빛난 대우건설, 가덕도로 이어진다"
  - 두 기사의 공통점은 "건설"뿐 — 동일 이슈가 아니라 서로 다른 건설사의 별개 사건 묶음.
- 현재 상태: "건설"은 §0-4 대상 아님(순수 한글이라 영문/숫자 modifier 없음 → 보강 안 됨),
  `_DISPLAY_GENERIC_WORDS`/`exclude_generic_singletons`에도 없어(행위·인사 서술어 위주)
  final에 그대로 노출될 수 있다.
- 후속 개선 방향(설계 단계, 이번 미구현):
  1. 업종/분야 generic singleton 방어 검토.
  2. "건설/금융/은행/병원/기업/산업/사업/정책/시장/기술" 같은 넓은 분야어가 단독 display로
     final에 들어오면 감점 또는 제외.
  3. 단, "태풍"/"주담대"/"하이닉스 주가"처럼 단독·조합으로 명확한 이슈는 유지(오탐 방지 우선).
  4. 기사들이 동일 회사/동일 사건/동일 phrase로 묶이지 않으면 broad category singleton으로
     보고 제외.
  5. 더 구체적인 dominant phrase가 있으면 display fallback(건설 → "현대건설 안전기술" /
     "대우건설 이라크 사업").
  6. dominant phrase도 없고 서로 다른 기사면 final에서 제외.
- 제약(설계 단계부터 고정): gate/merge threshold 완화 금지, filler로 10개 채우기 금지,
  "태풍/주담대"류 정상 단독어 오제외 금지(false positive가 false negative보다 위험).
  broad 분야어를 고정 사전으로 관리할지, 기사 주체 분산도(서로 다른 회사명 비율) 같은
  데이터 기반 신호로 판정할지는 계획 단계에서 별도 결정한다.

## 1. 배경

통합 랭킹(News/DataLab/Google/Daum 신호 합성) 적용 후에도 화면 품질 이슈가 남아 있다.

1. 유사 키워드 중복 노출 (배재고등학교 / 배재고)
2. 대표 문구가 키워드의 핵심 이슈와 무관한 기사에서 선택됨 (독일 → 불교/철학 기사)
3. 표면적으로 다른 키워드가 사실은 같은 사건(같은 기사 클러스터)을 가리킴 (압수수색/김영환/공수처)
4. 키워드가 기사에 등장은 하지만 중심 주제가 아닌 경우 대표로 잘못 선택됨 (선풍기 → 증정/판촉 기사)
5. title에는 없고 description에만 우연히 등장한 키워드가 완전히 무관한 기사를 대표로 끌어옴 (배재고등학교 야구부 → 손흥민 귀국)

다섯 문제 모두 "최종 Top10 확정 전, 후보/기사 단계에서" 처리해야 서로 얽히지 않는다. 하나의 작업으로 묶어 진행한다.

## 2. 현재 구조 (조사 결과)

### 파이프라인 순서 (main.py `run_news_briefing`)

```
1. collect_candidates()      # daum/danawa/google/aux 병합 → pool
2. build_news_signals()      # 키워드별 news 신호 + normalized articles
3. datalab/google/daum 신호 수집
4. ranker.compute_scores()   # 신호 합성 → score, 내림차순 정렬
5. ranker.select_top(10)     # 상위 10개 컷
6. build_ranked_issues()     # entry 조립 (summary, articles dedup by url)
7. apply_movement()          # 이전 Top10과 비교해 movement 주입
8. enrich_issue_thumbnails() # og:image 보강
9. upsert_news_issues()      # news_issue_cache 저장
```

**dedupe/merge/representative 선택이 들어갈 자리는 4~5 사이(ranker 책임)** — score까지 계산된 뒤, Top10을 자르기 전에 유사 키워드 dedupe와 same-issue merge를 적용하고, 빈 자리를 다음 후보로 채운 다음 select_top 해야 한다. movement(7)는 이 최종 Top10을 받아야 하므로 손대지 않아도 순서가 맞다.

### 관련 파일 실제 역할

| 파일 | 현재 역할 | 이번 작업 관련 여부 |
|---|---|---|
| `news/ranker.py` | score 계산(순수함수), `select_top` | **핵심 수정 대상** — dedupe/merge/backfill을 select_top 전에 삽입 |
| `news/candidates.py` | 후보 병합, News 신호 산출(`compute_news_signal`), `_tokens` 재사용 | **수정** — article relevance/clustering 계산 추가 지점으로 적합 |
| `news/summarizer.py` | 규칙 기반 대표 요약(`summarize`), `_tokens` 토크나이저 보유 | **수정** — 클러스터링에 `_tokens` 재사용, representative 선택 로직과 역할 조율 필요 |
| `news/normalizer.py` | 네이버 응답 1건 → article dict(title/url/press/thumbnail/snippet). URL 매칭 기준 안전 확인됨 | 원칙적으로 변경 불필요 — field 섞임 버그(요구사항 5-6) 재현 안 됨(단일 raw item에서 파생) |
| `news/movement.py` | 순수함수, Top10 비교만. previous 상태 안 가짐 | **변경 금지** — 호출 순서만 유지(merge 이후 top10을 받음) |
| `news/builder.py` | Top10 조립 책임만 (rank/summary/articles) | **원칙적으로 변경 최소화** — articles 정렬을 primary cluster 우선으로 바꾸는 정도만, dedupe/merge 로직은 넣지 않음(책임 혼합 방지) |
| `news/dedup.py` | URL 기준 기사 중복 제거만 (15줄) | 그대로 유지, 키워드 dedupe는 별도 함수로 분리 |
| `tests/test_news_ranking.py` | ranker/candidates/datalab/google/builder/movement 단위테스트(unittest) | **테스트 추가** |
| `news/fixtures/naver_news.json` | 키워드별 mock 뉴스 응답 | **fixture 추가**(독일 축구/철학 혼합, 선풍기 증정 혼입, 배재고/손흥민 혼입 등) |

### 저장 스키마

`news_issue_cache.issues` (jsonb) 안의 `keywords[]` item에 optional 필드를 추가하는 방식 — **DDL 변경 없음**. `representative_title` / `representative_summary` / `representative_article` / `primary_cluster_size` / `topic_coherence` / `related_keywords` / `aliases` / `display_keyword` / `merge_reason` / `relevance_score` / `relevance_reason` / `is_incidental` / **`sources`(merge 후 candidate lookup 실패 방지용, §7-3)** 모두 item 레벨 optional 필드로 추가.

### 프론트 (StartHub)

- `js/news-brief.js` `_headline(k)`: 현재 `articles[0].title → summary` 순서. **요구사항 순서(representative_summary → representative_title → article title)와 다름 → 수정 필요**.
- `articles.length` 숫자 배지 반복 표시 로직은 **현재 코드에 없음** (조사 결과 확인됨) → 요구사항 항목이지만 실제로는 손댈 것 없음, 회귀 방지 관점에서만 유지 확인.
- movement 배지(`_movementBadge`)는 기존 유지, 변경 불필요.
- 상세 바텀시트(`_renderDetail`): articles 배열 순서를 그대로 렌더 → crawler가 정렬만 primary cluster 우선으로 넘겨주면 프론트 변경 없이 반영됨. 단, incidental mention 기사를 하단/제외하는 기준이 있다면 그 정렬 순서 반영 여부만 확인.
- `tools/check.js`: 스키마 검증 성격이면 새 optional 필드 추가에 맞춰 확장 필요 여부 확인(있다면).

## 3. 작업 단위 분리 (한번에 묶을 것 / 따로 갈 것)

다섯 개선 대상은 실제로는 "같은 파이프라인 단계(키워드 dedupe → 기사 clustering/relevance → representative 선택 → same-issue merge)"를 공유하므로 **하나의 작업으로 묶어 순차 구현**하되, 내부적으로는 아래 순서로 단계를 나눠 진행한다(구현 순서 = 리스크 낮은 것부터):

1. **article relevance 필터** (개선 4, 5) — 키워드-기사 중심성 판정. 다른 항목들의 전제(어떤 기사가 "진짜 관련 기사"인지)가 되므로 가장 먼저 필요.
2. **경량 clustering + representative 선택** (개선 2) — relevance 위에서 primary cluster를 뽑는 단계.
3. **유사 키워드 dedupe** (개선 1) — 키워드 문자열/기관명 유사도, Top10 채우기.
4. **same-issue merge** (개선 3) — article overlap 기반 병합, display_keyword 조합. 1~2에서 만든 relevance/cluster 정보를 재사용.

즉 코드 변경은 하나의 브랜치/커밋 세트로 진행하되, 구현 스텝은 4→2→1→3 순서(의존관계 순)로 진행한다. 각 스텝마다 단위 테스트를 추가해 스텝별로 검증 가능하게 한다.

## 4. 예상 allowed files (최종 목록)

### community-trend-crawler

| 파일 | 변경 종류 |
|---|---|
| `news/ranker.py` | dedupe/merge/backfill을 `select_top` 전 단계로 추가 (신규 함수, 기존 `compute_scores` 반환 계약 유지) |
| `news/candidates.py` | article relevance 계산, 경량 clustering(topic_coherence), representative 후보 산출 로직 추가 |
| `news/summarizer.py` | 대표 선택 로직과 `_tokens` 공유 방식 조율 (representative_title/summary 산출 함수 추가 또는 이관) |
| `news/builder.py` | 신규 optional 필드(representative_*, primary_cluster_size, topic_coherence, related_keywords 등)를 entry에 실어 나르는 부분만 추가. articles 정렬을 relevance/primary cluster 기준으로 조정. **dedupe/merge 로직 자체는 넣지 않음** |
| `news/movement.py` | 원칙적으로 미수정. 호출 시점(merge 이후)만 유지되는지 main.py에서 확인 |
| `news/normalizer.py` | 원칙적으로 미수정(필요 시에만, field 섞임 버그 재확인 후 판단) |
| `news/dedup.py` | 원칙적으로 미수정(URL 기준 기사 dedupe는 그대로 유지) |
| `main.py` | `run_news_briefing()` 호출 순서 조정(신규 dedupe/merge 단계 삽입 지점) — 필요 시 |
| `tests/test_news_ranking.py` | 신규 테스트 케이스 추가(테스트 케이스 1~9 + 추가 5건) |
| `news/fixtures/naver_news.json` 및 신규 fixture | 배재고/배재고등학교, 압수수색/김영환/공수처, 독일(축구/철학), 선풍기(증정 혼입), 배재고등학교 야구부(손흥민 혼입) 시나리오 fixture 추가 |

### StartHub

| 파일 | 변경 종류 |
|---|---|
| `js/news-brief.js` | `_headline()` fallback 순서를 representative_summary → representative_title → article title로 변경. `_renderDetail()`은 articles 정렬 반영 확인(로직 변경은 최소) |
| `css/news-brief.css` | 필요 시에만(신규 배지/레이아웃 없으면 미수정 가능성 높음) |
| `tools/check.js` | 신규 optional 필드 관련 검증이 필요하면 확장, 아니면 미수정 |

## 5. 설계 원칙 재확인 (사용자 지정 제약 반영)

- movement 계산은 dedupe/merge 이후 최종 Top10 기준 — main.py 호출 순서에서 `apply_movement`는 그대로 마지막(merge 후)에 위치, 이 부분은 이미 현재 순서와 일치하므로 **merge 단계를 select_top 이전에 끼워 넣기만 하면 됨**.
- builder는 Top10 엔트리 조립 책임만 유지, dedupe/representative selection/same-issue merge는 ranker(or candidates) 책임.
- movement.py는 순수함수 유지, previous 상태 주입 없음 — 미수정.
- 무거운 NLP/형태소 분석기 도입 금지 — 기존 `summarizer._tokens`(정규식 기반 토크나이저) 재사용, Jaccard/token overlap만 사용.
- 네이버 API 실호출/GitHub Actions 실행/운영 DB write/DDL 변경/secret 변경/push 전부 금지 — fixture와 로컬 테스트로만 검증.

## 6. 리스크 / 주의사항

- **score 재계산 없는 dedupe**: 유사 키워드 중 대표만 남길 때 "score가 더 높은 쪽"을 남기므로, 제거된 키워드의 candidate/신호가 대표에 흡수되지 않도록(related_keywords에 텍스트만 보존, score 합산 금지) 범위를 명확히 해야 함 — 그렇지 않으면 score inflation으로 랭킹 왜곡 가능.
- **same-issue merge와 dedupe의 순서**: 문자열 유사(개선 1)와 article overlap(개선 3)이 겹치는 후보가 있을 수 있음 — 처리 순서를 dedupe 먼저(문자열/기관명 기준) → merge 나중(콘텐츠 기준)으로 명확히 정의해 이중 처리 방지.
- **relevance 필터가 너무 공격적이면** 기사 수가 줄어 `ARTICLES_MIN`(5) 하한을 못 채우는 키워드가 늘 수 있음 — relevance 낮은 기사도 배열엔 유지하되 정렬/대표 선택에서만 배제하는 현재 요구사항 방향이 맞음(제외 임계값은 신중히, 기본은 재정렬만).
- **substring containment 오탐**: "독일", "사원", "기흥"처럼 넓은 단어는 절대 단순 substring merge 금지 — 별도 화이트/블랙리스트가 아니라 "포함관계 + 글자 수 비율" 같은 정량 기준으로 판단(예: 짧은 쪽이 너무 일반적인 단독 명사면 제외).
- **테스트 fixture 늘어나는 만큼 회귀 테스트 시간 증가** — 크지 않지만 CI 없는 로컬 unittest라 문제 없음.

## 7. 처리 순서 (요구사항 원문 반영, 확정 — Codex 1차 리뷰 반영으로 세분화)

1. candidate pool 생성
2. **relevance 계산** — `candidates.py` `build_news_signals()` 단계에서 기사별 `relevance_score`/`relevance_reason` 산출 (score 계산 *전*. 기존 `title_relevance` penalty가 이미 score에 반영되므로, 계산 순서를 score 뒤로 미루면 score에 반영되지 않는 모순 발생 — Codex P1 반영)
3. **score 계산** — relevance를 반영한 news_meta 기준으로 `ranker.compute_scores()` 실행 (기존 `title_relevance` 기반 penalty는 유지하되, 신규 relevance_score와 정합성 확인)
4. keyword별 articles clustering(topic_coherence) — relevance 높은 기사 우선으로 클러스터링
5. representative_title / representative_summary / representative_article 산출 — primary cluster + relevance 상위 기사에서만 선택
6. 유사 키워드 dedupe — score 더 높은 대표만 남기고 제거된 키워드는 `related_keywords`에 텍스트만 보존(점수 합산 금지)
7. same-issue merge — article overlap 기반. merge 결과의 `keyword`(canonical, movement 비교용)와 `display_keyword`(화면 노출용)를 분리 유지(아래 7-1 참조)
8. 최종 Top10 확정 — dedupe/merge로 줄어든 자리는 전체 후보 리스트를 다시 순회하며 이미 처리된 cluster/alias와 재중복되지 않는 다음 후보로 backfill(아래 7-2 참조)
9. movement 계산 — canonical `keyword` 기준으로 비교(merge로 표시가 바뀌어도 movement 안정)
10. news_issue_cache 저장
11. 프론트 표시

### 7-1. canonical keyword vs display_keyword (Codex P1 반영)

- `movement.py`는 `keyword` 문자열로 이전/현재 Top10을 비교한다. same-issue merge로 대표 키워드가 바뀌면(예: "압수수색" → "김영환 압수수색") 매 실행마다 canonical key가 달라져 movement가 전부 `new`로 오판될 위험이 있다.
- 해결: merge/dedupe 후에도 `keyword` 필드는 안정적인 canonical 값으로 유지하고(예: merge 전 가장 먼저 선택된 대표 후보의 원래 keyword, 또는 score 최고 후보의 keyword), 사건 맥락을 담은 조합형 표기는 `display_keyword`에만 넣는다.
- `builder.py`의 entry 조립과 `news-brief.js` 프론트 표시는 `display_keyword`(없으면 `keyword`)를 우선 노출한다.
- `movement.py`는 여전히 `keyword`(canonical)만 보고 비교 — **movement.py 자체는 미수정**.

### 7-2. backfill 시 재중복 방지 (Codex P1 반영)

- 단순히 "빈 자리 수만큼 다음 후보를 채운다"고 하면, 새로 채운 후보가 이미 dedupe/merge 처리된 alias나 cluster와 다시 겹칠 수 있다.
- 해결: dedupe/merge 로직은 `ranked` 리스트 전체를 순회하며 "이미 선택된 keyword/alias/cluster 집합"을 계속 갱신하는 단일 루프로 구현한다(2-pass가 아니라 selected-set 누적 방식). Top10이 채워질 때까지 이 루프를 이어간다.

### 7-3. merge 후 candidate/source 메타 보존 (Codex P1 반영)

- `builder.py`의 `build_ranked_entry()`는 `candidate_map[item["keyword"]]`로 daum/google 신호를 조회한다. merge로 `keyword`가 조합형으로 바뀌면 이 lookup이 깨진다.
- 해결: merge된 ranked item 안에 원본 후보들의 `sources`(또는 대표 후보의 sources)를 직접 실어서 반환하고, `builder.py`는 `candidate_map` lookup 실패 시에도 깨지지 않도록 item에 실린 sources를 우선 사용한다. canonical `keyword`를 유지하는 7-1 방침과 함께라면 대부분의 경우 lookup은 그대로 작동하지만, 방어적으로 item 내장 sources를 신뢰 우선순위 1순위로 둔다.
- **저장 계약 명시(Codex 2차 리뷰 P1 반영)**: 이 `sources`는 `news_issue_cache.issues.keywords[]` item의 optional 필드로도 저장/전달된다(§2 저장 스키마 목록에 반영 완료). ranked item 내부 임시 값이 아니라 builder가 최종 entry에 실어 내보내는 필드로 고정한다.

### 7-4. 토크나이저 공유 방식 (Codex P2 반영)

- `summarizer._tokens`(private, `_` prefix)를 `candidates.py`가 이미 재사용 중이고, 이번에 representative/clustering까지 얹으면 private 함수 의존이 더 늘어난다.
- 신규 파일을 늘리지 않는 범위에서는 `summarizer.py`에 `_tokens`를 공개 함수(`tokenize` 등)로 승격하거나, 현재처럼 `_tokens`를 계속 재사용하되 이번 작업 안에서 새로 만드는 clustering/relevance 함수는 모두 `candidates.py`에 모아 책임 소재를 한 파일로 좁힌다. **별도 `news/text.py` 신설은 이번 스코프에서는 보류**(범위 확장 방지 원칙과 상충 — 필요성이 명확해지면 별도 승인 후 진행).

## 8. 다음 단계 (완료됨)

1. ~~계획 리뷰(1차/2차)~~ — 완료, P0 없음 / P1 5건 전부 반영(§7-1~7-3, §9, §10)
2. ~~구현(candidates.py/ranker.py/builder.py/main.py/news-brief.js)~~ — 완료
3. ~~diff 리뷰 9회(구현 후)~~ — 완료. 매 회차 P1/P2 즉시 수정, 최종 P0/P1 없음(§11 참조)
4. ~~단위 테스트 73개 통과 + StartHub npm run check 통과~~ — 완료
5. ~~커밋 (crawler bd9ba3b / StartHub f8bfc01)~~ — 완료
6. ~~운영 반영: push → Vercel 배포 확인 → workflow_dispatch 1회 → DB upsert 확인 → 운영 화면 확인~~ — 완료(§0 참조)

후속 개선(§0의 남은 P2 등)은 이 계획서 범위 밖 — 필요 시 별도 계획서로 새로 시작한다.

## 9. Codex 1차 리뷰 결과 요약

- P0: 없음
- P1(반영 완료):
  1. relevance 계산 시점이 score 계산 이전이어야 함 → §7 순서 수정
  2. merge 후 keyword 식별자가 movement 비교를 깨뜨릴 수 있음 → §7-1 canonical/display 분리
  3. backfill이 단순 빈자리 채우기면 재중복 가능 → §7-2 selected-set 누적 방식
  4. merge 후 candidate lookup 깨짐 가능 → §7-3 item 내장 sources 우선
- P2(참고, 일부 반영):
  - 토크나이저 private 함수 의존 확대 → §7-4, 신규 파일 신설은 보류
  - URL만으로 same-issue merge 시 재배포 URL 다르면 누락 가능 → title token overlap을 보조 기준으로 병행(이미 목표에 명시된 "article title/description overlap" 기준으로 커버됨, 별도 조치 불요)
  - 배재고/배재고등학교류 한국어 기관명 축약 규칙은 테스트에 명시 필요 → 테스트 케이스 설계 시 반영
  - representative summary와 headline 우선순위 계약을 명확히 고정 → §2 프론트 절 및 builder 출력 필드 계약으로 고정(representative_summary → representative_title → article title, crawler/builder가 `summary` 필드 자체도 이 대표 기준으로 채움)

## 10. Codex 2차 리뷰 결과 요약 (사용자 지정 8개 기준)

2차 검증은 사용자가 지정한 8개 기준(관련성 계산 순서/canonical-display 분리 일관성/backfill 재중복 방지/merge 후 lookup 보존/movement 위치/builder 책임 분리/DDL-JSON 확장 충분성/프론트 fallback 안전성)으로 진행.

- 기준 1(relevance before score): 문제 없음
- 기준 2(canonical/display 분리 일관성): 문제 없음
- 기준 3(backfill selected-set 누적): 문제 없음
- 기준 4(merged item sources 보존 의도): 로직 의도는 명확함
- 기준 5(movement 최종 Top10 이후 위치): 문제 없음
- 기준 6(builder 책임 분리): 문제 없음
- 기준 7(JSON optional field 확장 충분성): **P1** — §7-3에서 명시한 merged item의 `sources` 보존이 §2 저장 스키마 optional 필드 목록에 빠져 있었음 → **반영 완료**(§2, §7-3에 `sources` 필드를 저장/전달 계약으로 명시)
- 기준 8(프론트 fallback 안전성): 문제 없음

P0/P1 잔존 없음(1건 즉시 반영) → 구현 착수 가능 상태.

## 11. 구현 후 diff 리뷰 이력 (1~9차, 전부 review-only)

계획 리뷰 통과 후 구현 착수. 구현 완료마다 Codex review-only로 diff를 검증하고, 나온 P1/P2를 즉시 반영해 재검증하는 과정을 반복했다. 최종(9차)에서 P0/P1 없음 확인 후 커밋·운영 반영을 진행했다.

| 회차 | 주요 findings | 처리 |
|---|---|---|
| 1차 | P1: same-issue merge가 대표 1건 기사만 비교해 transitive overlap(A-B dedupe 후 B-C만 overlap)을 놓칠 수 있음 | `dedupe_and_merge`를 그룹 전체 기준 fixed-point 루프로 재작성 |
| 2차 | P1: `_article_overlap`의 `[:5]` 슬라이스가 그룹이 커질 때 뒤쪽 기사를 놓칠 수 있음 | 슬라이스 제거 |
| 3차 | P2: 기사 전체를 하나의 token union으로 합쳐 비교하면 무관 기사가 섞였을 때 실제 overlap이 희석됨 | article-level pairwise 최댓값 방식으로 변경 |
| 4차 | P2: incidental mention 기사(증정/판촉)까지 same-issue 판정 근거로 사용해 관련 없는 후보가 merge될 수 있음 | overlap 비교 전 `is_incidental=True` 기사 필터링 |
| 5차 | P2(사용자 승인 하 추가 수정): "선풍기 증정" 같은 부수 언급 기사 때문에 진짜 주체(예: "한국투자증권")까지 incidental로 낮아질 수 있음 | keyword-relative marker 판정 도입(1차 시도: title 주체 절 개념) |
| 6차 | P2: "주체 절이면 marker 무시" 규칙이 "다이슨 선풍기 증정 이벤트"(구두점 없음) 같은 케이스에서 새 false negative 발생 | marker-keyword 순수 interval distance로 재작성 시도 |
| 7차 | P2: 순수 거리 판정은 "쿠팡, 선풍기 증정 이벤트"의 "쿠팡"(짧은 주체명)을 오탐시킴 | "keyword가 title 첫 절 전체와 완전히 일치할 때만 주체로 인정"하는 조건으로 최종 확정 |
| 8차 | P2: 구두점 없는 "주체+판촉 이벤트" 패턴은 여전히 완벽히 구분 안 됨(휴리스틱 근본 한계, 인정하고 진행) | 추가 수정 없음 — 실무 투입 가능 판정, 운영 모니터링으로 전환 |
| 9차(최종) | No P0/P1. 커밋 가능 여부 재확인(Yes) | 커밋 진행 |

가장 오래 반복된 지점(4~8차, incidental mention 판정)은 "키워드가 기사의 진짜 주체인지 부속물인지"를 무거운 NLP 없이 문자열 규칙만으로 구분하려는 시도의 근본적 한계를 보여준다. 완벽한 해는 개체명 인식(NER) 수준이 필요하지만 계획서의 "무거운 NLP 의존성 추가 금지" 원칙과 상충해 도입하지 않았고, 현재 규칙(첫 절 완전 일치 조건)은 알려진 핵심 반례를 모두 닫은 상태에서 실무 투입을 승인했다.
