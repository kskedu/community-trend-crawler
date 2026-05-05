# community-trend-crawler

커뮤니티 사이트 인기글을 수집해 Supabase에 저장하는 크롤러.
GitHub Actions로 주기적 실행.

## 트리거 방식 (2026-05-05~)

GitHub Actions 무료 정시 cron 큐 지연 문제(최대 3시간+) 회피를 위해
**cron-job.org → GitHub `workflow_dispatch` API** 외부 트리거 사용.

- **cron-job.org**: 매시 정각 KST(`Asia/Seoul`) HTTP POST 호출 → 정확도 ±1초
- **호출 endpoint**: `POST https://api.github.com/repos/kskedu/community-trend-crawler/actions/workflows/crawl.yml/dispatches`
- **Headers**: `Authorization: Bearer <PAT>` + `Accept: application/vnd.github.v3+json` + `Content-Type: application/json`
- **Body**: `{"ref":"main"}`
- **PAT**: classic, scopes: `repo` (private 레포)
- **백업**: GitHub 자체 cron `17 * * * *` 도 그대로 유지 (cron-job.org 다운 시 안전망)
- **헬스체크**: `.github/workflows/healthcheck.yml` 텔레그램 알림 유지

## 구조

```
community-trend-crawler/
├── main.py              # 진입점, 스크래퍼 + 키워드 크롤러 통합 실행
├── models.py            # Post 데이터 모델
├── config.py            # 공통 설정 (Chrome 12종 헤더, 타임아웃 등)
├── scrapers/            # 커뮤니티 게시글 크롤러
│   ├── base.py          # BaseScraper (fetch, fetch_bytes, fetch_og_image)
│   ├── clien.py · ruliweb.py · ppomppu.py · mlbpark.py
│   ├── bobaedream.py · inven.py · dcinside.py · humoruniv.py
│   ├── theqoo.py · slrclub.py · todayhumor.py · etoland.py
│   ├── cook82.py · instiz.py · ygosu.py · natepann.py
│   └── (fmkorea.py, ddanzi.py — 비활성)
├── keywords/            # 검색엔진 실시간 키워드 크롤러
│   ├── base.py          # BaseKeywordScraper
│   ├── danawa.py        # 다나와 인기 키워드 Top 10
│   ├── daum.py          # 다음 실시간 트렌드 Top 10
│   └── namuwiki.py      # namu.news SSR 기반 실시간 검색어 Top 10
├── processor/
│   ├── dedup.py         # URL 기반 중복 제거
│   ├── filter.py        # 광고/공지/노이즈 필터
│   └── scorer.py        # 점수 계산
└── db/
    └── supabase.py      # upsert_posts, upsert_keywords
```

## 스크래퍼 현황

| 사이트 | ID | 상태 | 비고 |
|---|---|---|---|
| 클리앙 | clien | ✅ | og:image |
| 루리웹 | ruliweb | ✅ | |
| 뽐뿌 | ppomppu | ✅ | hot.php 전체 인기글 |
| 엠팍 | mlbpark | ✅ | |
| 보배드림 | bobaedream | ✅ | |
| 인벤 | inven | ✅ | |
| 디씨인사이드 | dcinside | ✅ | |
| 웃긴대학 | humoruniv | ✅ | |
| 더쿠 | theqoo | ✅ | og:image |
| SLR클럽 | slrclub | ✅ | EUC-KR, 자체 이미지 |
| 오늘의유머 | todayhumor | ✅ | EUC-KR |
| 이토랜드 | etoland | ✅ | EUC-KR, og:image |
| 82쿡 | 82cook | ✅ | best_article.php |
| 인스티즈 | instiz | ✅ | |
| 와고 | ygosu | ✅ | 베스트 daily |
| 네이트판 | natepann | ✅ | UTF-8, 대문 '톡커들의 선택' Top 40 (talkerChoiceArea0/1) |
| 에펨코리아 | fmkorea | ❌ | 봇 차단(430) |
| 딴지일보 | ddanzi | ❌ | 제거 |

## 키워드 스크래퍼 (keywords/)

검색엔진 실시간 키워드 수집 → Supabase `keyword_cache`. StartHub 프론트는
이 테이블을 직접 조회해 즉시 표시 (Vercel 함수 미경유).

| 소스 | ID | 대상 URL | 비고 |
|---|---|---|---|
| 다나와 | danawa | `/dsearch.php?query=best` | `hot_keyword` 섹션 파싱. Vercel은 403 차단되어 GH Actions(한국 친화 IP)로 이관 |
| 다음 | daum | `/search?w=tot&q=ㄴㄴ` | `list_trend` 내 `data-keyword` 추출 |
| 나무위키 | namuwiki | `https://namu.news/` | SSR HTML의 `WikiRank` JSON 파싱, 클릭 시 `namu.wiki/Go?q=` |

## Supabase DB 스키마

### community_posts

| 컬럼 | 타입 | 설명 |
|---|---|---|
| id | uuid | PK |
| title | text | 게시글 제목 |
| content | text | 본문 (일부 사이트만) |
| image_url | text | 썸네일 이미지 URL |
| source_url | text | 원본 게시글 URL (upsert 기준) |
| source_site | text | 커뮤니티 ID (`clien`, `ruliweb`, `theqoo`, `ygosu` 등) |
| upvotes | integer | 추천 수 |
| comments | integer | 댓글 수 |
| views | integer | 조회 수 |
| score | double precision | 계산된 인기 점수 (processor/scorer.py) |
| img_hash | text | 이미지 해시 (중복 판별용) |
| created_at | timestamptz | DB insert 시각 |
| collected_at | timestamptz | 크롤링 수집 시각 |
| click_count | integer | 프론트 클릭 수 |
| fav_count | integer | 프론트 즐겨찾기 수 |

- **upsert 키**: `source_url`
- **`collected_at` 갱신**: upsert 시 `datetime.now(UTC)`를 매번 명시 주입.
  과거에 필드 누락으로 상위 고정 인기글(엠팍 등)이 실시간/단기 range에서
  누락되는 버그 있었음 — [db/supabase.py](db/supabase.py) `upsert_posts` 참조
- **프론트 조회**: [StartHub/js/community.js](../StartHub/js/community.js)에서 `source_site` 필터 + `score/comments/views` 정렬

### keyword_cache
검색엔진 실시간 키워드. `keywords/` 크롤러 3종이 30분 주기로 upsert.

| 컬럼 | 타입 | 설명 |
|---|---|---|
| source | text (PK) | `danawa` / `daum` / `namuwiki` |
| keywords | jsonb | `[{keyword, url}, ...]` |
| updated_at | timestamptz | 마지막 수집 시각 |

- **upsert 키**: `source`
- **프론트 조회**: [StartHub/js/app.js](../StartHub/js/app.js) `_fetchKeywordCache()`에서 Supabase 직접 조회

## 필터링 정책 (processor/filter.py)

### 관리 방식 (2026-05-05~)
- **DB 우선**: Supabase `trend_block_keywords` 테이블에서 `enabled=true` 항목 로드
- **Fallback**: DB 조회 실패 시 filter.py 하드코딩 목록 사용
- **어드민 관리**: [StartHub/admin/trends.html](../StartHub/admin/trends.html) > `필터 관리` 탭에서 CRUD
- **이력 기록**: 추가/삭제 시 `trend_block_keyword_logs` 테이블에 자동 기록
- DB 스키마: [StartHub/docs/supabase-trend-block-keywords-migration.sql](../StartHub/docs/supabase-trend-block-keywords-migration.sql)

### 제목 길이
- 5자 이하 제목 전부 제거 (코드 고정, DB 비관리)

### 차단 키워드 / 패턴
→ Supabase `trend_block_keywords` 테이블에서 관리 (어드민 UI)
- type `keyword`: 제목에 포함 시 차단
- type `pattern`: 정규식 매칭 차단

### 오탐 위험으로 미포함
- 수익, 재테크, 코인, 이벤트, 판매, 공구, 직구
