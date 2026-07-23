import csv
import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import urllib.request
import unittest
from unittest import mock
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from packs.shared.csv_io import CsvIO


ROOT = Path(__file__).resolve().parents[1]
NORMALIZE = ROOT / "packs/ingestion/primitives/discover_contacts_pipeline/messages/normalize_contacts.py"
IMESSAGE = ROOT / "packs/ingestion/primitives/discover_contacts_pipeline/messages/extract_imessage.py"


class MessagesPackTests(unittest.TestCase):
    def test_message_ingestion_json_contracts_parse(self) -> None:
        for path in (ROOT / "packs/ingestion/schemas").rglob("*.json"):
            with self.subTest(path=path):
                json.loads(path.read_text())

    def test_normalize_contact_exporter_csv(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            input_csv = tmp / "contacts.csv"
            output_jsonl = tmp / "contacts.jsonl"
            manifest = tmp / "manifest.json"
            with input_csv.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "phone",
                        "name",
                        "source",
                        "is_in_group_chats",
                        "group_names",
                        "message_count",
                        "last_message",
                        "skip",
                        "match_status",
                        "matched_person_id",
                        "matched_name",
                        "matched_linkedin_url",
                        "match_confidence",
                        "match_method",
                        "match_reason",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "phone": "(415) 555-0101",
                        "name": "Jane Doe",
                        "source": "imessage",
                        "is_in_group_chats": "true",
                        "group_names": "Founders | Board",
                        "message_count": "12",
                        "last_message": "2026-04-01T00:00:00+00:00",
                        "skip": "",
                        "match_status": "matched",
                        "matched_person_id": "person-1",
                        "matched_name": "Jane Doe",
                        "matched_linkedin_url": "https://linkedin.com/in/jane",
                        "match_confidence": "0.97",
                        "match_method": "name_exact",
                        "match_reason": "Unique exact match",
                    }
                )
                writer.writerow(
                    {
                        "phone": "+14155550101",
                        "name": "",
                        "source": "whatsapp",
                        "is_in_group_chats": "",
                        "group_names": "Operators",
                        "message_count": "20",
                        "last_message": "2026-04-02T00:00:00+00:00",
                        "skip": "yes",
                        "match_status": "",
                        "matched_person_id": "",
                        "matched_name": "",
                        "matched_linkedin_url": "",
                        "match_confidence": "",
                        "match_method": "",
                        "match_reason": "",
                    }
                )

            result = subprocess.run(
                [
                    "python3",
                    str(NORMALIZE),
                    "normalize",
                    "--input",
                    str(input_csv),
                    "--out-jsonl",
                    str(output_jsonl),
                    "--manifest",
                    str(manifest),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            )
            summary = json.loads(result.stdout)
            self.assertEqual(summary["counts"]["input_rows"], 2)
            self.assertEqual(summary["counts"]["normalized_rows"], 1)
            rows = [json.loads(line) for line in output_jsonl.read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["phone"], "+14155550101")
            self.assertEqual(rows[0]["sources"], ["imessage", "whatsapp"])
            self.assertEqual(rows[0]["message_count"], 32)
            self.assertEqual(rows[0]["imessage_message_count"], 12)
            self.assertEqual(rows[0]["whatsapp_message_count"], 20)
            self.assertEqual(rows[0]["last_message"], "2026-04-02T00:00:00+00:00")
            self.assertEqual(rows[0]["imessage_last_message"], "2026-04-01T00:00:00+00:00")
            self.assertEqual(rows[0]["whatsapp_last_message"], "2026-04-02T00:00:00+00:00")
            self.assertTrue(rows[0]["skip"])
            self.assertIn("Operators", rows[0]["group_names"])

    def test_extract_imessage_from_sqlite_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            chat_db = tmp / "chat.db"
            addressbook_dir = tmp / "AddressBook" / "Sources" / "fixture"
            addressbook_dir.mkdir(parents=True)
            addressbook_db = addressbook_dir / "AddressBook-v22.abcddb"

            with sqlite3.connect(chat_db) as conn:
                conn.executescript(
                    """
                    CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
                    CREATE TABLE message (
                      ROWID INTEGER PRIMARY KEY,
                      handle_id INTEGER,
                      date INTEGER,
                      associated_message_type INTEGER,
                      text TEXT,
                      attributedBody BLOB
                    );
                    CREATE TABLE chat (
                      ROWID INTEGER PRIMARY KEY,
                      chat_identifier TEXT,
                      display_name TEXT,
                      room_name TEXT
                    );
                    CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
                    INSERT INTO handle (ROWID, id) VALUES (1, '+14155550101'), (2, 'not-an-email@example.com');
                    INSERT INTO message (handle_id, date, associated_message_type, text, attributedBody)
                      VALUES (1, 725846400000000000, NULL, 'SECRET MESSAGE BODY', X'534543524554'),
                             (1, 725846500000000000, NULL, 'SECRET MESSAGE BODY', X'534543524554'),
                             (2, 725846600000000000, NULL, 'SECRET MESSAGE BODY', X'534543524554');
                    INSERT INTO chat (ROWID, chat_identifier, display_name, room_name)
                      VALUES (1, 'chat123', 'Founders', NULL);
                    INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (1, 1);
                    """
                )

            with sqlite3.connect(addressbook_db) as conn:
                conn.executescript(
                    """
                    CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, ZFIRSTNAME TEXT, ZLASTNAME TEXT);
                    CREATE TABLE ZABCDPHONENUMBER (ZOWNER INTEGER, ZFULLNUMBER TEXT);
                    INSERT INTO ZABCDRECORD (Z_PK, ZFIRSTNAME, ZLASTNAME) VALUES (1, 'Jane', 'Doe'), (2, 'Grace', 'Hopper');
                    INSERT INTO ZABCDPHONENUMBER (ZOWNER, ZFULLNUMBER) VALUES (1, '(415) 555-0101'), (2, '(415) 555-0102');
                    """
                )

            check_result = subprocess.run(
                [
                    "python3",
                    str(IMESSAGE),
                    "--chat-db",
                    str(chat_db),
                    "--addressbook-glob",
                    str(tmp / "AddressBook/Sources/*/AddressBook-v22.abcddb"),
                    "check",
                    "--strict",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            )
            check_payload = json.loads(check_result.stdout)
            self.assertTrue(check_payload["addressbook"]["readable"])
            self.assertEqual(check_payload["addressbook"]["contacts"], 2)
            self.assertEqual(check_payload["addressbook"]["readable_databases"], 1)

            output_csv = tmp / "out.csv"
            output_jsonl = tmp / "out.jsonl"
            manifest = tmp / "manifest.json"
            result = subprocess.run(
                [
                    "python3",
                    str(IMESSAGE),
                    "--chat-db",
                    str(chat_db),
                    "--addressbook-glob",
                    str(tmp / "AddressBook/Sources/*/AddressBook-v22.abcddb"),
                    "extract",
                    "--output-csv",
                    str(output_csv),
                    "--output-jsonl",
                    str(output_jsonl),
                    "--manifest",
                    str(manifest),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            )
            summary = json.loads(result.stdout)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["counts"]["contacts"], 2)
            self.assertEqual(summary["counts"]["contact_only"], 1)
            rows = [json.loads(line) for line in output_jsonl.read_text().splitlines()]
            rows_by_phone = {row["phone"]: row for row in rows}
            self.assertEqual(rows_by_phone["+14155550101"]["name"], "Jane Doe")
            self.assertEqual(rows_by_phone["+14155550101"]["message_count"], 2)
            self.assertTrue(rows_by_phone["+14155550101"]["is_in_group_chats"])
            self.assertEqual(rows_by_phone["+14155550101"]["group_names"], ["Founders"])
            self.assertEqual(rows_by_phone["+14155550102"]["name"], "Grace Hopper")
            self.assertIsNone(rows_by_phone["+14155550102"]["message_count"])
            emitted = "\n".join(
                [result.stdout, output_csv.read_text(), output_jsonl.read_text(), manifest.read_text()]
            )
            self.assertNotIn("SECRET MESSAGE BODY", emitted)

    def test_extract_imessage_has_deterministic_order(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            chat_db = tmp / "chat.db"
            addressbook_dir = tmp / "AddressBook" / "Sources" / "fixture"
            addressbook_dir.mkdir(parents=True)
            addressbook_db = addressbook_dir / "AddressBook-v22.abcddb"

            with sqlite3.connect(chat_db) as conn:
                conn.executescript(
                    """
                    CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
                    CREATE TABLE message (
                      ROWID INTEGER PRIMARY KEY,
                      handle_id INTEGER,
                      date INTEGER,
                      associated_message_type INTEGER
                    );
                    INSERT INTO handle (ROWID, id)
                      VALUES (3, '+14155550103'),
                             (1, '+14155550101'),
                             (2, '+14155550102');
                    INSERT INTO message (handle_id, date, associated_message_type)
                      VALUES (3, 725846500000000000, NULL),
                             (3, 725846400000000000, NULL),
                             (1, 725846500000000000, NULL),
                             (1, 725846400000000000, NULL),
                             (2, 725846600000000000, NULL);
                    """
                )

            with sqlite3.connect(addressbook_db) as conn:
                conn.executescript(
                    """
                    CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, ZFIRSTNAME TEXT, ZLASTNAME TEXT);
                    CREATE TABLE ZABCDPHONENUMBER (ZOWNER INTEGER, ZFULLNUMBER TEXT);
                    INSERT INTO ZABCDRECORD (Z_PK, ZFIRSTNAME, ZLASTNAME)
                      VALUES (1, 'One', 'Person'),
                             (2, 'Two', 'Person'),
                             (3, 'Three', 'Person'),
                             (4, 'Contact', 'Only');
                    INSERT INTO ZABCDPHONENUMBER (ZOWNER, ZFULLNUMBER)
                      VALUES (1, '(415) 555-0101'),
                             (2, '(415) 555-0102'),
                             (3, '(415) 555-0103'),
                             (4, '(415) 555-0104');
                    """
                )

            output_csv = tmp / "out.csv"
            subprocess.run(
                [
                    "python3",
                    str(IMESSAGE),
                    "--chat-db",
                    str(chat_db),
                    "--addressbook-glob",
                    str(tmp / "AddressBook/Sources/*/AddressBook-v22.abcddb"),
                    "extract",
                    "--output-csv",
                    str(output_csv),
                    "--output-jsonl",
                    str(tmp / "out.jsonl"),
                    "--manifest",
                    str(tmp / "manifest.json"),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            )

            with output_csv.open(newline="", encoding="utf-8") as handle:
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(
                [row["phone"] for row in rows],
                ["+14155550101", "+14155550103", "+14155550102", "+14155550104"],
            )

    def test_extract_imessage_privacy_settings_print_only(self) -> None:
        result = subprocess.run(
            [
                "python3",
                str(IMESSAGE),
                "open-privacy-settings",
                "--target",
                "both",
                "--print-only",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["command"], "open-privacy-settings")
        self.assertEqual(payload["targets"], ["full-disk-access", "contacts"])
        self.assertFalse(payload["opened"])
        self.assertIn("Privacy_AllFiles", payload["urls"][0])
        self.assertIn("Privacy_Contacts", payload["urls"][1])


if __name__ == "__main__":
    unittest.main()


class MergeMessageContactsTests(unittest.TestCase):
    MERGE = ROOT / "packs/ingestion/primitives/discover_contacts_pipeline/messages/merge_contacts.py"
    HEADERS = [
        "phone",
        "name",
        "source",
        "is_in_group_chats",
        "group_names",
        "message_count",
        "last_message",
        "skip",
        "match_status",
        "matched_person_id",
        "matched_name",
        "matched_linkedin_url",
        "match_confidence",
        "match_method",
        "match_reason",
    ]

    def _write(self, path, rows):
        with path.open("w", newline="") as h:
            w = csv.DictWriter(h, fieldnames=self.HEADERS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in self.HEADERS})

    def test_merges_cross_channel_phones(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            imsg = tmp / "imessage.contacts.csv"
            wa = tmp / "whatsapp.contacts.csv"
            out = tmp / "contacts.csv"
            self._write(
                imsg,
                [
                    # Cross-channel: same phone in both inputs.
                    {
                        "phone": "+14155550101",
                        "name": "Jane Doe",
                        "source": "imessage",
                        "is_in_group_chats": "false",
                        "group_names": "",
                        "message_count": "120",
                        "last_message": "2026-01-01T00:00:00+00:00",
                    },
                    # iMessage-only.
                    {
                        "phone": "+14155550999",
                        "name": "Solo iMsg",
                        "source": "imessage",
                        "message_count": "5",
                        "last_message": "2025-12-01T00:00:00+00:00",
                    },
                    # Empty-name iMessage row that should not overwrite WhatsApp's name.
                    {
                        "phone": "+14155550202",
                        "name": "",
                        "source": "imessage",
                        "message_count": "3",
                        "last_message": "2025-11-15T00:00:00+00:00",
                    },
                    # Pre-matched iMessage row should win on confidence over WhatsApp's empty match.
                    {
                        "phone": "+14155550303",
                        "name": "Carol Lopez",
                        "source": "imessage",
                        "message_count": "10",
                        "last_message": "2026-02-01T00:00:00+00:00",
                        "match_status": "matched",
                        "matched_person_id": "p4",
                        "matched_name": "Carol Lopez",
                        "matched_linkedin_url": "https://l/in/carol",
                        "match_confidence": "1",
                        "match_method": "name_exact_linkedin",
                        "match_reason": "unique exact name match",
                    },
                ],
            )
            self._write(
                wa,
                [
                    # Cross-channel: same phone, different message count + group context, with a skip flag.
                    {
                        "phone": "+14155550101",
                        "name": "Jane Doe",
                        "source": "whatsapp",
                        "is_in_group_chats": "true",
                        "group_names": "Founders | Board",
                        "message_count": "200",
                        "last_message": "2026-03-01T00:00:00+00:00",
                        "skip": "yes",
                    },
                    # WhatsApp-only with a populated name where iMessage row was nameless.
                    {
                        "phone": "+14155550202",
                        "name": "Bob Smith",
                        "source": "whatsapp",
                        "message_count": "12",
                        "last_message": "2025-12-25T00:00:00+00:00",
                    },
                    # WhatsApp-only group-only contact (no iMessage row).
                    {
                        "phone": "+14155550404",
                        "name": "Plumber Mike",
                        "source": "whatsapp",
                        "is_in_group_chats": "true",
                        "group_names": "Building",
                        "message_count": "1",
                    },
                    # Same as Carol but with no match data; iMessage's match wins on confidence.
                    {
                        "phone": "+14155550303",
                        "name": "Carol Lopez",
                        "source": "whatsapp",
                        "message_count": "4",
                        "last_message": "2026-02-15T00:00:00+00:00",
                    },
                ],
            )
            result = subprocess.run(
                ["python3", str(self.MERGE), "merge", "-i", str(imsg), "-i", str(wa), "-o", str(out)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            )
            manifest = json.loads(result.stdout)
            self.assertEqual(manifest["counts"]["unique_phones"], 5)
            self.assertEqual(manifest["counts"]["cross_channel_phones"], 3)
            self.assertEqual(manifest["counts"]["by_source"], {"imessage": 4, "whatsapp": 4})
            self.assertEqual(manifest["counts"]["rows_written"], 5)

            with out.open(newline="") as h:
                rows = list(CsvIO.dict_reader(h))
            by_phone = {r["phone"]: r for r in rows}

            jane = by_phone["+14155550101"]
            self.assertEqual(jane["source"], "imessage,whatsapp")
            # total across channels: 120 + 200 = 320
            self.assertEqual(jane["message_count"], "320")
            self.assertEqual(jane["imessage_message_count"], "120")
            self.assertEqual(jane["whatsapp_message_count"], "200")
            # OR'd skip
            self.assertEqual(jane["skip"], "yes")
            # OR'd group flag, sorted union of group_names
            self.assertEqual(jane["is_in_group_chats"], "true")
            self.assertEqual(jane["group_names"], "Board | Founders")
            # max(last_message), with per-source last-message columns preserved.
            self.assertEqual(jane["last_message"], "2026-03-01T00:00:00+00:00")
            self.assertEqual(jane["imessage_last_message"], "2026-01-01T00:00:00+00:00")
            self.assertEqual(jane["whatsapp_last_message"], "2026-03-01T00:00:00+00:00")

            bob = by_phone["+14155550202"]
            # Empty-name iMessage row should not overwrite WhatsApp's name; the merge
            # picks first non-empty name, and since iMessage came first with empty
            # name, the WhatsApp name "Bob Smith" wins.
            self.assertEqual(bob["name"], "Bob Smith")
            self.assertEqual(bob["source"], "imessage,whatsapp")

            carol = by_phone["+14155550303"]
            self.assertEqual(carol["match_status"], "matched")
            self.assertEqual(carol["matched_person_id"], "p4")
            self.assertEqual(carol["match_confidence"], "1")

            self.assertIn("+14155550404", by_phone)
            self.assertIn("+14155550999", by_phone)

            # CSV is sorted by (message_count desc, last_message desc, phone).
            self.assertEqual(rows[0]["phone"], "+14155550101")  # 320

    def test_merge_rejects_legacy_queue_schema_with_conversion_hint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            legacy = tmp / "legacy.csv"
            out = tmp / "contacts.csv"
            legacy.write_text("handle,display_name,phone_e164,total_messages\nphone-1,Ada Lovelace,+15550000001,5\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(self.MERGE), "merge", "--input", str(legacy), "--output", str(out)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            combined = result.stdout + result.stderr
            self.assertIn("Please convert this file", combined)
            self.assertIn("packs/ingestion/schemas/contacts-csv.md", combined)
            self.assertIn("display_name/full_name -> name", combined)


class DeepResearchContactsTests(unittest.TestCase):
    DR = ROOT / "packs/ingestion/primitives/deep_context/deep_research_contacts.py"

    def _fake_parallel_server(self, port: int):
        """Spin up an in-process server that mimics the Parallel.ai task-group API."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading as _t

        state = {
            "groups": {},  # group_id -> {is_active, runs}
            "runs": {},  # run_id -> {group, input_dict, metadata, status}
            "next_group": 0,
            "next_run": 0,
        }

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a, **k):  # noqa: A002
                return

            def _json(self, status, body):
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _read_json(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

            def do_POST(self):
                if self.path == "/v1beta/tasks/groups":
                    state["next_group"] += 1
                    gid = f"tgrp_{state['next_group']:04d}"
                    state["groups"][gid] = {"is_active": True, "runs": []}
                    return self._json(
                        200,
                        {
                            "taskgroup_id": gid,
                            "status": {
                                "is_active": True,
                                "num_task_runs": 0,
                                "task_run_status_counts": {},
                                "status_message": None,
                            },
                        },
                    )
                if self.path.startswith("/v1beta/tasks/groups/") and self.path.endswith("/runs"):
                    gid = self.path.split("/")[4]
                    if gid not in state["groups"]:
                        return self._json(404, {"error": "no such group"})
                    body = self._read_json()
                    inputs = body.get("inputs", [])
                    rids = []
                    for inp in inputs:
                        state["next_run"] += 1
                        rid = f"trun_{state['next_run']:04d}"
                        state["runs"][rid] = {
                            "group": gid,
                            "input": inp.get("input"),
                            "metadata": inp.get("metadata", {}),
                            "status": "completed",
                        }
                        state["groups"][gid]["runs"].append(rid)
                        rids.append(rid)
                    # Mark group complete after first add_runs call.
                    state["groups"][gid]["is_active"] = False
                    return self._json(
                        200,
                        {
                            "run_ids": rids,
                            "status": {
                                "is_active": False,
                                "num_task_runs": len(rids),
                                "task_run_status_counts": {"completed": len(rids)},
                                "status_message": None,
                            },
                        },
                    )
                return self._json(404, {"error": "not found", "path": self.path})

            def do_GET(self):
                # /v1beta/tasks/groups/{id}
                if self.path.startswith("/v1beta/tasks/groups/") and "/runs" not in self.path:
                    gid = self.path.split("/")[4]
                    if gid not in state["groups"]:
                        return self._json(404, {"error": "no such group"})
                    g = state["groups"][gid]
                    return self._json(
                        200,
                        {
                            "taskgroup_id": gid,
                            "status": {
                                "is_active": g["is_active"],
                                "num_task_runs": len(g["runs"]),
                                "task_run_status_counts": {"completed": len(g["runs"])},
                                "status_message": None,
                            },
                        },
                    )
                # /v1/tasks/runs/{id}/result
                if self.path.startswith("/v1/tasks/runs/"):
                    rid = self.path.split("/")[4]
                    if rid not in state["runs"]:
                        return self._json(404, {"error": "no such run"})
                    run = state["runs"][rid]
                    handle = run["metadata"].get("handle") or rid
                    name = run["input"].get("display_name", handle) if isinstance(run["input"], dict) else handle
                    return self._json(
                        200,
                        {
                            "run": {"run_id": rid, "metadata": run["metadata"], "status": run["status"]},
                            "input": {"input": run["input"]},
                            "output": {
                                "content": {
                                    "real_name": name,
                                    "name_confidence": 0.9,
                                    "name_evidence": "fake search",
                                    "work_experience": json.dumps(
                                        [
                                            {
                                                "title": "Engineer",
                                                "company": "FakeCorp",
                                                "is_current": True,
                                                "confidence": 0.8,
                                            }
                                        ]
                                    ),
                                    "education": json.dumps(
                                        [
                                            {
                                                "school": "Fake University",
                                                "degree": "BS",
                                                "field": "CS",
                                                "end_year": 2020,
                                                "confidence": 0.7,
                                            }
                                        ]
                                    ),
                                    "location_city": "San Francisco",
                                    "location_country": "United States",
                                    "linkedin_url": f"https://www.linkedin.com/in/{handle}",
                                    "github_url": None,
                                    "summary": "A fake summary for the test.",
                                    "research_notes": "Test notes.",
                                }
                            },
                        },
                    )
                return self._json(404, {"error": "not found", "path": self.path})

        server = ThreadingHTTPServer(("127.0.0.1", port), H)
        thread = _t.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, state

    def _free_port(self) -> int:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def test_run_against_fake_parallel_writes_per_handle_artifacts(self) -> None:
        port = self._free_port()
        server, state = self._fake_parallel_server(port)
        try:
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                queue_csv = tmp / "queue.csv"
                # Build a research_queue.csv shape-compatible row.
                rq_headers = [
                    "handle",
                    "display_name",
                    "first_name",
                    "last_name",
                    "primary_email",
                    "domain",
                    "total_messages",
                    "bio",
                    "all_emails",
                    "operator_id",
                    "gmail_token_id",
                    "source_channel",
                    "follower_count",
                    "following_count",
                    "verified",
                    "location",
                    "website_url",
                    "twitter_user_id",
                    "first_seen_at",
                    "profile_image_url",
                    "person_id",
                    "moe_verdict",
                    "moe_composite",
                    "moe_confidence",
                    "moe_top_expert",
                    "moe_top_signal",
                    "moe_top_reasoning",
                    "whale_names",
                    "operator",
                    "phone_e164",
                    "phone_last4",
                    "area_code",
                    "message_source",
                    "last_message",
                    "is_in_group_chats",
                    "match_status",
                    "group_names",
                    "match_confidence",
                    "match_method",
                    "match_reason",
                ]
                with queue_csv.open("w", newline="") as h:
                    w = csv.DictWriter(h, fieldnames=rq_headers)
                    w.writeheader()
                    for handle, name, phone in [
                        ("phone-4155550101", "Jane Doe", "+14155550101"),
                        ("phone-4155550202", "Bob Smith", "+14155550202"),
                    ]:
                        w.writerow(
                            {k: "" for k in rq_headers}
                            | {
                                "handle": handle,
                                "display_name": name,
                                "first_name": name.split()[0],
                                "last_name": name.split()[1],
                                "phone_e164": phone,
                                "area_code": "415",
                                "source_channel": "phone",
                                "message_source": "imessage,whatsapp",
                                "total_messages": "120",
                                "is_in_group_chats": "true",
                                "group_names": "Founders",
                            }
                        )

                output_dir = tmp / "research"
                env = {
                    **os.environ,
                    "PARALLEL_API_KEY": "test-key",
                    "POWERPACKS_PARALLEL_BASE_URL": f"http://127.0.0.1:{port}",
                }
                result = subprocess.run(
                    [
                        "python3",
                        str(self.DR),
                        "run",
                        "--input",
                        str(queue_csv),
                        "--output-dir",
                        str(output_dir),
                        "--processor",
                        "core2x",
                        "--poll-interval",
                        "1",
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=env,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                # The `run` subcommand prints two JSON objects (submit then poll).
                # Parse them in sequence and grab the final (poll) manifest.
                decoder = json.JSONDecoder()
                stdout = result.stdout.lstrip()
                manifests = []
                while stdout.strip():
                    obj, idx = decoder.raw_decode(stdout)
                    manifests.append(obj)
                    stdout = stdout[idx:].lstrip()
                self.assertEqual(len(manifests), 2)
                manifest = manifests[-1]
                self.assertEqual(manifest["counts"]["run_ids"], 2)
                self.assertEqual(manifest["counts"]["results_fetched"], 2)
                self.assertEqual(manifest["counts"]["real_name_found"], 2)
                self.assertEqual(manifest["counts"]["linkedin_found"], 2)
                submitted_inputs = [run["input"] for run in state["runs"].values()]
                self.assertEqual(len(submitted_inputs), 2)
                for sent in submitted_inputs:
                    self.assertEqual(set(sent), {"handle", "display_name", "bio", "known_info", "phone_number", "area_code"})
                    self.assertEqual(sent["known_info"], "")
                    self.assertNotIn("source_channel", sent)
                    self.assertNotIn("Message count", json.dumps(sent))
                    self.assertNotIn("Last message", json.dumps(sent))
                    self.assertNotIn("Message source", json.dumps(sent))
                    self.assertNotIn("group", json.dumps(sent).lower())
                # Per-handle artifacts written
                for handle in ("phone-4155550101", "phone-4155550202"):
                    raw = output_dir / handle / "00_parallel_raw.json"
                    research = output_dir / handle / "01_research_parallel.json"
                    self.assertTrue(raw.exists(), raw)
                    self.assertTrue(research.exists(), research)
                    payload = json.loads(research.read_text())
                    self.assertEqual(payload["status"], "draft")
                    self.assertEqual(payload["research_method"], "parallel-core2x")
                    self.assertTrue(payload["social"]["linkedin_url"].startswith("https://"))
                    self.assertEqual(payload["metadata"]["source_channel"], "phone")
                    self.assertGreaterEqual(len(payload["positions"]), 1)
                    self.assertGreaterEqual(len(payload["education"]), 1)

                # Idempotency: re-running estimate now reports skipped_already_done.
                est = subprocess.run(
                    [
                        "python3",
                        str(self.DR),
                        "estimate",
                        "--input",
                        str(queue_csv),
                        "--output-dir",
                        str(output_dir),
                        "--processor",
                        "core2x",
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=env,
                    check=True,
                )
                est_payload = json.loads(est.stdout)
                self.assertEqual(est_payload["skipped_already_done"], 2)
                self.assertEqual(est_payload["would_submit"], 0)
                self.assertEqual(est_payload["estimated_latency"]["per_task"], "60s-10min")
                self.assertEqual(est_payload["estimated_latency"]["rough_wall_clock"], "no paid Parallel work")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
