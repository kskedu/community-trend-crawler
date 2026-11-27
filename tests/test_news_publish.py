"""B4 news_top 목록·상세 게시(news/publish.py · db.supabase.upsert_news_top_bundle) 계약 시험."""
import copy
import unittest
from unittest.mock import patch

import httpx
from postgrest.exceptions import APIError

import db.supabase as db_supabase
import main
from news import publish
from news.thumbnail import build_thumbnail_cache


def _issues(n=3, with_display=True, gen="2026-11-27T01:21:09+00:00"):
    kws = []
    for i in range(1, n + 1):
        arts = [{"url": f"https://e/{i}/a", "press": "P", "title": f"T{i}", "snippet": "s", "clean_description": "c",
                 "thumbnail": f"https://img/{i}.jpg", "published_at": "2026-11-27", "relevance_score": 0.9}]
        k = {"rank": i, "keyword": f"k{i}", "display_keyword": f"K{i}", "summary": f"S{i}", "movement": "new",
             "rank_delta": 0, "score": 1.0, "signals": {"news": True}, "articles": arts}
        if with_display:
            k["display_articles"] = copy.deepcopy(arts)
        kws.append(k)
    return {"keywords": kws, "generated_at": gen, "data_sources": ["naver_news"]}


class TestPayloads(unittest.TestCase):
    def test_list_payload_has_only_list_fields_and_snapshot(self):
        p = publish.build_list_payload(_issues())
        self.assertEqual(p["snapshot"], {"generated_at": "2026-11-27T01:21:09+00:00"})
        self.assertEqual(set(p["keywords"][0]), set(publish.LIST_FIELDS))
        self.assertEqual([k["rank"] for k in p["keywords"]], [1, 2, 3])

    def test_detail_payloads_same_snapshot_and_used_fields_only(self):
        d = publish.build_detail_payloads(_issues())
        self.assertEqual([s for s, _ in d], ["news_top_detail_1", "news_top_detail_2", "news_top_detail_3"])
        for _, p in d:
            self.assertEqual(p["snapshot"]["generated_at"], "2026-11-27T01:21:09+00:00")
            self.assertEqual(set(p["display_articles"][0]), set(publish.ARTICLE_FIELDS))   # relevance_score 등 내부 필드 제외

    def test_detail_falls_back_to_articles_for_old_shape(self):
        d = publish.build_detail_payloads(_issues(1, with_display=False))
        self.assertEqual(d[0][1]["display_articles"][0]["url"], "https://e/1/a")

    def test_detail_rank_bounded_and_unique(self):
        iss = _issues(3)
        iss["keywords"][1]["rank"] = 11          # 범위 밖
        iss["keywords"][2]["rank"] = 1           # 중복
        iss["keywords"].append({"rank": True, "keyword": "bool"})   # bool 은 정수가 아니다
        self.assertEqual([s for s, _ in publish.build_detail_payloads(iss)], ["news_top_detail_1"])

    def test_storage_drops_duplicate_articles_only_with_display_and_keeps_input(self):
        iss = _issues(2)
        iss["keywords"][1].pop("display_articles")
        before = copy.deepcopy(iss)
        out = publish.storage_issues(iss)
        self.assertNotIn("articles", out["keywords"][0])
        self.assertIn("display_articles", out["keywords"][0])
        self.assertIn("articles", out["keywords"][1])     # display 없는 구 형식은 그대로
        self.assertEqual(iss, before)                       # 입력 불변

    def test_thumbnail_cache_reads_display_articles_when_articles_absent(self):
        stored = publish.storage_issues(_issues(2))
        cache = build_thumbnail_cache(stored)
        self.assertEqual(cache.get("https://e/1/a"), "https://img/1.jpg")
        self.assertEqual(cache.get("https://e/2/a"), "https://img/2.jpg")


class _Resp:
    def __init__(self):
        self.data = []
        self.count = None


class _FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.upserts = []

    def table(self, name):
        return self

    def upsert(self, rows, on_conflict=None, **opts):
        self.upserts.append((rows if isinstance(rows, list) else [rows], on_conflict, opts))
        return self

    def execute(self):
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        return _Resp()


class TestBundle(unittest.TestCase):
    def setUp(self):
        db_supabase.ledger.reset()
        p = patch.object(db_supabase.time, "sleep")
        p.start()
        self.addCleanup(p.stop)

    def _run(self, client, iss):
        with patch.object(db_supabase, "get_client", return_value=client):
            return db_supabase.upsert_news_top_bundle(iss)

    def _summary(self):
        return main.summarize_run("news_top_only", {}, "saved", db_supabase.ledger.entries, db_supabase.ledger.db_unavailable)

    def test_bundle_is_one_statement_with_all_rows(self):
        c = _FakeClient(["ok"])
        self.assertEqual(self._run(c, _issues(3)), "bundle")
        self.assertEqual(len(c.upserts), 1)
        rows, conflict, opts = c.upserts[0]
        self.assertEqual([r["source"] for r in rows], ["news_top", "news_top_list", "news_top_detail_1", "news_top_detail_2", "news_top_detail_3"])
        self.assertEqual(conflict, "source")
        self.assertEqual(opts.get("returning"), db_supabase.ReturnMethod.minimal)
        self.assertNotIn("articles", rows[0]["issues"]["keywords"][0])     # 저장용 news_top(F7-NF-01)
        self.assertEqual(len({r["updated_at"] for r in rows}), 1)          # 같은 회차 시각
        self.assertEqual(self._summary()["status"], "OK")

    def test_rejected_bundle_falls_back_to_single_news_top_and_is_partial_not_failed(self):
        rejected = APIError({"message": "new row violates check constraint", "code": "23514", "hint": None, "details": None})
        c = _FakeClient([rejected, "ok"])
        self.assertEqual(self._run(c, _issues(2)), "single")
        self.assertEqual(len(c.upserts), 2)
        self.assertEqual([r["source"] for r in c.upserts[1][0]], ["news_top"])
        s = self._summary()
        self.assertEqual(s["status"], "PARTIAL")
        self.assertEqual(s["required_save_failed"], [])

    def test_transient_failure_counts_as_required_news_top_failure(self):
        c = _FakeClient([httpx.ReadTimeout("t")])
        self.assertIsNone(self._run(c, _issues(2)))
        s = self._summary()
        self.assertEqual(s["status"], "FAILED")
        self.assertIn("news_issue_cache:news_top", s["required_save_failed"])

    def test_payload_build_error_falls_back_to_single(self):
        c = _FakeClient(["ok"])
        with patch.object(publish, "build_list_payload", side_effect=TypeError("x")):
            self.assertEqual(self._run(c, _issues(2)), "single")
        self.assertEqual([r["source"] for r in c.upserts[0][0]], ["news_top"])



class TestPublicEntryPoint(unittest.TestCase):
    """main 은 지금처럼 upsert_news_issues(issues, source="news_top") 만 부른다 — 반환은 news_top 저장 여부."""
    def setUp(self):
        db_supabase.ledger.reset()

    def test_news_top_goes_through_bundle_and_returns_bool(self):
        c = _FakeClient(["ok"])
        with patch.object(db_supabase, "get_client", return_value=c):
            self.assertIs(db_supabase.upsert_news_issues(_issues(2), source="news_top"), True)
        self.assertEqual(len(c.upserts[0][0]), 2 + 2)

    def test_other_sources_stay_single_row(self):
        c = _FakeClient(["ok"])
        with patch.object(db_supabase, "get_client", return_value=c):
            self.assertIs(db_supabase.upsert_news_issues(_issues(1), source="custom"), True)
        self.assertEqual([r["source"] for r in c.upserts[0][0]], ["custom"])


if __name__ == "__main__":
    unittest.main()
