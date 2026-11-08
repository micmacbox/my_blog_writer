"""Legacy deployment entry point for the independent review writer only."""
from pathlib import Path
import runpy
import sys

project = Path(__file__).resolve().parent / "review_writer"
sys.path.insert(0, str(project))
runpy.run_path(str(project / "app.py"), run_name="__main__")
