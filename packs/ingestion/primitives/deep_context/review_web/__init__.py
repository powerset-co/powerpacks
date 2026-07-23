"""Local, file-backed review UI for the staged deep-context workflow.

Only packaged asset paths remain compatibility exports. Python callers import
domain, decision, workflow, rendering, server, or CLI behavior from its concrete
owner module so patches affect the code that executes.
"""

from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent
REVIEW_CSS = _PACKAGE_DIR / "reconcile_review.css"
REVIEW_HTML = _PACKAGE_DIR / "reconcile_review.html"
REVIEW_JS = _PACKAGE_DIR / "reconcile_review.js"

__all__ = [
    "REVIEW_CSS",
    "REVIEW_HTML",
    "REVIEW_JS",
]
