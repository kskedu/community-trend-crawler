# community-trend-crawler

커뮤니티 사이트 인기글을 수집해 Supabase에 저장하는 크롤러.
GitHub Actions로 주기적 실행.

## 구조

```
community-trend-crawler/
├── main.py              # 진입점, 스크래퍼 목록 관리
├── models.py            # Post 데이터 모델
├── config.py            # 공통 설정 (헤더, 타임아웃 등)
├── scrapers/
│   ├── base.py          # BaseScraper (fetch, fetch_bytes, fetch_og_image)
│   ├── clien.py
│   ├── ruliweb.py
│   ├── ppomppu.py
│   ├── mlbpark.py
│   ├── bobaedream.py
│   ├── inven.py
│   ├── dcinside.py
│   ├── humoruniv.py
│   ├── ddanzi.py
│   ├── theqoo.py
│   ├── slrclub.py
│   ├── todayhumor.py
│   └── etoland.py
├── processor/
│   ├── dedup.py         # URL 기반 중복 제거
│   ├── filter.py        # 광고/공지/노이즈 필터
│   └── scorer.py        # 점수 계산
└── db/
    └── supabase.py      # Supabase upsert
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
| 딴지일보 | ddanzi | ✅ | |
| 더쿠 | theqoo | ✅ | og:image |
| SLR클럽 | slrclub | ✅ | EUC-KR, 자체 이미지 |
| 오늘의유머 | todayhumor | ✅ | EUC-KR |
| 이토랜드 | etoland | ✅ | EUC-KR, og:image |
| 에펨코리아 | fmkorea | ❌ | 봇 차단(430) |

## 필터링 정책 (processor/filter.py)

### 제목 길이
- 5자 이하 제목 전부 제거

### 차단 키워드
**공지/운영**
- 공지, 공지사항, 안내, 규칙, 이용규칙, 이용안내
- 비밀번호, 권장, 필독, 운영, 운영진, 관리자
- 점검, 서버점검, 서비스점검
- 이벤트 안내, 당첨, 정책
- [공지], [안내], [필독], [운영], [이벤트]
- 투표 참여, 설문

**광고/스팸**
- [광고], [홍보], [협찬], [PR], [AD]
- 리딩방, 단톡방
- 즉시입금, 바로입금, 당일입금, 현금입금
- 무조건, 선착순

### 차단 패턴 (Regex)
- `^AD[\[\s#(]` — AD로 시작하는 제목
- `#\S+.*#\S+` — 해시태그 2개 이상
- `[▶▼★◆◇■□●○]{2,}` — 특수문자 2개 이상 연속
- `^\[공지\]`, `^\[안내\]`, `^\[필독\]`, `^\[운영\]`
- `^공지[\s:]`, `^안내[\s:]`

### 오탐 위험으로 미포함
- 수익, 재테크, 코인, 이벤트, 판매, 공구, 직구
