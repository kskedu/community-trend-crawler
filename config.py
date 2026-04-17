import os

# Supabase
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://xyunhewepaxdrnajrttv.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# 크롤러 공통 설정
REQUEST_DELAY = 2.0       # 요청 간격 (초)
REQUEST_TIMEOUT = 10      # 타임아웃 (초)
MAX_POSTS_PER_SITE = 30   # 사이트당 최대 수집 글 수
RETRY_COUNT = 3           # 실패 시 재시도 횟수

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
}

# 사이트별 컬러 (프론트 배지용)
SITE_COLORS = {
    "clien":      "#1DA462",
    "fmkorea":    "#E8501C",
    "ruliweb":    "#1B6FBF",
    "ppomppu":    "#E85C00",
    "mlbpark":    "#003B8E",
    "bobaedream": "#C0392B",
    "inven":      "#2E7D32",
    "slrclub":    "#5C6BC0",
    "82cook":     "#D81B60",
    "humoruniv":  "#F57C00",
    "dcinside":   "#1565C0",
    "ddanzi":     "#333333",
    "theqoo":     "#FF6B6B",
    "todayhumor": "#FF8C00",
}

# 사이트별 표시명
SITE_NAMES = {
    "clien":      "클리앙",
    "fmkorea":    "에펨코리아",
    "ruliweb":    "루리웹",
    "ppomppu":    "뽐뿌",
    "mlbpark":    "엠팍",
    "bobaedream": "보배드림",
    "inven":      "인벤",
    "slrclub":    "SLR클럽",
    "82cook":     "82쿡",
    "humoruniv":  "웃긴대학",
    "dcinside":   "디씨인사이드",
    "ddanzi":     "딴지일보",
    "theqoo":     "더쿠",
    "todayhumor": "오늘의유머",
}
