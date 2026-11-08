"""Compatibility entry point for the review writer asset validator."""
from pathlib import Path
import runpy

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "review_writer" / "scripts" / "prepare_naver_post.py"), run_name="__main__")
