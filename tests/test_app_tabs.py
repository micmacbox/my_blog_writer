"""Exercise the shared web entry point without network or AI requests."""
import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch


@unittest.skipUnless(importlib.util.find_spec("cv2"), "웹앱 테스트에는 requirements.txt의 OpenCV가 필요합니다.")
class AppTabsTests(unittest.TestCase):
    def test_login_separate_tabs_state_validation_and_logout(self):
        from streamlit.testing.v1 import AppTest

        app_path = Path(__file__).resolve().parents[1] / "app.py"
        with patch.dict(os.environ, {"BLOG_APP_PASSWORD": "test-tabs-only", "BLOG_APP_ENV": "cloud"}):
            app = AppTest.from_file(str(app_path), default_timeout=30).run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.tabs), 0)

            app.text_input(key="_login_password").input("test-tabs-only")
            app.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertEqual([t.label for t in app.tabs], ["방문 후기 작성", "주간 푸드 트렌드"])
            self.assertIn("사진 업로드", [w.label for w in app.tabs[0].get("file_uploader")])
            self.assertEqual(app.tabs[1].text_area(key="weekly_queries").label, "검색어 (한 줄에 하나)")
            self.assertEqual([b.label for b in app.sidebar.button], ["로그아웃"])
            self.assertEqual(app.selectbox(key="weekly_naver_mode").value, "NAVER API HUB (신규 발급)")
            app.selectbox(key="weekly_naver_mode").select("기존 개발자센터 (이전 발급)").run()
            self.assertFalse(app.exception)
            self.assertEqual(app.selectbox(key="weekly_naver_mode").value, "기존 개발자센터 (이전 발급)")

            app.text_input(key="generation_thumbnail_title").input("후기 탭 입력 유지")
            app.selectbox(key="weekly_provider").select("근거 정리 (AI 없이)")
            app.checkbox(key="weekly_demo").check()
            app.button(key="weekly_run").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.session_state["weekly_report"]["drafts"]), 0)
            choices = next(w for w in app.multiselect if w.key.startswith("weekly_selected_"))
            choices.set_value([1])
            app.button(key="weekly_build").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.session_state["weekly_posts"]["drafts"]), 1)
            self.assertEqual(app.session_state["weekly_posts"]["drafts"][0]["topic"],
                             app.session_state["weekly_report"]["topics"][1]["name"])
            self.assertEqual(app.text_input(key="generation_thumbnail_title").value, "후기 탭 입력 유지")

            # A review validation error must leave the other tab and its results visible.
            app.selectbox(key="ai_provider").select("Groq").run()
            app.text_input(key="api_key_Groq").input("")
            next(b for b in app.button if b.label == "원고 생성").click().run()
            self.assertFalse(app.exception)
            self.assertTrue(app.error)
            self.assertEqual(len(app.tabs), 2)
            self.assertEqual(len(app.tabs[1].metric), 3)
            self.assertEqual(app.text_input(key="generation_thumbnail_title").value, "후기 탭 입력 유지")

            app.sidebar.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.tabs), 0)
            self.assertNotIn("weekly_report", app.session_state)
            self.assertNotIn("weekly_posts", app.session_state)


if __name__ == "__main__":
    unittest.main()
