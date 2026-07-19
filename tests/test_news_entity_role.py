"""entity-role 정제 + cohesion gate + B2 + canonical evidence 회귀 fixture (2026-07).

사용자 지정 필수 fixture(§5) + 추가 정합 검증. 외부 호출/DB write 없음.
- 장동건 보존 / 신천지 교단 보존 / 신천지 정치 제거 / snippet-only 제거
- 한화 다중사건 차단 / 한화 동일사건 통과 / 주어생략 복구 / 비엔티티 event 보호
- 전재중복 dedup / canonical evidence 정합(summary·display·B2 동일 base)
"""
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news import candidates as cand
from news import ranker
from news.builder import build_ranked_entry, ARTICLES_MIN, ARTICLES_MAX
from news.replay import replay_selection


_NOW = datetime.now(timezone.utc)


def _iso(hours_ago=1.0):
    return (_NOW - timedelta(hours=hours_ago)).strftime("%a, %d %b %Y %H:%M:%S +0000")


def _raw(title, url, desc="", press_host="news1.example.com", hours_ago=1.0):
    # originallink host가 press를 결정한다(normalizer.guess_press). 서로 다른 press가
    # 필요한 burst 테스트를 위해 host를 파라미터화한다.
    return {
        "title": title,
        "originallink": f"https://{press_host}/{abs(hash(title)) % 100000}",
        "link": url,
        "description": desc,
        "pubDate": _iso(hours_ago),
    }


def _sig(keyword, raw_items):
    return cand.compute_news_signal(keyword, raw_items)


def _display_titles(keyword, sig):
    articles = cand.filter_articles_for_display(sig["articles"], min_count=1)
    disp = cand.build_display_articles(
        keyword, articles, sig.get("representative_article")
    )
    return [a["title"] for a in disp]


class TestEntityRoleRefinement(unittest.TestCase):

    # 1. 장동건 보존 — 같은 인물의 여러 사건이 다른 cluster로 쪼개져도 subject/unknown 보존.
    def test_jangdonggun_preserved(self):
        arts = [
            _raw("노화 고백한 장동건, 급 탱탱 동안됐다", "u1", "배우 장동건"),
            _raw("못 알아볼 뻔…장동건, 공식석상서 포착", "u2", "장동건 행사"),
            _raw("54세 장동건, 못 알아볼 뻔한 바뀐 얼굴", "u3", "장동건 외모"),
            _raw("중년 배우들 회춘…볼살 통통해진 장동건", "u4", "황정민과 장동건"),
            # 부수 언급(경품)은 제외돼야 한다.
            _raw("영화 시사회 경품 증정, 장동건 친필 사인 지급", "u5", "경품 장동건 친필 사인 증정"),
        ]
        sig = _sig("장동건", arts)
        titles = _display_titles("장동건", sig)
        self.assertGreaterEqual(len(titles), 3, "장동건 정상 기사 3건+ 보존")
        self.assertTrue(all("장동건" in t for t in titles))
        self.assertFalse(any("경품 증정" in t for t in titles), "경품 부수언급 제외")

    # 2. 신천지 교단 사건 보존 — 신천지가 핵심 당사자면 유지.
    def test_shincheonji_religious_event_preserved(self):
        arts = [
            _raw("고성서 신천지 이만희 총회장 공정재판 촉구 기도회", "u1", "대원암 기도회"),
            _raw("신천지 이만희 총회장 재판 다시 열린다", "u2", "법원 신천지 재판"),
        ]
        sig = _sig("신천지", arts)
        titles = _display_titles("신천지", sig)
        self.assertTrue(any("이만희" in t for t in titles), "교단 사건 기사 보존")

    # 3. 신천지 정치 수사 제거 — "반명·신천지와의 대결"류는 non_subject.
    def test_shincheonji_political_rhetoric_removed(self):
        arts = [
            _raw("김민석 이번 전대 본질은 위장 반명·신천지와의 대결", "u1", "김민석 전 총리"),
            _raw("고성서 신천지 이만희 총회장 공정재판 촉구 기도회", "u2", "대원암"),
            _raw("신천지 이만희 총회장 재판 다시 열린다", "u3", "법원"),
        ]
        sig = _sig("신천지", arts)
        titles = _display_titles("신천지", sig)
        self.assertFalse(any("반명" in t or "대결" in t for t in titles),
                         "정치 수사 기사가 display에서 제외돼야 함")
        # role 판정 직접 검증
        role, reason = cand.classify_entity_role(
            "신천지", {"title": "김민석 이번 전대 본질은 위장 반명·신천지와의 대결",
                      "relevance_reason": "keyword_main_topic"})
        self.assertEqual(role, "non_subject")

    # 4. snippet-only 정치 기사 제거 — 제목엔 신천지 없고 snippet에만.
    def test_shincheonji_snippet_only_removed(self):
        arts = [
            _raw("고성서 신천지 이만희 총회장 공정재판 촉구 기도회", "u1", "대원암"),
            _raw("신천지 이만희 총회장 재판 다시 열린다", "u2", "법원"),
            _raw("노무현 이어 이해찬 정치 계승…정청래 적통 공세", "u3", "전대 신천지 개입 가능성"),
        ]
        sig = _sig("신천지", arts)
        titles = _display_titles("신천지", sig)
        self.assertFalse(any("이해찬" in t or "적통" in t for t in titles),
                         "snippet-only 정치 기사 제외")

    # 5. 한화 다중 사건 차단 — 토큰만 공유, 사건 다름 → cohesion 미달로 gate 탈락.
    def test_hanwha_multi_event_blocked(self):
        arts = [
            _raw("한화 이글스 7연패 탈출 극적 승리", "u1", "야구", press_host="sports.example.com"),
            _raw("한화그룹 방산 대규모 인수 발표", "u2", "방산 인수", press_host="biz.example.com"),
        ]
        sig = _sig("한화", arts)
        self.assertEqual(sig["keyword_kind"], "entity")
        self.assertFalse(sig["has_dominant_event"])
        self.assertFalse(sig["same_event_burst"])
        self.assertEqual(ranker._quality_gate_reason("한화", sig), "low_quality_news")

    # 6. 한화 동일 사건 보존 — 다른 언론사·시간근접·공통 사건토큰 → 통과 + 키워드명 구체화.
    def test_hanwha_same_event_passes_and_specifies(self):
        arts = [
            _raw("한화 7연패 탈출 극적 승리", "u1", "7연패 탈출", press_host="a.example.com", hours_ago=1),
            _raw("한화 7연패 탈출 사슬 끊었다", "u2", "7연패 역전", press_host="b.example.com", hours_ago=2),
            _raw("한화 7연패 탈출 팬 환호", "u3", "7연패 환호", press_host="c.example.com", hours_ago=3),
        ]
        sig = _sig("한화", arts)
        self.assertTrue(sig["has_dominant_event"] or sig["same_event_burst"])
        self.assertIsNone(ranker._quality_gate_reason("한화", sig))
        item = {"keyword": "한화", "display_keyword": "한화", "news_meta": sig, "related_keywords": []}
        resolved = ranker._resolve_singleton_display(item)
        self.assertNotEqual(resolved["display_keyword"], "한화", "엔티티+사건으로 구체화")
        self.assertIn("7연패", resolved["display_keyword"])

    # 7. 주어 생략 복구 — 조사 없지만 title 앞·keyword_main_topic이면 subject.
    def test_subject_recovered_when_leading(self):
        role, reason = cand.classify_entity_role(
            "장동건", {"title": "장동건 신작 영화 제작발표회 참석",
                      "relevance_reason": "keyword_main_topic"})
        self.assertEqual(role, "subject")

    # 8. 비엔티티 단일 사건 키워드 보호 — event는 cohesion 강화 미적용.
    def test_event_keyword_not_hardened(self):
        arts = [
            _raw("강원 산불 확산 대피", "u1", "산불 대피", press_host="a.example.com"),
            _raw("경북 산불 진화 총력", "u2", "산불 진화", press_host="b.example.com"),
        ]
        sig = _sig("산불", arts)
        self.assertEqual(sig["keyword_kind"], "event")
        # event는 has_dominant_event 여부와 무관하게 cohesion gate를 타지 않는다.
        self.assertIsNone(ranker._quality_gate_reason("산불", sig))

    # 9. 전재중복 dedup — 같은 URL 재전송은 canonical evidence에서 1건.
    def test_syndicated_duplicate_deduped(self):
        dup = {"title": "한화 7연패 탈출 극적 승리", "originallink": "https://x.example.com/same",
               "link": "https://x.example.com/same", "description": "7연패 탈출", "pubDate": _iso(1)}
        arts = [
            dup,
            dict(dup),  # 완전 동일 URL 재전송
            _raw("한화 7연패 탈출 사슬 끊었다", "u2", "7연패 역전", press_host="b.example.com", hours_ago=2),
        ]
        sig = _sig("한화", arts)
        articles, _, _ = cand.canonical_evidence(sig, "한화")
        urls = [a.get("url") for a in articles]
        self.assertEqual(len(urls), len(set(urls)), "중복 URL 제거")

    # 10. canonical evidence 정합 — summary·B2·display가 동일 base set(dedup→filter→MAX)을 쓴다.
    def test_canonical_evidence_consistency(self):
        arts = [
            _raw("한화 7연패 탈출 극적 승리", "u1", "7연패 탈출", press_host="a.example.com", hours_ago=1),
            _raw("한화 7연패 탈출 사슬 끊었다", "u2", "7연패 역전", press_host="b.example.com", hours_ago=2),
        ]
        sig = _sig("한화", arts)
        # canonical_evidence가 builder(build_ranked_entry)와 동일한 base articles를 산출.
        base_articles, summary, summary_type = cand.canonical_evidence(sig, "한화")
        ranked_item = {"keyword": "한화", "news_meta": sig, "score": 0.5,
                       "source_breakdown": {}, "rank_reason": "", "display_keyword": "한화",
                       "sources": {"daum_home": 1}}
        entry = build_ranked_entry(1, ranked_item)
        # builder의 summary_type == canonical_evidence의 summary_type(동일 파이프).
        self.assertEqual(entry["summary_type"], summary_type)
        # display_articles는 base articles의 부분집합(articles ⊇ display_articles).
        base_urls = {a.get("url") for a in base_articles}
        for d in entry["display_articles"]:
            self.assertIn(d.get("url"), base_urls,
                          "display_articles는 canonical base set에서 파생돼야 함")


class TestB2AndBackfill(unittest.TestCase):

    # B2가 select_top 전에 적용돼 하위 정상후보가 backfill되는지 replay로 검증.
    def test_b2_pre_select_backfills_lower_candidate(self):
        # 키워드별 고유 사건 어휘를 줘 dedupe_and_merge 병합을 피한다(diagnostics fixture 규약).
        # 상위 2개는 두 기사가 서로 다른 고유사건이라 공통 하위주제 없음 → no_representative.
        # 나머지 10개는 각자 고유 사건토큰 2개를 두 기사가 공유 → 정상 대표 생성.
        uniq = ["가나", "다라", "마바", "사아", "자차", "카타", "파하", "거너", "더러", "머버",
                "서어", "저처"]
        arts_by = {}
        keywords = []
        for i in range(12):
            # 상위 1개는 다토큰 키워드(unknown → cohesion gate 미적용)로 두어 cohesion이 아닌
            # B2(no_representative)에서만 걸리게 한다. 두 기사가 keyword 외 공통 토큰이 없음.
            kw = f"장기{uniq[i]} 특별사안" if i < 1 else f"이슈{uniq[i]}"
            keywords.append(kw)
            if i < 1:
                arts_by[kw] = [
                    _raw(f"{kw} 산악회원 야유회 성황", "u", f"{kw} 산악회원 야유회",
                         press_host=f"h{i}a.example.com"),
                    _raw(f"{kw} 병원장 표창장 수여", "u", f"{kw} 병원장 표창장",
                         press_host=f"h{i}b.example.com"),
                ]
            else:
                ev = f"{uniq[i]}참사"
                arts_by[kw] = [
                    _raw(f"{kw} {ev} 긴급대응 속보", "u", f"{kw} {ev} 긴급대응",
                         press_host=f"h{i}a.example.com"),
                    _raw(f"{kw} {ev} 긴급대응 후속", "u", f"{kw} {ev} 긴급대응",
                         press_host=f"h{i}b.example.com"),
                ]
        r = replay_selection({"keywords": keywords, "articles_by_keyword": arts_by})
        selected_kws = [s["keyword"] for s in r["selected"]]
        no_rep = r["no_rep_excluded"]
        # 상위 no_rep 후보가 빠져도 하위 정상후보가 채워 10개 유지(backfill).
        self.assertEqual(len(selected_kws), ranker.TOP_N,
                         f"B2 backfill로 Top10 유지: selected={selected_kws} no_rep={no_rep}")
        self.assertIn("장기가나 특별사안", no_rep, "상위 다토큰 후보가 no_representative로 제외")
        self.assertNotIn("장기가나 특별사안", selected_kws)


if __name__ == "__main__":
    unittest.main()
