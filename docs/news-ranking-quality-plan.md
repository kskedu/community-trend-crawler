# 실시간 이슈 랭킹 품질 개선 계획

상태: 초안(계획 리뷰 전)
관련 원 설계: `docs/news-ranking-plan.md`(있다면), `news/ranker.py`, `news/movement.py`, `news/builder.py`

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

## 8. 다음 단계

1. ~~이 계획서를 Codex review-only로 계획 리뷰~~ — 1차 완료, P0 없음 / P1 4건 반영 완료(§7-1~7-3), P2 반영(§7-4)
2. 필요 시 Codex 재리뷰(수정된 §7 기준, 최대 5회 중 2회차)
3. P0/P1 없으면 구현 승인 요청

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
