"""End-to-end tests for authentication and active message enrichment primitives.

Covered primitives:
- powerset_auth (login flow against fake Auth0 + browserless mode, whoami,
  token, logout)
- match_local_candidates (local matcher tiers)
- llm_review_contacts (estimate + review against fake OpenRouter)

Each test spins up a tiny ThreadingHTTPServer and points the primitive at it.
No network calls escape these tests.
"""

from __future__ import annotations

import csv
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

from packs.shared.csv_io import CsvIO


ROOT = Path(__file__).resolve().parents[1]
POWERSET_AUTH = ROOT / "packs/powerset/primitives/auth/auth.py"
MATCH_LOCAL = ROOT / "packs/ingestion/primitives/match_local_candidates/match_local_candidates.py"
LLM_REVIEW = ROOT / "packs/ingestion/primitives/llm_review_contacts/llm_review_contacts.py"


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


class MatchLocalTests(unittest.TestCase):
    def _write_contacts(self, path: Path, rows: list[dict[str, str]]) -> None:
        headers = [
            "phone", "name", "source", "is_in_group_chats", "group_names",
            "message_count", "last_message", "skip", "match_status",
            "matched_person_id", "matched_name", "matched_linkedin_url",
            "match_confidence", "match_method", "match_reason",
        ]
        with path.open("w", newline="") as h:
            w = csv.DictWriter(h, fieldnames=headers)
            w.writeheader()
            for row in rows:
                w.writerow({k: row.get(k, "") for k in headers})

    def _write_candidates(self, path: Path, rows: list[dict[str, str]]) -> None:
        headers = ["id", "name", "linkedin_url", "phone_number", "emails", "public_identifier"]
        with path.open("w", newline="") as h:
            w = csv.DictWriter(h, fieldnames=headers)
            w.writeheader()
            for row in rows:
                w.writerow({k: row.get(k, "") for k in headers})

    def test_default_ignores_legacy_powerset_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            contacts = tmp / "contacts.csv"
            local_people = tmp / "local-people.csv"
            legacy_catalog = tmp / "powerset_contacts.csv"
            self._write_contacts(contacts, [
                {"phone": "+14155550101", "name": "Local Person", "source": "imessage"},
                {"phone": "+14155550102", "name": "Legacy Person", "source": "imessage"},
            ])
            self._write_candidates(legacy_catalog, [
                {"id": "legacy", "name": "Legacy Person", "linkedin_url": "https://l/in/legacy"},
            ])
            with local_people.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=[
                    "id", "full_name", "linkedin_url", "public_identifier",
                    "primary_phone", "all_phones", "primary_email", "all_emails",
                ])
                writer.writeheader()
                writer.writerow({
                    "id": "local",
                    "full_name": "Local Person",
                    "linkedin_url": "https://l/in/local",
                    "public_identifier": "local",
                })

            result = subprocess.run(
                [
                    "python3", str(MATCH_LOCAL), "match",
                    "--contacts", str(contacts),
                    "--local-people", str(local_people),
                    "--review", str(tmp / "missing-review.csv"),
                ],
                cwd=tmp, capture_output=True, text=True, timeout=10,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["candidates_path"], "")
            self.assertEqual(payload["explicit_catalog_candidates"], 0)
            self.assertEqual(payload["local_people_candidates"], 1)
            with contacts.open(newline="") as handle:
                by_name = {row["name"]: row for row in CsvIO.dict_reader(handle)}
            self.assertEqual(by_name["Local Person"]["matched_person_id"], "local")
            self.assertEqual(by_name["Legacy Person"]["match_status"], "unmatched")

    def test_single_token_first_name_suggests_unique_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            contacts = tmp / "contacts.csv"
            candidates = tmp / "candidates.csv"
            self._write_contacts(contacts, [
                {"phone": "+18055550101", "name": "Tanner", "source": "imessage,whatsapp",
                 "message_count": "4949"},
                {"phone": "+14155550202", "name": "Alex", "source": "imessage",
                 "message_count": "50"},
                {"phone": "+14155550303", "name": "Sam", "source": "imessage",
                 "message_count": "5"},
            ])
            self._write_candidates(candidates, [
                {"id": "p1", "name": "Tanner Vega", "linkedin_url": "https://l/in/tanner-vega"},
                {"id": "p2", "name": "Alex Kim", "linkedin_url": "https://l/in/alex-kim"},
                {"id": "p3", "name": "Alex Park", "linkedin_url": "https://l/in/alex-park"},
                # Sam is intentionally absent so we cover the unmatched path.
            ])
            result = subprocess.run(
                ["python3", str(MATCH_LOCAL), "match",
                 "--contacts", str(contacts),
                 "--candidates", str(candidates),
                 "--no-local-people",
                 "--review", str(tmp / "missing-review.csv")],
                cwd=ROOT, capture_output=True, text=True, timeout=10, check=True,
            )
            payload = json.loads(result.stdout)
            self.assertEqual(payload["stats"]["total"], 3)
            self.assertEqual(payload["stats"]["matched"], 0)
            self.assertEqual(payload["stats"]["suggested"], 2)
            self.assertEqual(payload["stats"]["unmatched"], 1)

            with contacts.open(newline="") as h:
                rows = list(CsvIO.dict_reader(h))
            by_phone = {r["phone"]: r for r in rows}

            tanner = by_phone["+18055550101"]
            self.assertEqual(tanner["match_status"], "suggested")
            self.assertEqual(tanner["matched_person_id"], "p1")
            self.assertEqual(tanner["matched_name"], "Tanner Vega")
            self.assertEqual(tanner["match_method"], "name_first_only_unique_suggested")

            alex = by_phone["+14155550202"]
            self.assertEqual(alex["match_status"], "suggested")
            self.assertEqual(alex["match_method"], "name_first_only_ambiguous")

            sam = by_phone["+14155550303"]
            self.assertEqual(sam["match_status"], "unmatched")
            self.assertEqual(sam["match_method"], "unmatched")
            self.assertEqual(sam["match_reason"], "single-token name with no candidate first-name match")

    def test_exact_unique_match(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            contacts = tmp / "contacts.csv"
            candidates = tmp / "candidates.csv"
            self._write_contacts(contacts, [
                {"phone": "+14155550101", "name": "Jane Doe", "source": "imessage"},
                {"phone": "+14155550202", "name": "Multi Match", "source": "imessage"},
                {"phone": "+14155550303", "name": "Amir Mostafavi", "source": "imessage"},
                {"phone": "+14155550404", "name": "Ghost Person", "source": "imessage"},
            ])
            self._write_candidates(candidates, [
                {"id": "p1", "name": "Jane Doe", "linkedin_url": "https://l/in/jane"},
                {"id": "p2", "name": "Multi Match", "linkedin_url": "https://l/in/m1"},
                {"id": "p3", "name": "Multi Match", "linkedin_url": "https://l/in/m2"},
                {"id": "p4", "name": "Amirteymour Mostafavi", "linkedin_url": "https://l/in/amir"},
            ])
            result = subprocess.run(
                ["python3", str(MATCH_LOCAL), "match",
                 "--contacts", str(contacts),
                 "--candidates", str(candidates),
                 "--no-local-people",
                 "--review", str(tmp / "missing-review.csv")],
                cwd=ROOT, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["stats"]["total"], 4)

            with contacts.open(newline="") as h:
                rows = list(CsvIO.dict_reader(h))
            by_phone = {r["phone"]: r for r in rows}
            self.assertEqual(by_phone["+14155550101"]["match_status"], "matched")
            self.assertEqual(by_phone["+14155550101"]["matched_person_id"], "p1")
            self.assertEqual(by_phone["+14155550202"]["match_status"], "suggested")
            self.assertEqual(by_phone["+14155550303"]["match_status"], "matched")
            self.assertEqual(by_phone["+14155550303"]["match_method"], "name_prefix_lastname_linkedin")
            self.assertEqual(by_phone["+14155550404"]["match_status"], "unmatched")


class LlmReviewTests(unittest.TestCase):
    def _write_contacts(self, path: Path) -> None:
        headers = [
            "phone", "name", "source", "is_in_group_chats", "group_names",
            "message_count", "last_message", "skip", "match_status",
            "matched_person_id", "matched_name", "matched_linkedin_url",
            "match_confidence", "match_method", "match_reason",
        ]
        with path.open("w", newline="") as h:
            w = csv.DictWriter(h, fieldnames=headers)
            w.writeheader()
            for i, name in enumerate(["Alice", "Bob", "Carol", "Dan"]):
                w.writerow({
                    "phone": f"+14155550{i:03d}",
                    "name": name,
                    "source": "imessage",
                    "is_in_group_chats": "false",
                    "group_names": "",
                    "message_count": "10",
                    "last_message": "2026-04-01T00:00:00+00:00",
                    "skip": "",
                    "match_status": "unmatched",
                    "matched_person_id": "",
                    "matched_name": "",
                    "matched_linkedin_url": "",
                    "match_confidence": "",
                    "match_method": "unmatched",
                    "match_reason": "",
                })

    def test_estimate_does_not_call_api(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            contacts = tmp / "contacts.csv"
            self._write_contacts(contacts)
            result = subprocess.run(
                ["python3", str(LLM_REVIEW), "estimate",
                 "--input", str(contacts),
                 "--model", "openai/gpt-4.1-mini"],
                cwd=ROOT, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["candidates"], 4)
            self.assertEqual(payload["estimate"]["batches"], 1)
            self.assertGreater(payload["estimate"]["estimated_usd"], 0)

    def test_review_against_fake_openrouter(self) -> None:
        port = _free_port()
        _Handler.routes = {}
        server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                contacts = tmp / "contacts.csv"
                results_jsonl = tmp / "verdicts.jsonl"
                manifest_path = tmp / "manifest.json"
                self._write_contacts(contacts)
                env = {
                    "POWERPACKS_OPENROUTER_BASE": f"http://127.0.0.1:{port}/api/v1",
                    "PATH": "/usr/bin:/bin:/usr/local/bin",
                }
                result = subprocess.run(
                    [
                        "python3", str(LLM_REVIEW), "review",
                        "--input", str(contacts),
                        "--api-key", "test",
                        "--model", "openai/gpt-4.1-mini",
                        "--results", str(results_jsonl),
                        "--manifest", str(manifest_path),
                    ],
                    cwd=ROOT, capture_output=True, text=True, timeout=20, env=env,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["status"], "completed")
                self.assertEqual(payload["counts"]["verdicts"], 4)
                self.assertEqual(payload["counts"]["enrich"], 2)
                self.assertEqual(payload["counts"]["skip"], 2)
                sent_contacts = _Handler.routes.get("openrouter_contacts") or []
                self.assertTrue(sent_contacts)
                for contact in sent_contacts[0]:
                    self.assertEqual(set(contact), {"idx", "name"})

                with contacts.open(newline="") as h:
                    rows = list(CsvIO.dict_reader(h))
                # Indexes 1 and 3 (Bob, Dan) get SKIP per the fake handler.
                by_phone = {r["phone"]: r for r in rows}
                self.assertEqual(by_phone["+141555500001"[:12]]["skip"], "")  # Alice = ENRICH
                self.assertEqual(by_phone["+14155550000"]["skip"], "")
                self.assertEqual(by_phone["+14155550001"]["skip"], "yes")
                self.assertEqual(by_phone["+14155550002"]["skip"], "")
                self.assertEqual(by_phone["+14155550003"]["skip"], "yes")

                lines = [json.loads(line) for line in results_jsonl.read_text().splitlines()]
                self.assertEqual(len(lines), 4)
                self.assertTrue(any(line["verdict"] == "ENRICH" for line in lines))
                self.assertTrue(any(line["verdict"] == "SKIP" for line in lines))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
