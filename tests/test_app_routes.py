"""AppRoutes: the React shell page and its two assets, nothing else; and the persistent server's
account and feedback routes, which answer beside them before the network store exists."""

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

from packs.shared.web import account, feedback
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


# A person as the People drawer's flag sends them (web/src/lib/api/feedback.ts sendPersonFeedback).
PERSON = {"parent_id": "p1", "name": "Jordan Bravo", "public_identifier": "jordan-bravo", "title": "Engineer",
          "company": "Acme", "worth": "yes", "worth_source": "model", "share": "confirm",
          "share_source": "model", "reason": "unsure", "labels": ["coworker"], "confidence": 0.55}


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

    def _post(self, path: str, payload: dict) -> tuple[int, dict]:
        request = urllib.request.Request(self.base + path, method="POST", data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def _sender(self, reply: dict) -> object:
        (self.root / ".release-please-manifest.json").write_text('{".": "3.22.2"}')
        sender = patch.object(feedback, "SendFeedback").start()
        sender.return_value.run.return_value = reply
        return sender

    def test_feedback_files_the_note_with_the_person_and_the_release(self) -> None:
        sender = self._sender({"status": "submitted", "feedback_id": "row-1"})
        status, payload = self._post("/api/feedback", {"comment": "  Should be shared.  ", "person": PERSON})
        self.assertEqual((status, payload["feedback_id"]), (HTTPStatus.OK, "row-1"))
        request = sender.call_args.args[0]
        self.assertEqual((request.comment, request.category), ("Should be shared.", "person"))
        self.assertEqual(request.metadata,
                         {"source": "powerpacks-people", "powerpacks_version": "3.22.2", "person": PERSON})
        self.assertEqual(sender.call_args.kwargs["env_file"], self.root / ".env")

    def test_feedback_needs_a_note(self) -> None:
        sender = self._sender({"status": "submitted"})
        self.assertEqual(self._post("/api/feedback", {"comment": " ", "person": PERSON})[0], HTTPStatus.BAD_REQUEST)
        sender.assert_not_called()

    def test_feedback_without_a_sign_in_says_so(self) -> None:
        self._sender({"status": "needs_auth", "error": "not signed in to Powerset; run `$powerset login` first"})
        status, payload = self._post("/api/feedback", {"comment": "Should be shared.", "person": PERSON})
        self.assertEqual((status, payload["error"]), (HTTPStatus.UNAUTHORIZED, "Sign in to Powerset again, then send."))

    def test_signed_out_without_credentials(self) -> None:
        self.assertEqual(self._json("/api/account"),
                         (200, {"signed_in": False, "email": None, "name": None, "picture": None}))

    def test_signed_in_names_who_the_login_stored_and_keeps_the_token(self) -> None:
        self.credentials.write_text(json.dumps({
            "access_token": _token({"email": "casey@example.com"}), "refresh_token": "synthetic-refresh",
            "email": "casey@example.com", "name": "Jordan Bravo", "picture": "https://pictures.example.test/j.jpg"}))
        status, payload = self._json("/api/account")
        self.assertEqual((status, payload), (200, {"signed_in": True, "email": "casey@example.com",
                                                   "name": "Jordan Bravo",
                                                   "picture": "https://pictures.example.test/j.jpg"}))
        self.assertNotIn("synthetic", json.dumps(payload))

    def test_a_login_that_stored_no_name_has_none(self) -> None:
        for stored in ({}, {"name": "casey@example.com", "picture": None}):
            self.credentials.write_text(json.dumps({"access_token": _token({"sub": "auth0|1"}),
                                                    "email": "casey@example.com", **stored}))
            self.assertEqual(self._json("/api/account")[1],
                             {"signed_in": True, "email": "casey@example.com", "name": None, "picture": None})

    def test_login_uses_current_config_and_named_credentials(self) -> None:
        from packs.search.primitives.deep_search.results_web import server

        (self.root / ".env").write_text(
            "POWERPACKS_AUTH0_DOMAIN=synthetic.auth0.example\n"
            "POWERPACKS_AUTH0_CLIENT_ID=synthetic-client\n"
            "POWERPACKS_AUTH0_AUDIENCE=https://synthetic.invalid\n")
        with patch.object(server, "ROOT", self.root), patch.object(account.auth, "main", return_value=0) as login:
            self.assertEqual(server._login(), 0)
        self.assertEqual(login.call_args.args[0], [
            "login", "--auth0-domain", "synthetic.auth0.example", "--client-id", "synthetic-client",
            "--audience", "https://synthetic.invalid", "--credentials-path", str(self.credentials)])

    def test_login_routes_defer_to_setup_without_starting_another_login(self) -> None:
        from packs.search.primitives.deep_search.results_web import server

        http = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.root, lambda: ()))
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        self.base = f"http://127.0.0.1:{http.server_address[1]}"
        action = {"action": {"kind": "signin", "url": "https://synthetic.invalid"}}
        with patch.object(server, "ROOT", self.root), \
             patch("packs.powerset.primitives.install.status.InstallStatus.read", return_value=action), \
             patch.object(server.AppLogin, "start") as start, patch.object(server, "_login") as login:
            for path in ("/auth/login/start", "/auth/login"):
                status, payload = self._json(path, "POST")
                self.assertEqual(status, HTTPStatus.CONFLICT)
                self.assertEqual(payload["setup_url"], "/install")
            start.assert_not_called()
            login.assert_not_called()



if __name__ == "__main__":
    unittest.main()
