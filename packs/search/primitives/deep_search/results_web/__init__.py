"""Static, file-backed viewer for completed deep-search results."""

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
RESULTS_CSS = PACKAGE_DIR / "results.css"
RESULTS_HTML = PACKAGE_DIR / "results.html"
RESULTS_JS = PACKAGE_DIR / "results.js"

__all__ = ["RESULTS_CSS", "RESULTS_HTML", "RESULTS_JS"]
