"""Local web UI for the share decision: your people, decided in bulk."""

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PEOPLE_CSS = PACKAGE_DIR / "people.css"
PEOPLE_HTML = PACKAGE_DIR / "people.html"
PEOPLE_JS = PACKAGE_DIR / "people.js"

__all__ = ["PEOPLE_CSS", "PEOPLE_HTML", "PEOPLE_JS"]
