"""Managed login owns its browser and callback listener; manual login needs neither runtime."""
import argparse
import contextlib
import io
import json
import socket
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

from packs.powerset.primitives.auth import auth


class PowersetBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.args = argparse.Namespace(
            auth0_domain="https://auth.example.test", client_id="synthetic-client",
            audience="https://api.example.test", scopes="openid email offline_access",
            callback_host="localhost", callback_port=port, timeout=1,
            no_browser=False, force_account=False,
            credentials_path=Path(self.temporary.name) / "credentials.json",
        )

    def login(self, browser):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()), \
                patch.object(auth, "_login_in_browser", side_effect=browser, create=True), \
                patch("webbrowser.open"), \
                patch.object(auth, "_post_json", return_value=(200, {"access_token": "synthetic.jwt.token"}, "")) as exchange:
            code = auth.cmd_login(self.args)
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", self.args.callback_port))
        return code, json.loads(output.getvalue()), exchange

    def callback(self, url, timeout, *, error=False, invalid_state=False):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertEqual(len(params["code_challenge"][0]), 43)
        if self.args.force_account:
            self.assertEqual(params["prompt"], ["login"])
        query = {"state": "wrong-state" if invalid_state else params["state"][0]}
        query.update({"error": "access_denied"} if error else {"code": "synthetic-code"})
        try:
            urllib.request.urlopen(params["redirect_uri"][0] + "?" + urllib.parse.urlencode(query), timeout=2).close()
        except urllib.error.HTTPError:
            pass

    def test_managed_login_starts_callback_before_browser_and_preserves_pkce_and_force_account(self):
        self.args.force_account = True
        code, payload, exchange = self.login(self.callback)
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(exchange.call_args.args[1]["code"], "synthetic-code")
        self.assertEqual(len(exchange.call_args.args[1]["code_verifier"]), 86)
        self.assertTrue(self.args.credentials_path.exists())

    def test_browser_startup_failure_closes_callback_without_saving_credentials(self):
        code, payload, exchange = self.login(RuntimeError("Install Google Chrome or Brave to continue."))
        self.assertEqual(code, 1)
        self.assertIn("Install Google Chrome", payload["error"])
        exchange.assert_not_called()
        self.assertFalse(self.args.credentials_path.exists())

    def test_callback_error_closes_listener_without_token_exchange(self):
        code, payload, exchange = self.login(lambda url, timeout: self.callback(url, timeout, error=True))
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"], "access_denied")
        exchange.assert_not_called()

    def test_login_timeout_closes_callback_listener(self):
        code, payload, exchange = self.login(lambda *_: None)
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"], "login timed out")
        exchange.assert_not_called()

    def test_state_mismatch_rejects_callback_without_token_exchange(self):
        code, payload, exchange = self.login(lambda url, timeout: self.callback(url, timeout, invalid_state=True))
        self.assertEqual(code, 1)
        self.assertIn("Invalid state parameter", payload["error"])
        exchange.assert_not_called()
        self.assertFalse(self.args.credentials_path.exists())

    def test_no_browser_never_prepares_managed_runtime(self):
        self.args.no_browser = True
        code, payload, exchange = self.login(AssertionError("manual login must not start managed Chrome"))
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"], "login timed out")
        exchange.assert_not_called()
