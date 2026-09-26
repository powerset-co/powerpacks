"""Local web UI for the share decision: who leaves the laptop, decided in bulk."""

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
SHARE_CSS = PACKAGE_DIR / "share.css"
SHARE_HTML = PACKAGE_DIR / "share.html"
SHARE_JS = PACKAGE_DIR / "share.js"

__all__ = ["SHARE_CSS", "SHARE_HTML", "SHARE_JS"]
