"""Local web UI for the share decision: your people, decided in bulk.

The page is the React build in the repo's `web/dist/` (see `web/README.md`);
this package serves it.
"""

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
WEB_DIST = Path(__file__).resolve().parents[5] / "web" / "dist"
PEOPLE_CSS = WEB_DIST / "people.css"
PEOPLE_HTML = PACKAGE_DIR / "people.html"
PEOPLE_JS = WEB_DIST / "people.js"
# The row virtualizer, vendored (MIT); the Searches page's virtual-table.js imports it.
VIRTUAL_CORE_JS = PACKAGE_DIR / "vendor" / "tanstack-virtual-core.js"

__all__ = ["PEOPLE_CSS", "PEOPLE_HTML", "PEOPLE_JS", "VIRTUAL_CORE_JS"]
