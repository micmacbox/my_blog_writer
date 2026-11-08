"""Weekly Food uses its own entry point, credentials and session."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from streamlit.testing.v1 import AppTest


class WeeklyWebTests(unittest.TestCase):
    def test_separate_password_and_selected_topic_workflow(self):
        with patch.dict(os.environ, {'WEEKLY_FOOD_PASSWORD': 'weekly-test', 'BLOG_APP_PASSWORD': 'other-password'}):
            app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'), default_timeout=30).run()
            self.assertFalse(app.exception)
            app.text_input(key='_weekly_login_password').input('other-password')
            app.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertTrue(app.error)
            app.text_input(key='_weekly_login_password').input('weekly-test')
            app.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertFalse(app.tabs)
            self.assertFalse(app.get('file_uploader'))
            app.selectbox(key='weekly_provider').select('근거 정리 (AI 없이)')
            app.checkbox(key='weekly_demo').check()
            app.button(key='weekly_run').click().run()
            self.assertFalse(app.exception)
            selector = next(w for w in app.multiselect if w.key.startswith('weekly_selected_'))
            selector.set_value([1])
            app.button(key='weekly_build').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.session_state['weekly_posts']['drafts']), 1)
            app.sidebar.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertNotIn('weekly_posts', app.session_state)
