"""Review writer runs and authenticates without the Weekly Food project."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from streamlit.testing.v1 import AppTest


class ReviewWebTests(unittest.TestCase):
    def test_review_login_and_writer_without_weekly_controls(self):
        with patch.dict(os.environ, {'BLOG_APP_PASSWORD': 'review-test', 'BLOG_APP_ENV': 'cloud'}):
            app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'), default_timeout=30).run()
            self.assertFalse(app.exception)
            self.assertFalse(app.tabs)
            app.text_input(key='_login_password').input('review-test')
            app.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertFalse(app.tabs)
            self.assertTrue(any(w.label == '사진 업로드' for w in app.get('file_uploader')))
            self.assertFalse(any(w.key and w.key.startswith('weekly_') for w in app.selectbox))
            app.sidebar.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any('개인 원고 작성기' in t.value for t in app.title))
