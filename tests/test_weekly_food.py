"""Run with python3 -m unittest discover -s tests -v. No API keys/network required."""
import io
import json
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from weekly_food.ai import AIError, validate_paragraphs, validate_topics
from weekly_food.core import (Evidence, KST, Topic, Window, canonical_url, deduplicate,
                              food_related, group_candidates, group_headlines, rank_topics)
from weekly_food.pipeline import create_drafts, export_files, research, save_report, zip_bytes
from weekly_food.sources import Http, SourceError, collect, naver_search, rss_items, youtube_search


class FoodTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 8, 15, 0, tzinfo=KST)
        self.window = Window.make(now=self.now)

    def source(self, n=1, title="동네빵집 소금크림빵 디저트 소개", publisher=None,
               kind="news", when=None, summary=""):
        return Evidence(title, f"https://news.example/{n}",
                        (when or self.now - timedelta(hours=n)).isoformat(), kind,
                        publisher or f"언론사{n}", publisher or f"news{n}.example", summary=summary)

    def test_korean_calendar_window_and_boundaries(self):
        self.assertEqual(self.window.start, datetime(2026, 11, 2, tzinfo=KST))
        self.assertTrue(self.window.contains(self.window.start))
        self.assertFalse(self.window.contains(self.now))
        self.assertFalse(self.window.contains(self.window.start - timedelta(seconds=1)))
        historical = Window.make(7, date(2026, 10, 30), self.now)
        self.assertEqual(historical.end, datetime(2026, 10, 31, tzinfo=KST))
        for days in [0, 32]:
            with self.assertRaises(ValueError):
                Window.make(days, now=self.now)
        with self.assertRaises(ValueError):
            Window.make(end_date=date(2026, 11, 9), now=self.now)

    def test_invalid_evidence_and_tracking_url(self):
        self.assertEqual(canonical_url("https://example.com/a?utm_source=x&id=1#top"),
                         "https://example.com/a?id=1")
        for bad in ["javascript:alert(1)", "file:///tmp/test", "https://user:pass@example.com"]:
            with self.assertRaises(ValueError):
                canonical_url(bad)
        with self.assertRaises(ValueError):
            Evidence("제목", "https://example.com", "2026-11-08T12:00:00", "news", "매체", "매체")

    def test_duplicates_queries_and_publication_window(self):
        a = self.source(1, title="소금크림빵 새 소식 - 매체A")
        b = self.source(2, title="소금크림빵 새 소식 - 매체B")
        a.queries, b.queries = ["맛집"], ["디저트"]
        old = self.source(3, when=self.window.start - timedelta(hours=1))
        future = self.source(4, when=self.window.end + timedelta(hours=1))
        result = deduplicate([a, b, old, future], self.window)
        self.assertEqual(len(result), 1)
        self.assertEqual(set(result[0].queries), {"맛집", "디저트"})

    def test_rank_requires_independent_publishers(self):
        a, b = self.source(1, publisher="same"), self.source(2, publisher="same")
        self.assertEqual(rank_topics([Topic("빵", "디저트", [a.id, b.id])], [a, b], self.window), [])
        b.publisher_id = "different"
        topics = rank_topics([Topic("빵", "디저트", [a.id, a.id, b.id, "invented"])], [a, b], self.window)
        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0].publisher_count, 2)
        self.assertEqual(len(topics[0].source_ids), 2)
        self.assertTrue(0 <= topics[0].score <= 100)

    def test_historical_views_do_not_imply_historical_engagement(self):
        window = Window.make(7, date(2025, 1, 8), self.now)
        a = self.source(1, kind="youtube", when=window.end - timedelta(days=1))
        a.views = 1_000_000
        ranked = rank_topics([Topic("빵", "디저트", [a.id])], [a], window, min_sources=1)
        self.assertEqual(ranked[0].score_parts["유튜브_일평균_누적조회"], 0)

    def test_fallback_and_focus_are_grounded(self):
        a, b = self.source(), self.source(2, title="동네빵집 소금크림빵 디저트 소식")
        self.assertEqual(len(group_headlines([a, b])), 1)
        focused = group_headlines([a, b], ["없는가게", "소금크림빵"])
        self.assertEqual(focused[0].source_ids, [])
        self.assertEqual(len(focused[1].source_ids), 2)
        self.assertFalse(food_related(self.source(title="신곡, 디저트처럼 달콤한 변신")))

    def test_repeated_food_names_across_different_headlines(self):
        a = self.source(1, title="한우육개장이 소개된 오늘 방송")
        b = self.source(2, title="생생정보 한우육개장 맛집 소식")
        c = self.source(3, title="물류회사 택배맛집 캠페인")
        groups = group_candidates([a, b, c])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].name, "한우육개장")
        self.assertEqual(groups[0].grouping, "keyword")
        self.assertEqual(set(groups[0].source_ids), {a.id, b.id})
        self.assertFalse(food_related(c))

    def test_ai_cannot_invent_sources_or_entity_mentions(self):
        a, b = self.source(), self.source(2, title="다른 음식")
        result = {"topics": [
            {"name": "소금크림빵", "category": "디저트", "source_ids": [a.id, b.id, "fake"]},
            {"name": "없는가게", "category": "맛집", "source_ids": [a.id]},
            {"name": "! !", "category": "음식", "source_ids": [a.id]},
        ]}
        topics = validate_topics(result, [a, b])
        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0].source_ids, [a.id])

    def test_ai_draft_rejects_unknown_refs_experience_and_numbers(self):
        source = self.source()
        for text, ids in [("소금크림빵 관련 내용이 소개됐어요.", ["fake"]),
                          ("제가 다녀온 소금크림빵 맛집이에요.", [source.id]),
                          ("최근 999개 매장에서 판매되고 있어요.", [source.id])]:
            with self.assertRaises(AIError):
                validate_paragraphs({"paragraphs": [{"text": text, "source_ids": ids}]}, [source])
        valid = {"paragraphs": [{"text": "제목에서는 소금크림빵을 소개하고 있어요.", "source_ids": [source.id]}]}
        self.assertEqual(len(validate_paragraphs(valid, [source])), 1)

    def test_rss_html_dates_publisher_and_bad_xml(self):
        xml = b'''<rss><channel><item><title>&lt;b&gt;Bread&lt;/b&gt; news</title>
          <link>https://news.example/a</link><pubDate>Sun, 08 Nov 2026 03:00:00 GMT</pubDate>
          <source url="https://www.publisher.example">Publisher</source></item>
          <item><title>Bad</title><link>javascript:x</link></item></channel></rss>'''
        rows = rss_items(xml, "bread")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].title, "Bread news")
        self.assertEqual(rows[0].publisher_id, "publisher.example")
        with self.assertRaises(SourceError):
            rss_items(b"<!DOCTYPE rss><rss/>", "bread")

    def test_naver_blog_day_filter_and_auth_headers(self):
        http = Mock()
        http.json.return_value = {"items": [
            {"title": "빵집", "link": "https://blog.naver.com/test/1", "postdate": "20261108",
             "bloggername": "작성자", "bloggerlink": "https://blog.naver.com/test"},
            {"title": "지난 글", "link": "https://blog.naver.com/test/2", "postdate": "20261001"},
        ]}
        rows = naver_search(http, "빵집", self.window, "blog", "id", "secret", 20)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].date_precision, "day")
        self.assertEqual(rows[0].publisher_id, "https://blog.naver.com/test")
        self.assertEqual(http.json.call_args.args[0], "https://naverapihub.apigw.ntruss.com/search/v1/blog")
        self.assertEqual(http.json.call_args.kwargs["headers"], {
            "X-NCP-APIGW-API-KEY-ID": "id", "X-NCP-APIGW-API-KEY": "secret"})
        self.assertEqual(http.json.call_args.kwargs["params"]["format"], "json")

    def test_naver_hub_news_and_legacy_keys_use_separate_endpoints(self):
        for mode, url, header in [
            ("hub", "https://naverapihub.apigw.ntruss.com/search/v1/news", "X-NCP-APIGW-API-KEY"),
            ("legacy", "https://openapi.naver.com/v1/search/news.json", "X-Naver-Client-Secret"),
        ]:
            with self.subTest(mode=mode):
                http = Mock()
                http.json.return_value = {"items": [{
                    "title": "빵집 소식", "originallink": "https://publisher.example/news/1",
                    "pubDate": "Sun, 08 Nov 2026 03:00:00 GMT",
                }]}
                rows = naver_search(http, "빵집", self.window, "news", "id", "secret", 20, mode=mode)
                self.assertEqual(len(rows), 1)
                self.assertEqual(http.json.call_args.args[0], url)
                self.assertEqual(http.json.call_args.kwargs["headers"][header], "secret")
                self.assertEqual(http.json.call_count, 1)  # Never silently send the key to both hosts.
        with self.assertRaises(ValueError):
            naver_search(Mock(), "빵", self.window, "news", "id", "secret", 20, mode="unknown")

    def test_naver_collect_routes_configured_mode_and_exposes_error(self):
        credentials = {"NAVER_CLIENT_ID": "id", "NAVER_CLIENT_SECRET": "secret", "NAVER_API_MODE": "legacy"}
        with patch("weekly_food.sources.naver_search", return_value=[]) as search:
            _, status = collect(["빵"], self.window, credentials, ["news"])
            self.assertEqual(search.call_args.kwargs["mode"], "legacy")
            self.assertEqual(status[0]["api_mode"], "legacy")
        http = Mock()
        http.json.return_value = {"error": {"message": "secret-do-not-log"}}
        with self.assertRaises(SourceError) as error:
            naver_search(http, "빵", self.window, "news", "id", "secret", 20)
        self.assertNotIn("secret-do-not-log", str(error.exception))

    def test_youtube_statistics_are_fetched_separately(self):
        http = Mock()
        http.json.side_effect = [
            {"items": [{"id": {"videoId": "abc"}}]},
            {"items": [{"id": "abc", "snippet": {"title": "빵", "publishedAt": "2026-11-08T03:00:00Z",
                          "channelTitle": "채널", "channelId": "channel"},
                        "statistics": {"viewCount": "12000"}}]},
        ]
        rows = youtube_search(http, "빵", self.window, "key", 20)
        self.assertEqual(rows[0].views, 12000)
        self.assertEqual(http.json.call_count, 2)
        params = http.json.call_args_list[0].kwargs["params"]
        self.assertEqual(params["publishedAfter"], "2026-11-01T15:00:00Z")

    def test_network_error_redacts_request_keys(self):
        http = Http()
        http.session.get = Mock(side_effect=requests.RequestException("secret_token=do-not-log"))
        with patch("weekly_food.sources.time.sleep"):
            with self.assertRaises(SourceError) as result:
                http.get("https://example.com?key=secret_token")
        self.assertNotIn("secret_token", str(result.exception))
        self.assertEqual(http.session.get.call_count, 3)

    def test_failure_and_missing_keys_are_visible(self):
        with patch("weekly_food.sources.google_news", side_effect=SourceError("HTTP 503")):
            items, status = collect(["빵", "쿠키"], self.window, {}, ["rss", "youtube"])
        self.assertEqual(items, [])
        self.assertEqual([s["state"] for s in status], ["error", "skipped"])

    def test_demo_export_no_network_no_credentials_no_overwrite(self):
        with patch("weekly_food.pipeline.collect", side_effect=AssertionError("network")):
            report = research(self.window, ["빵"], {"KEY": "TOP_SECRET"}, demo=True)
        create_drafts(report, 2)
        self.assertEqual(len(report["drafts"]), 2)
        self.assertTrue(all("테스트용 가상" in draft["text"] for draft in report["drafts"]))
        self.assertNotIn("TOP_SECRET", json.dumps(report))
        with zipfile.ZipFile(io.BytesIO(zip_bytes(report))) as archive:
            self.assertIn("sources.json", archive.namelist())
            self.assertEqual(len([n for n in archive.namelist() if n.endswith(".txt")]), 2)
        with tempfile.TemporaryDirectory() as directory:
            first, second = save_report(report, directory), save_report(report, directory)
            self.assertNotEqual(first, second)
            self.assertTrue((first / "weekly_food.zip").is_file())

    def test_ai_outage_falls_back_without_losing_report(self):
        writer = Mock()
        writer.draft.side_effect = AIError("AI unavailable")
        report = research(self.window, ["빵"], demo=True)
        report["demo"] = False
        create_drafts(report, 1, writer)
        self.assertEqual(report["drafts"][0]["mode"], "근거 정리")
        self.assertTrue(report["warnings"])

    def test_empty_results_and_invalid_config_do_not_fabricate_drafts(self):
        with patch("weekly_food.pipeline.collect", return_value=([], [])):
            report = research(self.window, ["찾을수없는것"], sources=["rss"])
        create_drafts(report)
        self.assertEqual(report["topics"], [])
        self.assertEqual(report["drafts"], [])
        self.assertTrue(report["warnings"])
        for kwargs in [{"queries": []}, {"queries": ["빵"], "sources": []},
                       {"queries": ["빵"], "limit": 100}]:
            with self.assertRaises(ValueError):
                research(self.window, **kwargs)


if __name__ == "__main__":
    unittest.main()
