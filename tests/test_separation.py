"""Prove project copies function with no sibling checkout or root imports."""
import ast
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SeparationTests(unittest.TestCase):
    def copied(self, project, destination):
        return Path(shutil.copytree(ROOT / project, Path(destination) / project,
            ignore=shutil.ignore_patterns('__pycache__', '.venv', 'output', 'secrets.toml', '.env', '.env.*')))

    def run_project(self, project, arguments):
        with tempfile.TemporaryDirectory() as directory:
            copy = self.copied(project, directory)
            environment = dict(os.environ)
            environment.pop('PYTHONPATH', None)
            result = subprocess.run([sys.executable, *arguments], cwd=copy, env=environment,
                                    text=True, capture_output=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout[-5000:] + result.stderr[-5000:])
            return result.stdout

    def test_no_cross_project_imports_or_parent_path_injection(self):
        for project, forbidden in [('review_writer', {'weekly_food'}),
                                   ('weekly_food', {'review_writer', 'face_mosaic', 'password_gate'})]:
            for path in (ROOT / project).rglob('*.py'):
                if '__pycache__' in path.parts:
                    continue
                tree = ast.parse(path.read_text())
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        names = [a.name.split('.')[0] for a in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        names = [(node.module or '').split('.')[0]]
                    else:
                        continue
                    self.assertFalse(set(names) & forbidden, str(path))
        weekly_dependencies = (ROOT / 'weekly_food/requirements.txt').read_text().lower()
        self.assertNotIn('opencv', weekly_dependencies)
        self.assertNotIn('pymupdf', weekly_dependencies)
        self.assertNotIn('weekly_food', (ROOT / 'review_writer/app.py').read_text())

    def test_weekly_food_cli_from_isolated_project_copy(self):
        output = self.run_project('weekly_food', ['-m', 'weekly_food', '--demo'])
        self.assertIn('원고 2개', output)

    def test_weekly_food_all_tests_from_isolated_copy(self):
        self.run_project('weekly_food', ['-m', 'unittest', 'discover', '-s', 'tests', '-q'])

    def test_review_writer_from_isolated_copy(self):
        self.run_project('review_writer', ['-m', 'unittest', 'discover', '-s', 'tests', '-q'])
