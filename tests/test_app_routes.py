"""AppRoutes: the React shell page and its two assets, nothing else."""

from __future__ import annotations

import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from packs.shared.web.app import AppRoutes

FELL_THROUGH = 418


class _Handler(BaseHTTPRequestHandler):
    routes = AppRoutes()

    def do_GET(self) -> None:  # noqa: N802
        if not self.routes.get(self, urllib.parse.urlparse(self.path)):
            self.send_response(FELL_THROUGH)
            self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


class AppRoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _get(self, path: str) -> tuple[int, dict[str, str], bytes]:
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def test_pages_are_the_shell_with_absolute_asset_urls(self) -> None:
        for path in ("/", "/install", "/?stage=worth", "/people", "/searches", "/searches/run?run_id=jordan-role"):
            status, headers, body = self._get(path)
            self.assertEqual((status, headers["Content-Type"]), (HTTPStatus.OK, "text/html; charset=utf-8"), path)
            self.assertIn(b"src='/app/assets/app.js'", body)
            self.assertIn(b"href='/app/assets/app.css'", body)
            self.assertIn(b"data-people-root", body)

    def test_the_two_assets_are_served_uncached_and_nothing_else(self) -> None:
        for name, kind in (("app.js", "text/javascript; charset=utf-8"), ("app.css", "text/css; charset=utf-8")):
            status, headers, body = self._get(f"/app/assets/{name}")
            self.assertEqual((status, headers["Content-Type"], headers["Cache-Control"]),
                             (HTTPStatus.OK, kind, "no-cache"))
            self.assertTrue(body)
        self.assertEqual(self._get("/app/assets/people.js")[0], HTTPStatus.NOT_FOUND)

    def test_other_paths_fall_through(self) -> None:
        for path in ("/review", "/people/", "/people/assets/people.js", "/searches/", "/searches/api/catalog",
                     "/searches/assets/results.js", "/api/people/rows"):
            self.assertEqual(self._get(path)[0], FELL_THROUGH, path)


if __name__ == "__main__":
    unittest.main()
