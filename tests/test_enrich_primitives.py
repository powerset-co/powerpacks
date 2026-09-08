"""End-to-end tests for authentication and active message enrichment primitives.

Covered primitives:
- powerset_auth (login flow against fake Auth0 + browserless mode, whoami,
  token, logout)

Each test spins up a tiny ThreadingHTTPServer and points the primitive at it.
No network calls escape these tests.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path



ROOT = Path(__file__).resolve().parents[1]
POWERSET_AUTH = ROOT / "packs/powerset/primitives/auth/auth.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Fake servers
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    routes: dict = {}

    def log_message(self, format, *args):  # noqa: A002
        return

    def _json(self, status: int, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            return json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return None

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/v2/contacts":
            page = int(params.get("page", ["0"])[0])
            page_size = int(params.get("page_size", ["200"])[0])
            all_rows = self.routes["candidates"]
            start = page * page_size
            slice_ = all_rows[start:start + page_size]
            return self._json(200, {"data": slice_, "total_count": len(all_rows)})
        return self._json(404, {"error": "not found", "path": parsed.path})

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/oauth/token":
            payload = self._read_json() or {}
            grant = payload.get("grant_type")
            if grant == "authorization_code":
                if payload.get("code") != self.routes.get("auth_code"):
                    return self._json(400, {"error": "invalid_code"})
                return self._json(200, {
                    "access_token": self.routes["access_token"],
                    "refresh_token": "rt-test",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                })
            if grant == "refresh_token":
                return self._json(200, {
                    "access_token": self.routes["access_token"] + "-refreshed",
                    "refresh_token": "rt-test-2",
                    "expires_in": 3600,
                })
            return self._json(400, {"error": "bad_grant"})
        if parsed.path == "/api/v1/chat/completions":
            payload = self._read_json() or {}
            # Echo a deterministic verdict per contact (alternating).
            content = payload.get("messages", [{}])[0].get("content", "")
            # Detect the "Contacts to evaluate" json by simple parsing.
            try:
                json_part = content.split("Contacts to evaluate:\n", 1)[1]
                json_part = json_part.split("\n\nRespond", 1)[0]
                contacts = json.loads(json_part)
            except Exception:
                contacts = []
            self.routes.setdefault("openrouter_contacts", []).append(contacts)
            results = []
            for c in contacts:
                idx = c.get("idx")
                verdict = "ENRICH" if idx % 2 == 0 else "SKIP"
                results.append({
                    "idx": idx,
                    "name": c.get("name"),
                    "verdict": verdict,
                    "reason": "test",
                })
            return self._json(200, {
                "choices": [{"message": {"content": json.dumps({"results": results})}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            })
        return self._json(404, {"error": "not found", "path": parsed.path})


def _make_jwt(payload: dict) -> str:
    """Build an unsigned JWT (header.payload.fake-sig) good enough for the
    primitives' email-extraction helper."""
    import base64

    def b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")
    header = b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    body = b64(json.dumps(payload).encode())
    sig = b64(b"sig")
    return f"{header}.{body}.{sig}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class PowersetAuthTests(unittest.TestCase):
    def test_login_with_fake_auth0_no_browser(self) -> None:
        port = _free_port()
        callback_port = _free_port()
        access_token = _make_jwt({"email": "alice@example.com"})
        _Handler.routes = {
            "auth_code": "test-code",
            "access_token": access_token,
        }
        server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                creds_path = Path(td) / "creds.json"
                # Run login in a subprocess; it will block waiting for the
                # callback. Spawn a helper thread that hits /callback after a
                # short delay to mimic the user finishing the Auth0 redirect.

                def hit_callback():
                    time.sleep(1.0)
                    # Need to read state from the URL the login process logs;
                    # easier: open a connection that sends our test code/state
                    # using the *expected* state. Since the primitive
                    # generates a random state and waits for it to come back,
                    # we need to intercept the printed authorize URL.
                    pass

                # Easier path: drive the primitive's flow by sending the
                # callback request with a known state we extracted from the
                # printed authorize URL.
                proc = subprocess.Popen(
                    [
                        "python3", str(POWERSET_AUTH), "login",
                        "--no-browser",
                        "--auth0-domain", f"http://127.0.0.1:{port}",
                        "--client-id", "test-client",
                        "--audience", "https://api.test/",
                        "--callback-port", str(callback_port),
                        "--credentials-path", str(creds_path),
                        "--timeout", "20",
                    ],
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                # Read the URL from stderr ("open this URL if your browser…").
                deadline = time.time() + 10
                authorize_url = None
                stderr_buf = ""
                while time.time() < deadline:
                    line = proc.stderr.readline()
                    if not line:
                        if proc.poll() is not None:
                            break
                        continue
                    stderr_buf += line
                    if "open this URL" in line:
                        authorize_url = line.split("open this URL if your browser did not launch:", 1)[1].strip()
                        break
                self.assertIsNotNone(authorize_url, f"did not see authorize URL: {stderr_buf}")

                # Our fake Auth0 uses http (not https); rewrite the scheme so
                # we can drive the redirect ourselves.
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(authorize_url).query)
                state = qs["state"][0]
                redirect_uri = qs["redirect_uri"][0]
                callback_url = f"{redirect_uri}?code=test-code&state={state}"
                # Hit the local callback so the primitive completes.
                urllib.request.urlopen(callback_url, timeout=5).read()

                stdout, stderr = proc.communicate(timeout=15)
                self.assertEqual(proc.returncode, 0, stderr)
                manifest = json.loads(stdout)
                self.assertEqual(manifest["status"], "ok")
                self.assertEqual(manifest["email"], "alice@example.com")

                # whoami should now report logged_in.
                whoami = subprocess.run(
                    ["python3", str(POWERSET_AUTH), "whoami",
                     "--credentials-path", str(creds_path)],
                    capture_output=True, text=True, check=True,
                )
                whoami_out = json.loads(whoami.stdout)
                self.assertEqual(whoami_out["status"], "logged_in")
                self.assertEqual(whoami_out["email"], "alice@example.com")

                # token --bearer-only prints the access token.
                token = subprocess.run(
                    ["python3", str(POWERSET_AUTH), "token", "--bearer-only",
                     "--credentials-path", str(creds_path),
                     "--auth0-domain", f"http://127.0.0.1:{port}",
                     "--client-id", "test-client"],
                    capture_output=True, text=True, check=True,
                )
                self.assertEqual(token.stdout.strip(), access_token)

                # logout removes the file.
                subprocess.run(
                    ["python3", str(POWERSET_AUTH), "logout",
                     "--credentials-path", str(creds_path)],
                    capture_output=True, text=True, check=True,
                )
                self.assertFalse(creds_path.exists())
        finally:
            server.shutdown()
            server.server_close()



    def test_login_requires_explicit_auth0_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = os.environ.copy()
            for key in ("POWERPACKS_AUTH0_DOMAIN", "POWERPACKS_AUTH0_CLIENT_ID", "POWERPACKS_AUTH0_AUDIENCE"):
                env.pop(key, None)
            result = subprocess.run(
                [
                    "python3", str(POWERSET_AUTH), "login",
                    "--no-browser",
                    "--credentials-path", str(Path(td) / "creds.json"),
                    "--timeout", "1",
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "failed")
        self.assertIn("POWERPACKS_AUTH0_DOMAIN", payload["error"])
        self.assertIn("env.powerset.example", payload["error"])

    def test_whoami_and_logout_do_not_require_auth0_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            creds_path = Path(td) / "creds.json"
            creds_path.write_text(json.dumps({
                "access_token": _make_jwt({"email": "cached@example.com"}),
                "expires_at": time.time() + 3600,
                "email": "cached@example.com",
            }))
            env = os.environ.copy()
            for key in ("POWERPACKS_AUTH0_DOMAIN", "POWERPACKS_AUTH0_CLIENT_ID", "POWERPACKS_AUTH0_AUDIENCE"):
                env.pop(key, None)
            whoami = subprocess.run(
                ["python3", str(POWERSET_AUTH), "whoami", "--credentials-path", str(creds_path)],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(whoami.returncode, 0, whoami.stderr)
            self.assertEqual(json.loads(whoami.stdout)["email"], "cached@example.com")
            logout = subprocess.run(
                ["python3", str(POWERSET_AUTH), "logout", "--credentials-path", str(creds_path)],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(logout.returncode, 0, logout.stderr)
            self.assertFalse(creds_path.exists())


if __name__ == "__main__":
    unittest.main()
