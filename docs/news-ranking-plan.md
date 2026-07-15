# 실시간 이슈 통합 랭킹 고도화 계획서 (v1 — Codex 계획 리뷰 대기)

> 상태: **계획서 단계**. 코드 수정/외부 API 호출/DB write/workflow 실행/secret 변경/push **전부 금지**.
> 진행: 계획서 → Codex 계획 리뷰 → 원문 출력 → Claude 해석 → P0~P3 정리 → (P0/P1 있으면 수정 후 최대 5회 재리뷰) → P0/P1 소멸 시 구현 승인 요청.

> **갱신(2026-07-04) — seed source / 가중치 / 다양성 가드 구조 변경 반영**
> 이 v1 문서의 아래 세부(§3 seed 목록, §4 adapter, §7 가중치, §10 다양성 가드)는 이후 구조 개편으로 대체됐다. 최신 동작 기준:
> - **Danawa seed 제거**: 일반 실시간 이슈(`news_top`) seed에서 뺐다(쇼핑 편향). Danawa 스크래퍼/`keyword_cache(danawa)`는 존치(추후 shopping_top용). 추가로 **Nate(`nate_home`)·Bing(=`keyword_cache(msn)`, `bing_home`)·Google Trends RSS(`google_trends`)** 를 seed family로 편입.
> - **후보 sources**: `{keyword, sources: {google_trends|daum_home|nate_home|bing_home: rank, naver_news_aux|naver_news_phrase: True}}`. 독립 홈/트렌드 family = `{google_trends, naver_home(예약), daum_home, nate_home, bing_home}`.
> - **다양성 가드 교체**: "Daum 비단독 후보 < `MIN_NON_DAUM_CANDIDATES`" → **독립 source family 종수 < `MIN_SOURCE_FAMILIES`(=2)** 이면 upsert skip + last-good 유지(`candidates.count_source_families`).
> - **가중치 재구성(4축)**: News Evidence 0.45 / Search Demand 0.30 / Source Consensus 0.15 / Freshness 0.10. DataLab은 seed가 아니라 Search Demand 보조 신호로만 유지.
> - **DB 덮어쓰기 정책**: hard guard 실패(no news / source diversity / recent guard)는 upsert skip + last-good 유지. quality/fresh gate + recent guard를 통과한 5~9개는 저품질 filler 없이 partial snapshot으로 발행(오래된 last-good 10개보다 신선한 부분 결과 우선).
> - **Google Trends RSS provider**: 기본 disabled. `GOOGLE_TRENDS_ENABLED=true` **AND** `GOOGLE_TRENDS_PROVIDER=rss`일 때만 외부 HTTP 호출, 실패는 `google_fetch_failed` 로그만(전체 pipeline 안 죽임).
> 상세 최신 계약은 코드(`news/candidates.py`, `news/ranker.py`, `news/google.py`, `main.py`)와 `docs/news-ranking-quality-plan.md`를 우선한다.

---

## 1. 현재 Daum 복제 구조의 문제 정의

현재 `run_news_briefing()`(main.py:119) → `fetch_daum_seed()`(news/seed.py) → `build_issues()`(news/builder.py) 흐름은 다음과 같다:

1. `keyword_cache(source='daum')` 행을 read-only로 읽는다.
2. `_extract_keywords()`가 키워드 문자열을 추출 + 중복제거 + 상위 10개 자르기만 한다. **변형/필터/재정렬 없음.**
3. `build_issues()`가 키워드별로 네이버 뉴스만 붙인다. **키워드 자체와 순서는 안 건드림.**
4. `rank = enumerate(start=1)` → **Daum 순서 그대로.**

**결론: 실시간 이슈 Top10 = Daum 실검 Top10과 키워드·순서가 본질적으로 동일하다.** 이는 P0 설계 의도(빠른 출시)였으나, "자체 실시간 이슈"라는 정체성과 충돌한다. 차이는 수집 시점뿐이라 초기엔 100% 동일하게 보였다.

### 해결 목표
- 최종 Top10을 **자체 score** 기준으로 산출.
- Daum은 **후보 pool의 일부**일 뿐, 최종 순서를 결정하지 않는다.
- **Naver 신호가 최종 랭킹에서 가장 높은 영향력**을 갖는다.
- Google은 보조 신호. community_posts는 섞지 않는다.
- 소스 실패/skip 시 **남은 신호로 weight 재정규화**.

---

## 2. 통합 랭킹 전체 아키텍처

```
                    ┌─────────────── candidate sources ───────────────┐
keyword_cache(daum) ─┐                                                  │
keyword_cache(danawa)─┤  candidates.collect()                          │
google adapter ──────┤  → normalize + dedup → 후보 pool (최대 20~30)   │
(naver news 보조후보) ┘                                                  │
                                   │
                                   ▼
                    ┌──────────── per-candidate signals ───────────┐
                    │  naver_news.search_news(kw)  → news signal    │
                    │  datalab.fetch(batch[kw...])  → datalab signal │
                    │  google.signal(kw)            → google signal  │
                    │  daum rank                    → daum signal    │
                    └──────────────────────────────────────────────┘
                                   │
                                   ▼
                    ranker.score(candidate, signals, weights)
                    → 가용 신호만으로 weight 재정규화 + penalty 적용
                                   │
                                   ▼
                    상위 10개 선택 → build_issues(top10, news_map)
                    → issues jsonb (score/rank_reason/signals/... optional 필드)
                                   │
                                   ▼
                    upsert_news_issues(issues, source='news_top')
```

핵심 설계 원칙:
- **adapter 패턴**: 각 소스는 `(keyword) -> signal dict` 또는 `() -> [candidate]` 인터페이스로 격리. 실패 시 raise 하지 않고 빈 결과 + WARNING.
- **순수 함수 ranker**: I/O 없이 `(candidates, signals, weights) -> ranked`. 단위 테스트 용이.
- **기존 모듈 최대 재활용**: naver_news / normalizer / dedup / summarizer / builder 의 article 처리 로직은 그대로. 변경은 "키워드 선정·순서"에 집중.

---

## 3. 후보 수집 전략 (`news/candidates.py` 신규)

후보 = `{keyword, sources: {daum: rank|None, danawa: rank|None, google: rank|None}}` 형태로 병합.

1. **Daum seed**: 기존 `keyword_cache(daum)` Top10~20 (freshness 가드는 seed 신호용으로만, upsert 차단 기준에서 분리 — §10 참조).
2. **Danawa seed**: 기존 `keyword_cache(danawa)` 도 후보로 추가(이미 크롤러가 채움, read-only 재활용). 쇼핑 편향이 있으나 **후보 다양성**용. score 가중치는 낮게(daum seed 신호에 흡수하거나 별도 소가중).
3. **Google 후보**: google adapter가 후보 Top10~20 제공(가능 시). 실패 시 skip.
4. **Naver News 보조 후보 (P1-5 대응: 1차부터 경량 포함)**: Google stub + NLP 전면보류 조합이면 후보가 Daum+Danawa에 묶여 "순서만 바뀐 Daum 복제"가 될 위험이 있다(Codex P1-5). 이를 막기 위해 **무거운 NLP 없이** 다음 경량 후보원을 1차부터 둔다:
   - daum **상위 일부** 키워드(예: Top3~5)로 네이버 뉴스를 1콜씩 조회 → 기사 title에서 **빈출 토큰(기존 summarizer `_tokens` 재사용, stopword 적용)** 중 후보로 승격. 형태소 분석기 등 신규 의존성 없음.
   - 이 보조 후보 수는 소량(예: 최대 5~8개)으로 제한해 호출량/노이즈를 통제.
   - **한계 인지(Codex P1-5)**: 이 보조후보는 Daum 상위 키워드의 뉴스 title 토큰에서 파생되므로 **완전한 Daum 독립 후보원은 아니다**. 다만 (a) 뉴스 title 토큰이라 Daum 키워드와 다른 표현/연관어가 섞이고, (b) **최종 순위는 News 신호(0.55)가 지배**하므로 같은 후보집합이라도 순서는 Daum과 달라진다. 진짜 독립 후보원(Google 실연동, 형태소 NLP, 추가 소스)은 후속 과제로 명시.
   - **다양성 hard guard (단일 기준, §10과 동일)**: 최종 후보 pool에서 **Daum 단독 출처가 아닌 후보(danawa/news보조후보/google 등으로도 등장한 후보) 수 < `MIN_NON_DAUM_CANDIDATES`(기본 4)** 이면 → **upsert skip, 기존 캐시 보존**(경고 로그). "경고만 하고 진행" 옵션은 두지 않는다. 후보가 사실상 Daum뿐인 산출물은 Daum 복제와 다를 바 없으므로 저장하지 않는다.

후처리:
- **normalize**: 공백/특수문자 정리, 동의어/표기 차이 흡수는 1차에선 단순 strip + 소문자 비교 정도(과설계 회피).
- **dedup**: 동일 키워드 병합(여러 소스에 등장하면 sources에 합산).
- **상한**: 최대 20~30개로 자른다(네이버 News/DataLab 호출량 제어).

---

## 4. 각 adapter 설계

### 4-1. Daum adapter (기존 seed.py 확장 또는 candidates.py로 흡수)
- 입력: `keyword_cache(daum)` read-only.
- 출력: `[{keyword, daum_rank}]` (Top10~20).
- 변경 최소화: 기존 `fetch_daum_seed()`는 freshness 튜플을 반환하므로 그대로 두고, candidates에서 rank 포함 버전을 별도 헬퍼로 추가하거나 반환형을 확장.

### 4-2. Naver News adapter (기존 naver_news.py 재활용, 최소 확장)
- 기존 `search_news(keyword)`는 raw item 리스트 반환 → 그대로 사용.
- **추가 산출**(ranker 입력용, 본문 전문 저장 없음, normalizer 파생 → 신규 저장 필드 없음):
  - `recent_count`: **최근 N시간(기본 12h) 내** 기사 수. ← **(P1-3)** 네이버 `total`/전체 검색량은 실시간성 신호로 부적절하므로 사용하지 않는다. pubDate 파싱된 기사만 카운트.
  - `latest_age_hours`: 가장 최근 기사의 경과 시간(freshness).
  - `domain_diversity`: 고유 도메인 수.
  - `title_relevance`: 키워드가 title/snippet에 포함되는 비율.
  - `valid`: `recent_count >= 1`(최근성 있는 기사 1건 이상) 여부 — freshness 가드(§10)와 후보 유효성 판정에 사용.
- pubDate 미파싱 기사는 recent_count/latest_age 산정에서 제외(보수적). 단 article 표시에는 포함.
- 호출량: 후보 N개 × 1콜. N≤30이면 시간당 30콜 수준(쿼터 충분, §9).

### 4-3. Naver DataLab adapter (`news/datalab.py` 신규)
- 엔드포인트: 검색어트렌드 `https://openapi.naver.com/v1/datalab/search` (POST).
- **인기검색어 발굴용 아님** — 후보 키워드의 **상대 관심도/추이** 보강 신호.
- 입력: 후보 키워드 batch. API는 keywordGroups 최대 5그룹/요청.
- **⚠️ (P1-1) batch 간 비교 금지**: DataLab ratio는 **요청 단위 상대값**이라, 5개씩 나눈 서로 다른 batch의 `relative_interest`를 그대로 비교하면 랭킹이 왜곡된다. 대응:
  - (방식 A 채택) batch 내부에서만 의미 있는 `recent_delta`(최근 구간 vs 직전 구간 상승률, batch 내 자기 시계열 비율이라 batch 간 비교 무관)만 신호로 사용.
  - `relative_interest`(절대 관심도)는 batch 간 비교가 깨지므로 **1차에서 신호로 쓰지 않는다**(anchor keyword 방식은 후속 검토). 즉 DataLab 신호 = recent_delta 단독.
- 출력: `{keyword: {recent_delta}}`.
- **(P2) recent_delta 0-division 방어**: 직전 구간 값이 0(또는 누락)이면 비율 계산이 불가/무한대 → 해당 키워드는 **datalab 신호 없음**으로 처리(0이 아니라 "신호 부재"). 양 구간 모두 0이면 delta 0. 비율은 상한 클램프(예: ≤ 3.0)로 이상치 억제.
- 실패/쿼터초과 시 **전체 skip** → datalab 신호 weight 0 재정규화.
- credential: 네이버 News와 동일 `NAVER_CLIENT_ID/SECRET` 재사용(별도 등록 불필요).

### 4-4. Google adapter (`news/google.py` 신규, optional)
- **공식/허용 경로가 불명확하면 직접 크롤링/차단 우회 금지** (CLAUDE.md/요청 명시).
- 후보:
  - (a) 공식 API/허용 경로가 있으면 사용.
  - (b) 불명확하면 adapter를 **stub(미지원)** 으로 두고 항상 skip + WARNING → google 신호 weight 0으로 재정규화.
- **권장**: 1차 구현에서는 google adapter를 **인터페이스만 만들고 skip 기본값**으로 둔다. 별도 키/경로 승인 후 활성화. (요청의 "optional, 실패/미지원 시 skip" 충족)

> Codex에 질의할 핵심: Google Trends 무인증 RSS/daily trends 경로를 "차단 우회 없이 안정적으로" 쓸 수 있는지, 아니면 stub이 맞는지 판단.

---

## 5. 점수 산식 초안

> 아래는 **초안**이며, Naver News API 특성상 정량 신호(count/freshness/diversity)가 안정적이고 DataLab은 쿼터·지연 리스크가 있어, **News 비중을 상단(55%)** 으로 제안한다. Google은 stub 가능성이 높아 하한(10%).

가용한 모든 소스 기준 기본 weight (**사용자 승인값 2026-06-29**):
| 신호 | weight | 구성 |
|---|---|---|
| Naver News | **0.60** | recent_count(0.4) · latest_freshness(0.3) · domain_diversity(0.15) · title_relevance(0.15) |
| Naver DataLab | **0.20** | **recent_delta 단독** (절대비교 아닌 보정신호라 0.25→0.20 하향) |
| Google | 0.10 | google_rank/interest (stub면 0) |
| Daum seed | 0.10 | daum_rank 역순 보정값 (+danawa 소가중 흡수) |

- News의 `recent_count`는 **최근 N시간 기사 수**(네이버 `total` 미사용 — P1-3).
- 각 신호는 후보 집합 내에서 **0~1 정규화**(min-max 또는 rank 기반) 후 weight 곱 합산.
- DataLab은 recent_delta만 있으므로, delta가 없는 키워드는 datalab 신호 0(전체 skip과 구분: 일부만 없음).

**(P2) penalty는 초기 최소화** — penalty가 많으면 score 설명성/튜닝 난이도가 급증. 1차에는 다음 2개만:
- low relevance penalty: title_relevance가 임계 미만(키워드가 제목/snippet에 거의 없음) → 감점.
- noise keyword penalty: 길이 1·순수 숫자·광고성 토큰 등 명백한 노이즈 → 감점.
- (보류) duplicate domain / stale source penalty는 이미 News 신호(domain_diversity, latest_freshness)에 반영되므로 **중복 penalty 지양**. 효과 부족 시 후속 추가.

최종 `score = Σ(weight_i × norm_signal_i) − Σ penalty`. 상위 10개 선택. **동점 시 News signal 우선.**

- **(P2) `score`는 소수 4자리로 반올림 저장**(JSON 비대화 방지). `source_breakdown`도 동일.
- **(P2) Danawa 편향**: 쇼핑성 키워드가 뉴스 이슈 품질을 흐릴 수 있으므로 Danawa는 **후보 공급원으로만** 쓰고 daum과 합쳐 낮은 가중(0.10 내 흡수). 최종 순위는 News가 지배하므로 쇼핑 키워드는 News 신호 약하면 자연 탈락.
- **(P2) `rank_reason`은 실제 사용된 신호만** 사실대로 표기(과장 금지). 예: News만 기여 시 "최근 뉴스 다수", DataLab 상승 동반 시 "뉴스 다수 + 검색 관심 상승". 미사용 신호는 언급하지 않는다.

> 실제 가중치는 구현 직전 1~2회 dry-run 분포를 보고 미세조정(코드 상수로 관리, DDL 무관).

---

## 6. 실패/skip 시 weight 재정규화

```
available = {신호: weight for 신호 in 전체 if 해당 신호 사용가능}
total = sum(available.values())
renorm = {신호: w/total for 신호, w in available.items()}
```

- 예: DataLab·Google 둘 다 skip → News 0.55, Daum 0.10 → renorm: News 0.846, Daum 0.154.
- **(P1-2) 0-division 방어**: `available`이 비거나 `total==0`이면 재정규화하지 않고 **즉시 upsert skip**. 특히 **News 신호가 unavailable이면**(키 없음/전건 실패) 다른 신호 유무와 무관하게 **즉시 skip**(News는 필수 신호 — `news_available==False` → skip을 코드에 명시).
- 재정규화 결과는 `source_breakdown` / `data_sources`에 기록해 디버깅 가능하게.

---

## 7. `news_issue_cache` JSON 확장안 (DDL 변경 없음)

기존 `issues = {keywords: [...]}` 유지. 각 keyword entry에 **optional 필드만 추가**(기존 프론트는 모르는 필드 무시 가능):

```jsonc
{
  "keywords": [
    {
      "rank": 1,
      "keyword": "...",
      "summary": "...",            // no_representative면 "" (프론트는 홈 설명줄 자체를 숨김)
      "summary_type": "rule|title|seed_only|no_representative",
      "signals": {
        "news": true,
        "trend": false,            // 기존 호환
        "datalab": true,           // 신규: datalab 사용 여부
        "google": false            // 신규
      },
      "trend": null,               // 기존 호환 (datalab 점수화 후 객체로 채울 수 있음)
      "articles": [ ... ],         // 기존 동일 (title/url/press/snippet/published_at/thumbnail)
      // ===== 신규 optional =====
      "score": 0.0,                // 최종 score (소수 4자리 반올림)
      "rank_reason": "최근 뉴스 다수",  // 실제 사용 신호만 사실 표기(과장 금지)
      "source_breakdown": {        // 신호별 정규화 점수 (소수 4자리)
        "news": 0.0, "datalab": 0.0, "google": 0.0, "daum": 0.0
      }
    }
  ],
  // ===== issues 루트 레벨 신규 optional =====
  "data_sources": ["naver_news", "datalab", "daum"],  // 이번 산출에 실제 쓰인 소스
  "generated_at": "ISO8601"        // updated_at(컬럼)과 별개로 issues 내부에도 기록
}
```

- 기존 컬럼 `source(PK)`, `issues(jsonb)`, `updated_at` 그대로. **DDL 변경 0.**
- 프론트가 추가 필드를 무시하는지는 §11에서 검증.

---

## 8. 수정 예정 파일 목록 (allowed files)

**crawler (community-trend-crawler/)** — 1차 구현 범위:
- `news/candidates.py` (신규) — 후보 수집/병합/dedup.
- `news/datalab.py` (신규) — DataLab adapter.
- `news/google.py` (신규) — Google adapter(기본 stub+skip).
- `news/ranker.py` (신규) — 순수 함수 score/재정규화/penalty.
- `news/builder.py` (수정) — top10 + signals/score/rank_reason/source_breakdown 채우도록 entry 확장. 기존 article 로직 유지.
- `news/seed.py` (수정 최소) — candidates에서 daum rank 활용할 헬퍼 추가(기존 fetch_daum_seed 시그니처 보존).
- `main.py` (수정) — `run_news_briefing()`을 candidates→signals→ranker→build 흐름으로 교체. try/except 격리 유지.
- `news/dryrun.py` (수정) — 통합 랭킹 dry-run 경로 추가(fixture 기반, 실호출/DB write 없음).
- `news/fixtures/*` (추가) — datalab/google/multi-source fixture.
- `tests/` 해당 신규 테스트(있으면 위치 확인 후).
- `docs/news-ranking-plan.md` (본 문서).

**StartHub/** — 후속(별도 승인, 1차에서 미접촉):
- `js/news-brief.js`, `css/news-brief.css`, `app.html` — "랭킹 근거" 상세 표시(F안 UI 유지, 큰 변경 없음).

**절대 미접촉**: js/auto-group-modal.js, js/favorites.js, _qa_tmp.mjs, tools/diag-*.mjs, modoo_ideas*, 기타 community_posts/trend tab 관련 파일.

---

## 9. API 호출량 / 쿼터 리스크

- **⚠️ (P1-2) News와 DataLab은 쿼터가 다르다** (Codex 확인, 네이버 공식):
  - **Naver News Search API: 일 25,000회.**
  - **Naver DataLab 검색어트렌드: 일 1,000회.** (공용 25,000 아님)
- 추정:
  - News: 후보 N(≤30)개 + 보조후보 조회 소량 × 매시 = 대략 ~40콜 × 24 = **~960콜/일** ≪ 25,000. 여유.
  - DataLab: 후보 5개씩 묶음 → ⌈30/5⌉=6콜 × 24 = **144콜/일** ≪ 1,000. 여유(단 1,000 한도를 상수로 인지하고, 후보 상한·실행주기 변경 시 재계산).
  - 보호장치: 후보 상한(30) 강제, DataLab 실패 전체 skip, 실행주기(매시) 고정.
- **Google**: 1차 stub이면 0콜. 활성화 시 호출량/약관 재검토 후 진행.
- 보호장치: 후보 상한, 호출 실패 시 재시도 최소화(기존 RETRY_COUNT 따름), DataLab 실패 전체 skip.

---

## 10. stale / freshness 처리

- 현재 `is_fresh`(daum.updated_at 2h)가 **upsert 차단 기준**으로 쓰임 — daum이 stale이면 news_top 자체를 갱신 안 함.
- 통합 랭킹에선 daum이 후보 일부일 뿐이므로 **차단 기준을 News 신호로 이동**:
  - daum stale → daum을 후보에서 제외하거나 daum 신호 weight 0(재정규화), 단 News 후보(google 등)가 있으면 **계속 진행**.
  - 단, daum이 유일 후보원인 상황(google stub, danawa도 없음)에서 daum까지 stale이면 후보 부족 → upsert skip.
  - **(P1-4) 최종 upsert 가드 강화** — `has_any_news`(News≥1건 존재)만으로는 약함(오래된 기사 1건으로도 통과). 다음을 **모두** 만족해야 upsert:
    1. `news_available == True` (키/호출 정상).
    2. **최종 Top10 중 `recent_count>=1`(최근 N시간 기사 보유) 키워드가 임계 이상**(예: ≥5). 미만이면 "실시간성 부족" → upsert skip, 기존 캐시 보존.
    3. **다양성 hard guard 통과**(§3과 동일 기준): Daum 비단독 후보 수 ≥ `MIN_NON_DAUM_CANDIDATES`. 미만이면 upsert skip. (§3·§10은 **동일한 단일 가드**를 가리킨다 — 한 곳에 상수로 정의하고 양쪽에서 참조.)
  - 각 키워드 entry에 `latest_age_hours`(또는 latest_pubDate)를 신호로 보유해 위 판정에 사용(저장은 optional).
- `generated_at`을 issues에 기록해 프론트 업데이트시간 표시(D안)와 별개로 산출 시각 추적.
- **(P2) stale 캐시 노출 대비**: upsert가 연속 skip되어 캐시가 오래되면 프론트 "업데이트 시각"이 낡아 보일 수 있음 → 후속(StartHub 별도 승인)에서 일정 시간 초과 시 "최신 데이터 수집 지연" 류 표기 검토(1차 미포함, note).

> Codex 질의: freshness 차단 기준을 daum→news로 옮길 때 회귀 위험(빈/오래된 캐시 노출) 점검.

---

## 11. 기존 프론트 호환성

- **(P2 실측 확인)** news-brief.js를 직접 grep으로 확인한 결과, 읽는 필드는 `k.rank`(없으면 `i+1` fallback, news-brief.js:199), `k.signals.news`(news-brief.js:208), `k.summary`(news-brief.js:268), `k.articles[]`(`Array.isArray` 가드, news-brief.js:269/285), `k.keyword` 뿐. **schema validation / `Object.keys` / rank 개수 고정 / `signals.trend` truthy 처리 없음** → 신규 optional 필드(score/rank_reason/source_breakdown/data_sources/generated_at/signals.datalab/signals.google)는 **자연 무시**, 회귀 없음.
- `signals.trend`/`trend`는 기존대로 false/null 유지 가능(추가 datalab 신호는 `signals.datalab`로 분리하므로 충돌 없음).
- 상호배타(실시간이슈↔검색엔진), 보안(textContent, http/https only, rel noopener noreferrer)은 **변경 없음** → 회귀 없음.
- F안 UI(1위 강조 + 칩 리스트 + 꺾쇠) 유지. "랭킹 근거"는 후속 상세 표시(별도 승인).

---

## 12. 테스트 / 검증 계획

1. **단위 테스트**(순수 함수):
   - ranker: 동일 입력 → 결정적 score; News만 가용 시 재정규화 정확성; penalty 적용; 동점 tiebreak.
   - candidates: dedup/병합/상한.
   - normalize: 기존 보안 테스트 유지.
2. **dry-run**(fixture, 실호출/DB write 0):
   - multi-source fixture로 **Daum 순서와 다른** Top10이 나오는지 확인(핵심 합격 기준).
   - DataLab/Google skip 시 재정규화 동작.
3. **운영 검증**(별도 승인 후):
   - workflow_dispatch 1회 → 로그에 secret 미노출, 실호출 경로, source_breakdown 기록, upsert 201.
   - DB row: keywords 10개 + score/rank_reason/source_breakdown 존재, Daum과 순서 상이 확인.
   - 프론트 Preview: 신규 필드 무시하고 정상 렌더, 콘솔 에러 0.

합격 기준: **Top10이 Daum Top10과 순서가 다르고, News 신호가 순위에 지배적으로 반영됨.**

---

## 13. 롤백 계획

- crawler 코드: `run_news_briefing()` 교체 전 커밋을 태깅/기록. 문제 시 해당 함수만 이전 버전(daum seed 직결)으로 revert(파일 단위 작음).
- DB: `news_issue_cache(source='news_top')` 단일 행 upsert. 신규는 **optional 필드 추가뿐**이라 구버전 프론트가 깨지지 않음 → 데이터 롤백 불필요. 필요 시 다음 정상 cron이 덮어씀.
- DDL 변경 없음 → 스키마 롤백 불필요.
- StartHub 후속(상세 근거)은 분리 배포 → 독립 롤백.

---

## 14. Codex 계획 리뷰 요청문 (review-only)

> Git Bash에서 stdin 파이프로 호출. 임시 diff 파일 생성 금지.

### 14-1. 1차 리뷰 반영 이력 (Codex review #1 → v2)

P0 없음. P1 5건 전부 반영:
- **P1-1** DataLab batch 간 비교 불가 → `relative_interest` 폐기, **recent_delta 단독** 사용(§4-3, §5).
- **P1-2** DataLab 쿼터 = **일 1,000회**(News 25,000과 별도)로 정정 + 재정규화 0-division 즉시 skip(§9, §6).
- **P1-3** News volume `total` 미사용 → **recent_count(최근 N시간 기사 수)** 중심(§4-2, §5).
- **P1-4** freshness 가드 강화 → `has_any_news`만이 아니라 **Top10 중 최근성 보유 키워드 ≥임계** + news_available 필수(§10).
- **P1-5** Daum 복제 회피 → **경량 보조후보(신규 의존성 없음)** 1차 포함 + 다양성 가드(§3).

P2 반영: 프론트 파싱 실측 확인(§11), penalty 초기 최소화·score 소수4자리·Danawa 후보전용·rank_reason 사실표기(§5), stale 캐시 후속 표기 note(§10).

### 14-1b. 2차 리뷰 반영 이력 (Codex review #2 → v3)

2차 판정: P1-1~4 RESOLVED, P1-5 PARTIAL + 신규 P1 1건. 전부 반영:
- **P1-5/신규 P1(다양성 가드 일관성)**: §3·§10이 모순(경고+옵션 vs upsert 조건) → **단일 hard guard로 통일**. Daum 비단독 후보 < `MIN_NON_DAUM_CANDIDATES`(기본 4)면 **upsert skip**(경고만 진행 옵션 제거). 상수 한 곳 정의 후 양쪽 참조. 보조후보가 Daum 파생이라는 한계 명시 + News 지배로 순서 탈동조 보완(§3).
- **P2(호출량 수치 불일치)**: §14-2의 ~864 → §9 단일 기준(News ~960/일, DataLab 144/일)으로 통일.
- **P2(recent_delta 0-division)**: 직전 구간 0/누락 시 "신호 부재" 처리 + 비율 상한 클램프 명시(§4-3).

### 14-2. 2차(재리뷰) 질의

리뷰 관점 질의:
1. **가중치 적정성**: News 0.55 / DataLab 0.25(recent_delta 단독) / Google 0.10 / Daum 0.10 이 "News 지배 + Daum 탈동조화"에 타당한가? recent_delta 단독으로 DataLab 0.25가 과한가?
2. **재정규화 안전성**: 소스 skip 시 weight 재정규화 로직에 0-division/편향 위험은?
3. **freshness 기준 이동**(daum→news) 회귀 위험: stale 캐시 노출/빈 노출 가능성.
4. **Google adapter stub 결정**이 타당한가, 허용 가능한 공식 경로가 실제 있는가.
5. **DataLab batch(5그룹)** 사용과 쿼터 추정(News ~960콜/일 ≪ 25,000 · DataLab 144콜/일 ≪ 1,000, §9 단일 기준)의 현실성.
6. **JSON optional 확장**이 기존 프론트 무시 호환을 실제로 보장하는지, 빠뜨린 필드.
7. **후보 NLP 보조후보 1차 보류** 결정의 타당성.
8. **파일 분리(candidates/datalab/google/ranker)** 의 응집도/과설계 여부.
9. 본문 전문 미저장·키 미노출·차단 우회 금지 등 **보안 계약** 위반 소지.
10. P0/P1/P2/P3 분류 제안.
```
