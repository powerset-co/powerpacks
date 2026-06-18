"""End-to-end tests for the WhatsApp pack primitives.

Exercise:
- waha_runtime check (just confirms it returns valid JSON without erroring)
- waha_session status / health against a connection-refused URL (fast path)
- extract_whatsapp_contacts extract against a fake in-process WAHA server
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from packs.shared.csv_io import CsvIO


ROOT = Path(__file__).resolve().parents[1]
WAHA_RUNTIME = ROOT / "packs/messages/primitives/waha_runtime/waha_runtime.py"
WAHA_SESSION = ROOT / "packs/messages/primitives/waha_session/waha_session.py"
EXTRACT_WHATSAPP = ROOT / "packs/messages/primitives/extract_whatsapp_contacts/extract_whatsapp_contacts.py"

_WAHA_RUNTIME_SPEC = importlib.util.spec_from_file_location("waha_runtime", WAHA_RUNTIME)
assert _WAHA_RUNTIME_SPEC and _WAHA_RUNTIME_SPEC.loader
waha_runtime = importlib.util.module_from_spec(_WAHA_RUNTIME_SPEC)
_WAHA_RUNTIME_SPEC.loader.exec_module(waha_runtime)

_WAHA_SESSION_SPEC = importlib.util.spec_from_file_location("waha_session", WAHA_SESSION)
assert _WAHA_SESSION_SPEC and _WAHA_SESSION_SPEC.loader
waha_session = importlib.util.module_from_spec(_WAHA_SESSION_SPEC)
_WAHA_SESSION_SPEC.loader.exec_module(waha_session)


# ---------------------------------------------------------------------------
# Fake WAHA HTTP server
# ---------------------------------------------------------------------------

class FakeWAHAHandler(BaseHTTPRequestHandler):
    """Minimal WAHA-shaped HTTP server for extraction tests."""

    routes: dict[str, object] = {}
    request_counts: dict[str, int] = {}

    def log_message(self, format, *args):  # noqa: A002 - silence test logs
        return

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        self.request_counts[path] = self.request_counts.get(path, 0) + 1
        # /api/sessions/{name}
        if path == "/api/sessions/default":
            return self._json(200, {"name": "default", "status": "WORKING", "engine": {"engine": "NOWEB"}})
        if path == "/api/sessions":
            return self._json(200, [{"name": "default", "status": "WORKING"}])
        if path == "/api/default/chats":
            return self._json(200, self._page(self.routes["chats"], params))
        if path == "/api/contacts/all":
            if self.routes.get("ignore_contacts_pagination"):
                return self._json(200, self.routes["contacts"])
            return self._json(200, self._page(self.routes["contacts"], params))
        if path.startswith("/api/default/groups/") and "/participants" not in path:
            group_id = path.split("/")[-1]
            metadata_errors = self.routes.get("group_metadata_errors", {})
            if isinstance(metadata_errors, dict) and group_id in metadata_errors:
                status, payload = metadata_errors[group_id]
                return self._json(status, payload)
            metadata = self.routes.get("group_metadata", {}).get(group_id)
            if metadata is not None:
                return self._json(200, metadata)
            return self._json(404, {"error": "not found"})
        if path.startswith("/api/default/groups/") and path.endswith("/participants/v2"):
            group_id = path.split("/")[-3]
            participant_errors = self.routes.get("group_participants_v2_errors", {})
            if isinstance(participant_errors, dict) and group_id in participant_errors:
                status, payload = participant_errors[group_id]
                return self._json(status, payload)
            participants = self.routes.get("group_participants_v2", {}).get(
                group_id,
                self.routes.get("group_participants", {}).get(group_id, []),
            )
            return self._json(200, participants)
        if path.startswith("/api/default/groups/") and path.endswith("/participants"):
            group_id = path.split("/")[-2]
            participant_errors = self.routes.get("group_participants_errors", {})
            if isinstance(participant_errors, dict) and group_id in participant_errors:
                status, payload = participant_errors[group_id]
                return self._json(status, payload)
            participants = self.routes.get("group_participants", {}).get(group_id, [])
            return self._json(200, participants)
        if path.startswith("/api/default/chats/") and path.endswith("/messages"):
            chat_id = path.split("/")[-2]
            offset = int(params.get("offset", ["0"])[0])
            messages = self.routes["messages_by_chat"].get(chat_id, [])
            return self._json(200, messages[offset:offset + 500])
        return self._json(404, {"error": "not found", "path": path})

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _page(self, payload: object, params: dict[str, list[str]]) -> object:
        if not isinstance(payload, list) or "limit" not in params:
            return payload
        limit = int(params.get("limit", ["100"])[0])
        offset = int(params.get("offset", ["0"])[0])
        return payload[offset:offset + limit]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class WhatsAppPrimitiveTests(unittest.TestCase):
    def test_waha_runtime_accepts_expected_noweb_container(self) -> None:
        args = SimpleNamespace(
            container_name="powerpacks-waha",
            image="devlikeapro/waha:noweb-2026.3.4",
            engine="NOWEB",
            port=3000,
            session_dir=Path("/tmp/waha-sessions"),
        )
        container = {
            "name": "powerpacks-waha",
            "exists": True,
            "running": True,
            "image": "devlikeapro/waha:noweb-2026.3.4",
            "host_port": "3000",
            "session_mount": "/tmp/waha-sessions",
            "api_key_set": True,
            "engine_env": {
                "WAHA_DEFAULT_ENGINE": "NOWEB",
                "WHATSAPP_DEFAULT_ENGINE": "NOWEB",
                "WHATSAPP_RESTART_ALL_SESSIONS": "true",
            },
        }
        self.assertEqual(waha_runtime.runtime_mismatches(container, args), [])
        self.assertTrue(waha_runtime.runtime_check(container, args)["ok"])

    def test_waha_runtime_rejects_stale_chrome_container(self) -> None:
        args = SimpleNamespace(
            container_name="powerpacks-waha",
            image="devlikeapro/waha:noweb-2026.3.4",
            engine="NOWEB",
            port=3000,
            session_dir=Path("/tmp/waha-sessions"),
        )
        container = {
            "name": "powerpacks-waha",
            "exists": True,
            "running": True,
            "image": "devlikeapro/waha:chrome-2026.3.4",
            "host_port": "3001",
            "session_mount": "/tmp/waha-sessions-chrome",
            "api_key_set": False,
            "engine_env": {
                "WAHA_DEFAULT_ENGINE": "WEBJS",
                "WHATSAPP_DEFAULT_ENGINE": "WEBJS",
                "WHATSAPP_RESTART_ALL_SESSIONS": None,
            },
        }
        fields = {item["field"] for item in waha_runtime.runtime_mismatches(container, args)}
        self.assertIn("image", fields)
        self.assertIn("host_port", fields)
        self.assertIn("session_dir", fields)
        self.assertIn("WAHA_DEFAULT_ENGINE", fields)
        self.assertIn("WHATSAPP_DEFAULT_ENGINE", fields)
        self.assertIn("WAHA_API_KEY", fields)
        self.assertFalse(waha_runtime.runtime_check(container, args)["ok"])

    def test_waha_runtime_check_returns_valid_json(self) -> None:
        result = subprocess.run(
            ["python3", str(WAHA_RUNTIME), "check"],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        # exit code 0 if docker is healthy, 1 if not — both fine.
        self.assertIn(result.returncode, (0, 1))
        manifest = json.loads(result.stdout)
        self.assertEqual(manifest["primitive"], "waha_runtime")
        self.assertIn("docker", manifest)
        self.assertIn("runtime", manifest)
        self.assertIn("alternatives", manifest["docker"])
        self.assertGreater(len(manifest["docker"]["alternatives"]), 0)

    def test_waha_session_status_handles_unreachable_server(self) -> None:
        port = _free_port()
        result = subprocess.run(
            ["python3", str(WAHA_SESSION), "status",
             "--base-url", f"http://127.0.0.1:{port}",
             "--api-key", "test"],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["primitive"], "waha_session")
        self.assertFalse(payload["state"].get("reachable"))

    def test_waha_session_clears_stale_qr_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            qr_dir = Path(tmp)
            qr_png = qr_dir / "qr.png"
            qr_txt = qr_dir / "qr.txt"
            qr_png.write_bytes(b"old")
            qr_txt.write_text("old", encoding="utf-8")

            removed = waha_session.clear_qr_artifacts(qr_png, qr_txt)

            self.assertEqual(set(removed), {str(qr_png), str(qr_txt)})
            self.assertFalse(qr_png.exists())
            self.assertFalse(qr_txt.exists())

    def test_extract_whatsapp_contacts_against_fake_waha(self) -> None:
        FakeWAHAHandler.request_counts = {}
        FakeWAHAHandler.routes = {
            "contacts": [
                {
                    "id": {"_serialized": "14155550101@c.us"},
                    "name": "Jane Doe",
                    "pushname": "Jane",
                },
                {
                    "id": {"_serialized": "14155550202@c.us"},
                    "name": "Op Bob",
                },
                {
                    "id": {"_serialized": "14155559999@c.us"},
                    "name": "Contacts Only",
                },
            ],
            "chats": [
                {
                    "id": {"_serialized": "14155550101@c.us"},
                    "timestamp": 1735689600,
                    "messagesCount": 42,
                },
                {
                    "id": {"_serialized": "14155550202@c.us"},
                    "timestamp": 1735689700,
                },
                {
                    "id": {"_serialized": "987654321@g.us"},
                    "name": "Founders",
                    "groupMetadata": {
                        "subject": "Founders",
                        "participants": [
                            {"id": {"_serialized": "14155550101@c.us"}},
                        ],
                    },
                },
            ],
            "group_participants_v2": {
                "987654321%40g.us": [
                    {"id": "14155550101@c.us", "pn": "14155550101@c.us", "role": "participant"},
                    {"id": "14155550202@c.us", "pn": "14155550202@c.us", "role": "participant"},
                    {"id": "14155550606@c.us", "pn": "14155550606@c.us", "role": "participant", "name": "Group Charlie"},
                ],
            },
            "group_participants": {
                "987654321%40g.us": [
                    {"id": {"_serialized": "14155550101@c.us"}},
                    {"id": {"_serialized": "14155550202@c.us"}},
                ],
            },
            "messages_by_chat": {
                "14155550202%40c.us": [{"id": i} for i in range(3)],
            },
        }
        port = _free_port()
        server = ThreadingHTTPServer(("127.0.0.1", port), FakeWAHAHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                csv_path = tmp / "wa.csv"
                jsonl_path = tmp / "wa.jsonl"
                manifest_path = tmp / "wa.manifest.json"
                cache_path = tmp / "wa.message-count-cache.json"
                result = subprocess.run(
                    [
                        "python3", str(EXTRACT_WHATSAPP), "extract",
                        "--base-url", f"http://127.0.0.1:{port}",
                        "--api-key", "test",
                        "--session", "default",
                        "--output-csv", str(csv_path),
                        "--output-jsonl", str(jsonl_path),
                        "--manifest", str(manifest_path),
                        "--run-id", "test-run",
                        "--heartbeat-interval", "1",
                        "--message-count-cache", str(cache_path),
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env={
                        **os.environ,
                        "POWERPACKS_WHATSAPP_MIN_REQUEST_INTERVAL": "0",
                        "POWERPACKS_WHATSAPP_LIST_PAGE_SIZE": "2",
                    },
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                manifest = json.loads(result.stdout)
                self.assertEqual(manifest["status"], "completed")
                self.assertEqual(manifest["diagnostics"]["contacts_page_size"], 2)
                self.assertEqual(manifest["diagnostics"]["chats_page_size"], 2)
                self.assertEqual(FakeWAHAHandler.request_counts.get("/api/contacts/all"), 2)
                self.assertEqual(FakeWAHAHandler.request_counts.get("/api/default/chats"), 2)
                self.assertGreaterEqual(manifest["counts"]["contacts"], 2)
                self.assertEqual(manifest["diagnostics"]["message_count_total"], 1)
                progress_path = Path(manifest["artifacts"]["progress_jsonl"])
                self.assertTrue(progress_path.exists())
                self.assertIn("message_counts", result.stderr)

                with csv_path.open(encoding="utf-8") as handle:
                    rows = list(CsvIO.dict_reader(handle))
                phones = {row["phone"] for row in rows}
                self.assertIn("+14155550101", phones)
                self.assertIn("+14155550202", phones)
                self.assertNotIn("+14155559999", phones)

                # Jane: in group + has hinted message_count from chat payload.
                jane = next(row for row in rows if row["phone"] == "+14155550101")
                self.assertEqual(jane["name"], "Jane Doe")
                self.assertEqual(jane["source"], "whatsapp")
                self.assertEqual(jane["is_in_group_chats"], "true")
                self.assertEqual(jane["group_names"], "Founders")
                self.assertEqual(jane["message_count"], "42")

                bob = next(row for row in rows if row["phone"] == "+14155550202")
                self.assertEqual(bob["is_in_group_chats"], "true")
                self.assertEqual(bob["group_names"], "Founders")
                self.assertEqual(bob["message_count"], "3")

                charlie = next(row for row in rows if row["phone"] == "+14155550606")
                self.assertEqual(charlie["name"], "Group Charlie")
                self.assertEqual(charlie["is_in_group_chats"], "true")
                self.assertEqual(charlie["group_names"], "Founders")
                self.assertEqual(
                    FakeWAHAHandler.request_counts.get("/api/default/groups/987654321%40g.us/participants/v2"),
                    1,
                )
                self.assertIsNone(
                    FakeWAHAHandler.request_counts.get("/api/default/groups/987654321%40g.us/participants"),
                )

                # JSONL records carry the same shape used by normalize_message_contacts.
                jsonl_rows = [json.loads(line) for line in jsonl_path.read_text().splitlines()]
                self.assertEqual(len(jsonl_rows), len(rows))
                self.assertEqual(jsonl_rows[0]["sources"], ["whatsapp"])
        finally:
            server.shutdown()
            server.server_close()

    def test_contacts_pagination_must_be_honored(self) -> None:
        FakeWAHAHandler.request_counts = {}
        FakeWAHAHandler.routes = {
            "ignore_contacts_pagination": True,
            "contacts": [
                {"id": {"_serialized": "14155550101@c.us"}, "name": "A"},
                {"id": {"_serialized": "14155550202@c.us"}, "name": "B"},
                {"id": {"_serialized": "14155550303@c.us"}, "name": "C"},
            ],
            "chats": [],
            "messages_by_chat": {},
        }
        port = _free_port()
        server = ThreadingHTTPServer(("127.0.0.1", port), FakeWAHAHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                result = subprocess.run(
                    [
                        "python3", str(EXTRACT_WHATSAPP), "extract",
                        "--base-url", f"http://127.0.0.1:{port}",
                        "--api-key", "test",
                        "--session", "default",
                        "--output-csv", str(tmp / "wa.csv"),
                        "--manifest", str(tmp / "wa.manifest.json"),
                        "--skip-message-counts",
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env={
                        **os.environ,
                        "POWERPACKS_WHATSAPP_MIN_REQUEST_INTERVAL": "0",
                        "POWERPACKS_WHATSAPP_LIST_PAGE_SIZE": "2",
                    },
                )
                self.assertEqual(result.returncode, 1)
                manifest = json.loads(result.stdout)
                self.assertEqual(manifest["status"], "failed")
                self.assertIn("pagination failed", manifest["error"])
        finally:
            server.shutdown()
            server.server_close()

    def test_message_count_fetch_is_single_page_serial(self) -> None:
        FakeWAHAHandler.request_counts = {}
        FakeWAHAHandler.routes = {
            "contacts": [
                {"id": {"_serialized": "14155550303@c.us"}, "name": "Long Chat"},
            ],
            "chats": [
                {"id": {"_serialized": "14155550303@c.us"}, "timestamp": 1735689800},
            ],
            "messages_by_chat": {
                "14155550303%40c.us": [{"id": i} for i in range(750)],
            },
        }
        port = _free_port()
        server = ThreadingHTTPServer(("127.0.0.1", port), FakeWAHAHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                csv_path = tmp / "wa.csv"
                manifest_path = tmp / "wa.manifest.json"
                cache_path = tmp / "wa.message-count-cache.json"
                result = subprocess.run(
                    [
                        "python3", str(EXTRACT_WHATSAPP), "extract",
                        "--base-url", f"http://127.0.0.1:{port}",
                        "--api-key", "test",
                        "--session", "default",
                        "--output-csv", str(csv_path),
                        "--manifest", str(manifest_path),
                        "--message-count-cache", str(cache_path),
                        "--run-id", "test-run",
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env={**os.environ, "POWERPACKS_WHATSAPP_MIN_REQUEST_INTERVAL": "0"},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                manifest = json.loads(result.stdout)
                self.assertEqual(manifest["diagnostics"]["message_count_mode"], "single_page_serial")
                self.assertEqual(manifest["diagnostics"]["message_count_cap"], 500)
                self.assertEqual(manifest["diagnostics"]["message_count_completed"], 1)
                self.assertEqual(
                    FakeWAHAHandler.request_counts.get("/api/default/chats/14155550303%40c.us/messages"),
                    1,
                )
                with csv_path.open(encoding="utf-8") as handle:
                    rows = list(CsvIO.dict_reader(handle))
                self.assertEqual(rows[0]["message_count"], "500")
        finally:
            server.shutdown()
            server.server_close()

    def test_large_group_participants_are_skipped_by_default(self) -> None:
        FakeWAHAHandler.request_counts = {}
        FakeWAHAHandler.routes = {
            "contacts": [
                {"id": {"_serialized": "14155550707@c.us"}, "name": "Large Group Contact"},
            ],
            "chats": [
                {
                    "id": {"_serialized": "987654321@g.us"},
                    "name": "Huge Group",
                    "groupMetadata": {"subject": "Huge Group", "size": 31},
                },
            ],
            "group_participants_v2": {
                "987654321%40g.us": [
                    {"id": "14155550707@c.us", "pn": "14155550707@c.us", "role": "participant"},
                ],
            },
            "messages_by_chat": {},
        }
        port = _free_port()
        server = ThreadingHTTPServer(("127.0.0.1", port), FakeWAHAHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                csv_path = tmp / "wa.csv"
                manifest_path = tmp / "wa.manifest.json"
                result = subprocess.run(
                    [
                        "python3", str(EXTRACT_WHATSAPP), "extract",
                        "--base-url", f"http://127.0.0.1:{port}",
                        "--api-key", "test",
                        "--session", "default",
                        "--output-csv", str(csv_path),
                        "--manifest", str(manifest_path),
                        "--skip-message-counts",
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env={**os.environ, "POWERPACKS_WHATSAPP_MIN_REQUEST_INTERVAL": "0"},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                manifest = json.loads(result.stdout)
                self.assertEqual(manifest["diagnostics"]["max_group_participants"], 30)
                self.assertEqual(manifest["diagnostics"]["group_participants_skipped_large"], 1)
                self.assertEqual(manifest["diagnostics"]["group_participants_skipped_large_members"], 31)
                self.assertIsNone(
                    FakeWAHAHandler.request_counts.get("/api/default/groups/987654321%40g.us/participants/v2"),
                )
                with csv_path.open(encoding="utf-8") as handle:
                    rows = list(CsvIO.dict_reader(handle))
                self.assertEqual(rows, [])
        finally:
            server.shutdown()
            server.server_close()

    def test_stale_group_is_skipped_without_retry_or_participant_fetch(self) -> None:
        FakeWAHAHandler.request_counts = {}
        FakeWAHAHandler.routes = {
            "contacts": [
                {"id": {"_serialized": "14155550707@c.us"}, "name": "Poker Friend"},
            ],
            "chats": [
                {
                    "id": {"_serialized": "15127589244-1589065328@g.us"},
                    "name": "Poker",
                },
            ],
            "group_metadata_errors": {
                "15127589244-1589065328%40g.us": (
                    500,
                    {
                        "statusCode": 500,
                        "exception": {
                            "message": "Group with id '15127589244-1589065328@g.us' not found",
                        },
                    },
                ),
            },
            "group_participants_v2": {
                "15127589244-1589065328%40g.us": [
                    {"id": "14155550707@c.us", "pn": "14155550707@c.us", "role": "participant"},
                ],
            },
            "messages_by_chat": {},
        }
        port = _free_port()
        server = ThreadingHTTPServer(("127.0.0.1", port), FakeWAHAHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                csv_path = tmp / "wa.csv"
                manifest_path = tmp / "wa.manifest.json"
                result = subprocess.run(
                    [
                        "python3", str(EXTRACT_WHATSAPP), "extract",
                        "--base-url", f"http://127.0.0.1:{port}",
                        "--api-key", "test",
                        "--session", "default",
                        "--output-csv", str(csv_path),
                        "--manifest", str(manifest_path),
                        "--skip-message-counts",
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env={
                        **os.environ,
                        "POWERPACKS_WHATSAPP_MIN_REQUEST_INTERVAL": "0",
                        "POWERPACKS_WHATSAPP_GROUP_PARTICIPANTS_TIMEOUT": "5",
                    },
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                manifest = json.loads(result.stdout)
                self.assertEqual(manifest["diagnostics"]["group_participants_skipped_stale"], 1)
                self.assertEqual(manifest["diagnostics"]["group_participants_failed"], 0)
                self.assertEqual(manifest["diagnostics"]["errors"], [])
                self.assertEqual(
                    FakeWAHAHandler.request_counts.get("/api/default/groups/15127589244-1589065328%40g.us"),
                    1,
                )
                self.assertIsNone(
                    FakeWAHAHandler.request_counts.get(
                        "/api/default/groups/15127589244-1589065328%40g.us/participants/v2"
                    ),
                )
                with csv_path.open(encoding="utf-8") as handle:
                    rows = list(CsvIO.dict_reader(handle))
                self.assertEqual(rows, [])
        finally:
            server.shutdown()
            server.server_close()

    def test_group_participant_fetch_failure_is_recorded_and_falls_back(self) -> None:
        FakeWAHAHandler.request_counts = {}
        FakeWAHAHandler.routes = {
            "contacts": [
                {"id": {"_serialized": "14155550101@c.us"}, "name": "Jane Doe"},
            ],
            "chats": [
                {
                    "id": {"_serialized": "987654321@g.us"},
                    "name": "Founders",
                    "groupMetadata": {
                        "subject": "Founders",
                        "participants": [
                            {"id": {"_serialized": "14155550101@c.us"}},
                        ],
                    },
                },
            ],
            "group_participants_v2_errors": {
                "987654321%40g.us": (404, {"error": "not found"}),
            },
            "group_participants_errors": {
                "987654321%40g.us": (404, {"error": "not found"}),
            },
            "messages_by_chat": {},
        }
        port = _free_port()
        server = ThreadingHTTPServer(("127.0.0.1", port), FakeWAHAHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                csv_path = tmp / "wa.csv"
                manifest_path = tmp / "wa.manifest.json"
                result = subprocess.run(
                    [
                        "python3", str(EXTRACT_WHATSAPP), "extract",
                        "--base-url", f"http://127.0.0.1:{port}",
                        "--api-key", "test",
                        "--session", "default",
                        "--output-csv", str(csv_path),
                        "--manifest", str(manifest_path),
                        "--skip-message-counts",
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env={
                        **os.environ,
                        "POWERPACKS_WHATSAPP_MIN_REQUEST_INTERVAL": "0",
                        "POWERPACKS_WHATSAPP_GROUP_PARTICIPANTS_TIMEOUT": "5",
                    },
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                manifest = json.loads(result.stdout)
                self.assertEqual(manifest["diagnostics"]["group_participants_failed"], 1)
                self.assertEqual(manifest["diagnostics"]["group_participants_fallback"], 1)
                self.assertEqual(manifest["diagnostics"]["errors"][0]["step"], "group_participants")
                with csv_path.open(encoding="utf-8") as handle:
                    rows = list(CsvIO.dict_reader(handle))
                self.assertEqual(rows[0]["phone"], "+14155550101")
                self.assertEqual(rows[0]["is_in_group_chats"], "true")
        finally:
            server.shutdown()
            server.server_close()

    def test_message_count_cache_skips_unchanged_live_chat_recount(self) -> None:
        FakeWAHAHandler.request_counts = {}
        FakeWAHAHandler.routes = {
            "contacts": [
                {"id": "14155550404@s.whatsapp.net", "name": "Cached Chat"},
            ],
            "chats": [
                {"id": "14155550404@s.whatsapp.net", "conversationTimestamp": 1735689900},
            ],
            "messages_by_chat": {
                "14155550404%40s.whatsapp.net": [{"id": i} for i in range(7)],
            },
        }
        port = _free_port()
        server = ThreadingHTTPServer(("127.0.0.1", port), FakeWAHAHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                cache_path = tmp / "message-count-cache.json"
                env = {**os.environ, "POWERPACKS_WHATSAPP_MIN_REQUEST_INTERVAL": "0"}
                base_cmd = [
                    "python3", str(EXTRACT_WHATSAPP), "extract",
                    "--base-url", f"http://127.0.0.1:{port}",
                    "--api-key", "test",
                    "--session", "default",
                    "--message-count-cache", str(cache_path),
                ]
                first = subprocess.run(
                    base_cmd
                    + ["--output-csv", str(tmp / "first.csv"), "--manifest", str(tmp / "first.manifest.json")],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=env,
                )
                self.assertEqual(first.returncode, 0, first.stderr)
                first_manifest = json.loads(first.stdout)
                self.assertEqual(first_manifest["diagnostics"]["message_count_completed"], 1)
                self.assertEqual(first_manifest["diagnostics"]["message_count_cached"], 0)
                self.assertEqual(
                    FakeWAHAHandler.request_counts.get("/api/default/chats/14155550404%40s.whatsapp.net/messages"),
                    1,
                )

                FakeWAHAHandler.request_counts = {}
                second = subprocess.run(
                    base_cmd
                    + ["--output-csv", str(tmp / "second.csv"), "--manifest", str(tmp / "second.manifest.json")],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=env,
                )
                self.assertEqual(second.returncode, 0, second.stderr)
                second_manifest = json.loads(second.stdout)
                self.assertEqual(second_manifest["diagnostics"]["message_count_cached"], 1)
                self.assertEqual(second_manifest["diagnostics"]["message_count_total"], 0)
                self.assertIsNone(FakeWAHAHandler.request_counts.get("/api/default/chats/14155550404%40s.whatsapp.net/messages"))
                with (tmp / "second.csv").open(encoding="utf-8") as handle:
                    rows = list(CsvIO.dict_reader(handle))
                self.assertEqual(rows[0]["message_count"], "7")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
