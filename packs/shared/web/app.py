"""The local UI's React app: the shell page and its two built assets.

Flow: the review server (and the People test server) asks `AppRoutes.get` first.
GET `/people`, `/searches` or `/searches/run` (any query) -> `app.html`, a `#root` mount whose asset URLs are absolute so
any nested route resolves them; GET `/app/assets/app.js|app.css` -> the build in
the repo's `web/dist/` (see `web/README.md`). Everything else falls through.

Changelog:
  2026-09-26: created; replaces share/web's People page and /people/assets.
  2026-09-26: the shell also answers /searches and /searches/run (the Searches page).
  2026-09-28: the shell also answers /accounts and /tasks (Accounts, Scheduled tasks).
"""

from __future__ import annotations

import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path

WEB_DIST = Path(__file__).resolve().parents[3] / "web" / "dist"
APP_HTML = Path(__file__).resolve().parent / "app.html"
ASSET_PREFIX = "/app/assets/"
# The paths the React router owns; the server answers each with the shell page.
PAGE_PATHS = frozenset({"/people", "/searches", "/searches/run", "/accounts", "/tasks"})
ASSETS = {
    "app.js": (WEB_DIST / "app.js", "text/javascript; charset=utf-8"),
    "app.css": (WEB_DIST / "app.css", "text/css; charset=utf-8"),
}


class AppRoutes:
    """The shell page and its assets, mountable in any stdlib handler."""

    def get(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
        if parsed.path in PAGE_PATHS:
            _send(handler, APP_HTML.read_bytes(), "text/html; charset=utf-8", cache="no-store")
            return True
        if not parsed.path.startswith(ASSET_PREFIX):
            return False
        asset = ASSETS.get(parsed.path[len(ASSET_PREFIX):])
        if asset is None:
            _send(handler, b"not found", "text/plain", cache="no-store", status=HTTPStatus.NOT_FOUND)
        else:
            _send(handler, asset[0].read_bytes(), asset[1], cache="no-cache")
        return True


def _send(handler: BaseHTTPRequestHandler, body: bytes, content_type: str, *, cache: str,
          status: HTTPStatus = HTTPStatus.OK) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", cache)
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(body)
