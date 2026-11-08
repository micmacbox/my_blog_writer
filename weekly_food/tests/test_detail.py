"""Selection, extra evidence, licensing, failure isolation and portable image export."""
import base64
import copy
import io
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from weekly_food.core import Evidence, KST, Window
from weekly_food.detail import build_selected_posts
from weekly_food.media import generated_images, image_asset, licensed_photos
from weekly_food.pipeline import export_files, research, save_report, zip_bytes
from weekly_food.sources import SourceError


class DetailTests(unittest.TestCase):
    def setUp(self):
        self.report = research(Window.make(), ["음식"], demo=True)
        buffer = io.BytesIO()
        Image.new("RGB", (24, 16), "white").save(buffer, "PNG")
        self.raw = buffer.getvalue()

    def asset(self):
        return image_asset(self.raw, self.report["topics"][0]["name"], 1, {
            "kind": "commons", "url": "https://commons.wikimedia.org/wiki/File:Food.jpg",
            "caption": "참고 사진", "author": "Author", "license": "CC BY-SA 4.0",
            "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
            "attribution": "Author / CC BY-SA 4.0 / source link · 크기 변환",
        })

    def test_demo_only_writes_selected_topics_and_makes_no_paid_requests(self):
        before = copy.deepcopy(self.report)
        with patch("weekly_food.detail.collect", side_effect=AssertionError("network")), \
             patch("weekly_food.detail.generated_images", side_effect=AssertionError("paid image")):
            result = build_selected_posts(self.report, [1], image_mode="ai")
        self.assertEqual(self.report, before)
        self.assertEqual(len(result["drafts"]), 1)
        self.assertEqual(result["drafts"][0]["topic"], self.report["topics"][1]["name"])
        self.assertEqual(result["images"], [])

    def test_invalid_selection_and_missing_image_key_rejected_before_network(self):
        for selection in ([], [-1], [99], [0, 0]):
            with self.assertRaises(ValueError):
                build_selected_posts(self.report, selection)
        self.report["demo"] = False
        with self.assertRaises(ValueError):
            build_selected_posts(self.report, [0], image_mode="ai", image_key="")

    def test_extra_evidence_and_background_do_not_change_weekly_rank(self):
        self.report["demo"] = False
        name = self.report["topics"][0]["name"]
        timestamp = datetime.now(KST) - timedelta(days=10)
        related = Evidence(name + " 재료 설명", "https://extra.example/food", timestamp.isoformat(),
                           "news", "다른 매체", "extra.example", summary=name + "에 쓰는 재료를 설명합니다.")
        unrelated = Evidence("관련 없는 소식", "https://extra.example/other", timestamp.isoformat(),
                             "news", "다른 매체", "extra.example")
        background = Evidence(name, "https://ko.wikipedia.org/wiki/Food", datetime.now(KST).isoformat(),
                              "reference", "위키백과", "ko.wikipedia.org", summary="음식 배경 설명입니다.", date_precision="retrieved")
        with patch("weekly_food.detail.collect", return_value=([related, unrelated], [])) as fetch, \
             patch("weekly_food.detail.background_info", return_value=[background]):
            result = build_selected_posts(self.report, [0], image_mode="none")
        self.assertTrue(any("계량" in q for q in fetch.call_args.args[0]))
        selected = result["selected_topics"][0]
        self.assertIn(related.id, selected["source_ids"])
        self.assertIn(background.id, selected["source_ids"])
        self.assertNotIn(unrelated.id, selected["source_ids"])
        self.assertEqual(result["topics"], self.report["topics"])
        self.assertEqual(selected["detail_added_count"], 2)
        self.assertIn("게시일 아님", result["drafts"][0]["text"])

    def test_image_failure_keeps_the_written_draft(self):
        self.report["demo"] = False
        with patch("weekly_food.detail.collect", return_value=([], [])), \
             patch("weekly_food.detail.background_info", return_value=[]), \
             patch("weekly_food.detail.licensed_photos", side_effect=SourceError("사진 없음")):
            result = build_selected_posts(self.report, [0])
        self.assertEqual(len(result["drafts"]), 1)
        self.assertEqual(result["images"], [])
        self.assertTrue(any(s["state"] == "error" for s in result["detail_status"]))
        self.assertTrue(any("사진 없음" in w for w in result["warnings"]))

    def test_photo_license_and_download_host_are_checked(self):
        metadata = lambda license_url: {
            "LicenseUrl": {"value": license_url}, "LicenseShortName": {"value": "CC BY-SA 4.0"},
            "Artist": {"value": "<b>Author</b>"},
        }
        info = {"mime": "image/jpeg", "thumburl": "https://thumb.wikimedia.org/food.jpg",
                "url": "https://upload.wikimedia.org/food.jpg",
                "descriptionurl": "https://commons.wikimedia.org/wiki/File:Food.jpg"}
        fake_http = Mock()
        fake_http.json.return_value = {"query": {"pages": {
            "1": {"index": 1, "imageinfo": [{**info, "extmetadata": metadata("https://example.com/all-rights-reserved")}]},
            "2": {"index": 2, "imageinfo": [{**info, "thumburl": "http://127.0.0.1/secret", "extmetadata": metadata("https://creativecommons.org/licenses/by-sa/4.0/")}]},
            "3": {"index": 3, "imageinfo": [{**info, "extmetadata": metadata("https://creativecommons.org/licenses/by-sa/4.0/")}]},
        }}}
        response = Mock(status_code=200)
        response.iter_content.return_value = [self.raw]
        fake_http.get.return_value = response
        with patch("weekly_food.media.Http", return_value=fake_http):
            assets = licensed_photos("food", "음식", 1)
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["author"], "Author")
        self.assertEqual(fake_http.get.call_count, 1)
        self.assertEqual(fake_http.get.call_args.kwargs["allow_redirects"], False)

    def test_export_contains_real_image_bytes_and_attribution_without_base64_json(self):
        result = build_selected_posts(self.report, [0], image_mode="none")
        result["images"] = [self.asset()]
        files = export_files(result)
        self.assertIn("image_credits.md", files)
        portable = json.loads(files["report.json"])
        self.assertNotIn("data_base64", portable["images"][0])
        markdown = next(v for k, v in files.items() if k.startswith("drafts/") and k.endswith(".md"))
        self.assertIn("../images/", markdown)
        self.assertIn("CC BY-SA", markdown)
        with zipfile.ZipFile(io.BytesIO(zip_bytes(result))) as archive:
            name = result["images"][0]["filename"]
            with Image.open(io.BytesIO(archive.read(name))) as image:
                self.assertEqual(image.size, (24, 16))
        with tempfile.TemporaryDirectory() as directory:
            saved = save_report(result, directory)
            self.assertTrue((saved / result["images"][0]["filename"]).is_file())

    def test_non_image_response_cannot_be_exported_as_a_photo(self):
        with self.assertRaises(SourceError):
            image_asset(b"<html>login required</html>", "음식", 1, {})

    def test_ai_adapter_saves_provenance_and_never_stores_api_key(self):
        part = SimpleNamespace(inline_data=SimpleNamespace(data=self.raw, mime_type="image/png"))
        response = SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])
        with patch("google.genai.Client") as client:
            request = client.return_value.__enter__.return_value.models.generate_content
            request.return_value = response
            assets = generated_images("칼국수", "칼국수 한 그릇", "private-api-key", "test-image-model", 1)
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["kind"], "ai")
        self.assertIn("AI 생성", assets[0]["caption"])
        self.assertEqual(assets[0]["model"], "test-image-model")
        self.assertNotIn("private-api-key", json.dumps(assets))
        self.assertIn("칼국수", request.call_args.kwargs["contents"])


if __name__ == "__main__":
    unittest.main()
