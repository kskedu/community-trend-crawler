"""네이버 언론사별 인기뉴스 parser 단위 테스트 (unittest, 실제 네트워크 호출 없음).

fixture: news_naver_press/fixtures/*.html (실제 기사 제목에 의존하지 않는 구조 fixture).

검증 항목(구현 계획 §9 1~14, 15~20은 fetcher/collector 테스트에서 커버):
1. 언론사명 추출
2. press_id 추출
3. rank1 선택
4. title
5. article URL
6. article_id
7. thumbnail (src/data-src 폴백 포함)
8. time_label
9. 여러 언론사 반복 파싱
10. malformed box skip (press_name 없음/rank1 없음/title 없음/URL 없음/이상 URL)
11. rank1 없음
12. img 없음
13. duplicate press
14. duplicate article
18. HTML 구조 급변 → ok=False
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news_naver_press.parser import parse_popular_day_html, MIN_VALID_BOX_COUNT

FIXTURE_DIR = Path(__file__).parent.parent / "news_naver_press" / "fixtures"


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


class TestNormalPage(unittest.TestCase):
    """MIN_VALID_BOX_COUNT 미만이라 ok=False 되는 fixture라도, box 단위 파싱
    로직 자체는 파서 내부 함수(_parse_box)로 직접 검증한다."""

    def setUp(self):
        from news_naver_press import parser as P
        from bs4 import BeautifulSoup
        self.P = P
        html = _load("normal_page.html")
        soup = BeautifulSoup(html, "html.parser")
        self.boxes = soup.select(".rankingnews_box")

    def test_box_count(self):
        self.assertEqual(len(self.boxes), 3)

    def test_press_name_and_id(self):
        item = self.P._parse_box(self.boxes[0])
        self.assertEqual(item.press_name, "테스트언론사A")
        self.assertEqual(item.press_id, "052")

    def test_rank1_selected_not_rank2(self):
        item = self.P._parse_box(self.boxes[0])
        self.assertEqual(item.rank, 1)
        self.assertEqual(item.article_id, "0001111111")
        self.assertIn("A-1", item.title)

    def test_title(self):
        item = self.P._parse_box(self.boxes[0])
        self.assertEqual(item.title, "[테스트 기사 제목 A-1]")

    def test_article_url(self):
        item = self.P._parse_box(self.boxes[0])
        self.assertEqual(
            item.article_url,
            "https://n.news.naver.com/article/052/0001111111?ntype=RANKING",
        )

    def test_article_id_extracted(self):
        item = self.P._parse_box(self.boxes[0])
        self.assertEqual(item.article_id, "0001111111")

    def test_thumbnail_src_direct(self):
        # box0: rank1 img가 src 속성 직접 보유
        item = self.P._parse_box(self.boxes[0])
        self.assertTrue(item.thumbnail.startswith("https://mimgnews.pstatic.net/"))

    def test_thumbnail_upscaled_from_list_preset(self):
        # box0 원본 fixture는 ?type=nf70_70(70x70 list 썸네일) — 카드 UI 확대 시
        # 흐려지므로 w800으로 치환돼야 한다(2026-09-16 실측: 원본 1200x696 확인).
        item = self.P._parse_box(self.boxes[0])
        self.assertIn("type=w800", item.thumbnail)
        self.assertNotIn("nf70_70", item.thumbnail)

    def test_thumbnail_data_src_fallback(self):
        # box1: rank1 img가 src 없이 data-src만 보유(lazy-load) → 폴백 채택 + 업스케일
        item = self.P._parse_box(self.boxes[1])
        self.assertEqual(
            item.thumbnail,
            "https://mimgnews.pstatic.net/image/origin/008/2026/09/16/2222221.jpg?type=w800",
        )

    def test_press_logo_data_src_fallback(self):
        item = self.P._parse_box(self.boxes[1])
        self.assertEqual(
            item.press_logo,
            "https://mimgnews.pstatic.net/image/upload/office_logo/008/logo_008.png",
        )

    def test_img_missing_thumbnail_none(self):
        # box2: list_img 자체가 없음 → thumbnail None (수집 실패로 취급하지 않음)
        item = self.P._parse_box(self.boxes[2])
        self.assertIsNotNone(item)
        self.assertIsNone(item.thumbnail)

    def test_time_label(self):
        item = self.P._parse_box(self.boxes[0])
        self.assertEqual(item.time_label, "2시간전")

    def test_multiple_press_repeated_parsing(self):
        items = [self.P._parse_box(b) for b in self.boxes]
        names = [i.press_name for i in items]
        self.assertEqual(names, ["테스트언론사A", "테스트언론사B", "테스트언론사C"])


class TestMalformedAndDuplicates(unittest.TestCase):
    def setUp(self):
        from news_naver_press import parser as P
        from bs4 import BeautifulSoup
        self.P = P
        html = _load("malformed_and_duplicates.html")
        soup = BeautifulSoup(html, "html.parser")
        self.boxes = soup.select(".rankingnews_box")

    def test_box_count_all_present(self):
        # 정상1 + press_name없음 + rank1없음 + title없음 + URL없음 + 이상URL
        # + press_id중복 + article_url중복 = 8개 box가 HTML엔 존재
        self.assertEqual(len(self.boxes), 8)

    def test_normal_box_parses(self):
        item = self.P._parse_box(self.boxes[0])
        self.assertIsNotNone(item)
        self.assertEqual(item.press_id, "015")

    def test_missing_press_name_skipped(self):
        item = self.P._parse_box(self.boxes[1])
        self.assertIsNone(item)

    def test_missing_rank1_skipped(self):
        # rank2만 있고 rank1 없음
        item = self.P._parse_box(self.boxes[2])
        self.assertIsNone(item)

    def test_missing_title_skipped(self):
        item = self.P._parse_box(self.boxes[3])
        self.assertIsNone(item)

    def test_missing_article_url_skipped(self):
        item = self.P._parse_box(self.boxes[4])
        self.assertIsNone(item)

    def test_non_naver_article_url_rejected(self):
        item = self.P._parse_box(self.boxes[5])
        self.assertIsNone(item)

    def test_full_page_duplicate_press_and_article(self):
        # parse_popular_day_html의 dedup 로직은 MIN_VALID_BOX_COUNT 게이트 때문에
        # 이 fixture만으로는 ok=False가 된다 — dedup 카운팅 로직 자체는
        # 게이트를 낮춘 별도 호출로 검증한다(monkeypatch).
        html = _load("malformed_and_duplicates.html")
        orig_min = self.P.MIN_VALID_BOX_COUNT
        try:
            self.P.MIN_VALID_BOX_COUNT = 1
            result = self.P.parse_popular_day_html(html)
        finally:
            self.P.MIN_VALID_BOX_COUNT = orig_min

        self.assertTrue(result.ok)
        # 유효 item: 정상언론사(015) 1건만 채택.
        # box6(015 재사용)은 press_id 중복 → dup_press+1.
        # box7(030, article_url이 015 첫 기사와 동일)은 press_id는 새로우나
        #   article_url이 이미 seen_article에 있어 dup_article+1.
        self.assertEqual(result.valid_item_count, 1)
        self.assertEqual(result.duplicate_press_count, 1)
        self.assertEqual(result.duplicate_article_count, 1)
        self.assertEqual(result.items[0].press_id, "015")


class TestStructureChanged(unittest.TestCase):
    def test_structure_changed_rejected(self):
        from news_naver_press.parser import parse_popular_day_html
        html = _load("structure_changed.html")
        result = parse_popular_day_html(html)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "STRUCTURE_CHANGED_OR_BLOCKED")
        self.assertEqual(result.box_count, 0)

    def test_empty_html_rejected(self):
        from news_naver_press.parser import parse_popular_day_html
        result = parse_popular_day_html("")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "EMPTY_HTML")

    def test_below_min_box_count_rejected(self):
        # normal_page.html은 3개 box뿐 → MIN_VALID_BOX_COUNT(30) 미달로 ok=False.
        # 이것 자체가 "너무 적은 언론사 수는 구조변경/차단 의심" fail-closed 계약 검증.
        from news_naver_press.parser import parse_popular_day_html
        html = _load("normal_page.html")
        result = parse_popular_day_html(html)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "STRUCTURE_CHANGED_OR_BLOCKED")
        self.assertEqual(result.box_count, 3)
        self.assertLess(result.box_count, MIN_VALID_BOX_COUNT)


class TestUpscaleThumbnail(unittest.TestCase):
    def setUp(self):
        from news_naver_press import parser as P
        self.P = P

    def test_replaces_type_param(self):
        url = "https://mimgnews.pstatic.net/image/origin/011/2026/09/16/4662431.jpg?type=nf70_70"
        out = self.P._upscale_thumbnail(url)
        self.assertEqual(
            out,
            "https://mimgnews.pstatic.net/image/origin/011/2026/09/16/4662431.jpg?type=w800",
        )

    def test_preserves_other_query_params(self):
        url = "https://mimgnews.pstatic.net/image/origin/011/x.jpg?type=nf70_70&extra=1"
        out = self.P._upscale_thumbnail(url)
        self.assertIn("type=w800", out)
        self.assertIn("extra=1", out)

    def test_no_type_param_left_unchanged(self):
        url = "https://mimgnews.pstatic.net/image/origin/011/x.jpg"
        self.assertEqual(self.P._upscale_thumbnail(url), url)

    def test_other_host_left_unchanged(self):
        # 알 수 없는 CDN의 쿼리 문자열을 임의로 바꾸지 않는다(안전 범위 한정).
        url = "https://cdn.example.com/img.jpg?type=nf70_70"
        self.assertEqual(self.P._upscale_thumbnail(url), url)

    def test_none_passthrough(self):
        self.assertIsNone(self.P._upscale_thumbnail(None))

    def test_empty_string_passthrough(self):
        self.assertEqual(self.P._upscale_thumbnail(""), "")


if __name__ == "__main__":
    unittest.main()
