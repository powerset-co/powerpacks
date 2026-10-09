"""AppRoutes: the React shell page and its two assets, nothing else; and the persistent server's
account routes, which answer beside them before the network store exists."""

from __future__ import annotations

import base64
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from packs.shared.web import account
from packs.shared.web.app import AppRoutes
from packs.shared.web.server import persistent_handler

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


def _token(claims: dict[str, str]) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"synthetic-header.{payload}.synthetic-signature"


class AccountRoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.credentials = self.root / "credentials.json"
        patch.dict("os.environ", {"POWERPACKS_CREDENTIALS_PATH": str(self.credentials)}).start()
        self.addCleanup(patch.stopall)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), persistent_handler(self.root))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _json(self, path: str, method: str = "GET") -> tuple[int, dict]:
        request = urllib.request.Request(self.base + path, method=method, data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_signed_out_without_credentials(self) -> None:
        self.assertEqual(self._json("/api/account"), (200, {"signed_in": False, "email": None, "name": None}))

    def test_signed_in_names_the_token_holder_and_keeps_the_token(self) -> None:
        token = _token({"email": "casey@example.com", "name": "Jordan Bravo"})
        self.credentials.write_text(json.dumps({"access_token": token, "refresh_token": "synthetic-refresh",
                                                "email": "casey@example.com"}))
        status, payload = self._json("/api/account")
        self.assertEqual((status, payload),
                         (200, {"signed_in": True, "email": "casey@example.com", "name": "Jordan Bravo"}))
        self.assertNotIn("synthetic", json.dumps(payload))

    def test_a_token_without_a_name_claim_has_no_name(self) -> None:
        self.credentials.write_text(json.dumps({"access_token": _token({"sub": "auth0|1"}), "email": "casey@example.com"}))
        self.assertEqual(self._json("/api/account")[1], {"signed_in": True, "email": "casey@example.com", "name": None})

    def test_sign_in_answers_the_login_page_once_its_callback_listens(self) -> None:
        def login(args, on_authorize_url=None):
            on_authorize_url(f"https://{args.auth0_domain}/authorize?state=synthetic")
            return 0

        (self.root / ".env").write_text("POWERPACKS_AUTH0_DOMAIN=synthetic.auth0.example\n")
        with patch.object(account.auth, "cmd_login", side_effect=login):
            status, payload = self._json("/api/account/signin", "POST")
        self.assertEqual((status, payload), (200, {"url": "https://synthetic.auth0.example/authorize?state=synthetic"}))

    def test_sign_in_that_cannot_start_is_an_error(self) -> None:
        with patch.object(account.auth, "cmd_login", return_value=1):
            status, payload = self._json("/api/account/signin", "POST")
        self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertIn("could not start", payload["error"])


if __name__ == "__main__":
    unittest.main()
