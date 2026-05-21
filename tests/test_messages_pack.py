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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
NORMALIZE = ROOT / "packs/messages/primitives/normalize_message_contacts/normalize_message_contacts.py"
HARNESS = ROOT / "packs/messages/primitives/powerset_contacts_harness/powerset_contacts_harness.py"
IMESSAGE = ROOT / "packs/messages/primitives/extract_imessage_contacts/extract_imessage_contacts.py"
UPLOAD_REVIEW = ROOT / "packs/messages/primitives/upload_research_review/upload_research_review.py"
MIGRATE_REVIEW = ROOT / "packs/messages/primitives/migrate_review_schema/migrate_review_schema.py"


class MessagesPackTests(unittest.TestCase):
    def test_pack_json_contracts_parse(self) -> None:
        for path in (ROOT / "packs/messages").rglob("*.json"):
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
                    "--run-id",
                    "test-run",
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

    def test_harness_dry_run_uses_fake_cli(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            fake = tmp / "contact-exporter"
            fake.write_text("#!/usr/bin/env sh\nprintf 'contact-exporter 0.test\\n'\n", encoding="utf-8")
            fake.chmod(0o755)
            result = subprocess.run(
                [
                    "python3",
                    str(HARNESS),
                    "--contact-exporter",
                    str(fake),
                    "run",
                    "--channel",
                    "imessage",
                    "--output",
                    str(tmp / "contacts.csv"),
                    "--run-root",
                    str(tmp / "runs"),
                    "--run-id",
                    "dry-run",
                    "--dry-run",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            )
            manifest = json.loads(result.stdout)
            self.assertTrue(manifest["dry_run"])
            self.assertEqual(manifest["channel"], "imessage")
            self.assertIn("imessage", manifest["command"])
            self.assertTrue((tmp / "runs/dry-run/manifest.json").exists())

    def test_extract_imessage_contacts_from_sqlite_fixtures(self) -> None:
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
                    CREATE TABLE chat (
                      ROWID INTEGER PRIMARY KEY,
                      chat_identifier TEXT,
                      display_name TEXT,
                      room_name TEXT
                    );
                    CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
                    INSERT INTO handle (ROWID, id) VALUES (1, '+14155550101'), (2, 'not-an-email@example.com');
                    INSERT INTO message (handle_id, date, associated_message_type)
                      VALUES (1, 725846400000000000, NULL),
                             (1, 725846500000000000, NULL),
                             (2, 725846600000000000, NULL);
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
                    "--run-id",
                    "fixture-run",
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

    def test_extract_imessage_contacts_has_deterministic_order(self) -> None:
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
                    "--run-id",
                    "fixture-order-run",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            )

            with output_csv.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(
                [row["phone"] for row in rows],
                ["+14155550101", "+14155550103", "+14155550102", "+14155550104"],
            )

    def test_upload_research_review_summary_applies_approved_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            csv_path = Path(td) / "research_review.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "bucket", "handle", "top_title_company_pairs", "exclude",
                        "in_network", "approved", "upload_decision",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "bucket": "review",
                        "handle": "phone-1",
                        "top_title_company_pairs": "Founder @ Acme",
                        "exclude": "no",
                    }
                )
                writer.writerow(
                    {
                        "bucket": "confident",
                        "handle": "phone-2",
                        "top_title_company_pairs": "CEO @ Example",
                        "exclude": "yes",
                    }
                )
                writer.writerow(
                    {
                        "bucket": "medium",
                        "handle": "phone-3",
                        "top_title_company_pairs": "Engineer @ Widget",
                        "exclude": "",
                    }
                )
                writer.writerow(
                    {
                        "bucket": "medium",
                        "handle": "phone-4",
                        "top_title_company_pairs": "Designer @ Network",
                        "exclude": "",
                        "in_network": "true",
                    }
                )
                writer.writerow(
                    {
                        "bucket": "medium",
                        "handle": "phone-5",
                        "top_title_company_pairs": "PM @ Approved",
                        "exclude": "",
                        "approved": "true",
                    }
                )
                writer.writerow(
                    {
                        "bucket": "medium",
                        "handle": "phone-6",
                        "top_title_company_pairs": "Investor @ Include",
                        "exclude": "",
                        "upload_decision": "include",
                    }
                )

            result = subprocess.run(
                [
                    "python3",
                    str(UPLOAD_REVIEW),
                    "summarize",
                    "--csv",
                    str(csv_path),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            )
            payload = json.loads(result.stdout)
            self.assertEqual(payload["approved_count"], 3)
            self.assertEqual(payload["skipped_unapproved_count"], 3)
            self.assertNotIn("yes_count", payload)
            self.assertNotIn("maybe_count", payload)
            self.assertNotIn("no_count", payload)
            self.assertNotIn("explicit_include_count", payload)
            self.assertNotIn("explicit_exclude_count", payload)
            self.assertNotIn("bucket_default_count", payload)
            self.assertNotIn("row_count", payload)

    def test_upload_research_review_posts_multipart_csv(self) -> None:
        observed: dict[str, object] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                observed["path"] = self.path
                observed["authorization"] = self.headers.get("Authorization")
                observed["content_type"] = self.headers.get("Content-Type")
                observed["body"] = self.rfile.read(length)
                body = json.dumps(
                    {
                        "artifact_id": "artifact-1",
                        "status": "ready",
                        "created_at": "2026-05-05T00:00:00+00:00",
                        "total_count": 1,
                        "approved_count": 1,
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        with tempfile.TemporaryDirectory() as td:
            csv_path = Path(td) / "research_review.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["bucket", "handle", "top_title_company_pairs", "exclude"],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "bucket": "review",
                        "handle": "phone-1",
                        "top_title_company_pairs": "Founder @ Acme",
                        "exclude": "no",
                    }
                )
                writer.writerow(
                    {
                        "bucket": "confident",
                        "handle": "phone-2",
                        "top_title_company_pairs": "CEO @ Example",
                        "exclude": "yes",
                    }
                )

            result = subprocess.run(
                [
                    "python3",
                    str(UPLOAD_REVIEW),
                    "upload",
                    "--csv",
                    str(csv_path),
                    "--api-url",
                    f"http://127.0.0.1:{server.server_port}",
                    "--token",
                    "test-token",
                    "--confirm-upload",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            )

        payload = json.loads(result.stdout)
        self.assertEqual(payload["response"]["artifact_id"], "artifact-1")
        self.assertEqual(payload["approved_count"], 1)
        self.assertEqual(payload["prepared_summary"]["approved_count"], 1)
        self.assertEqual(payload["prepared_summary"]["skipped_unapproved_count"], 1)
        self.assertEqual(observed["path"], "/v2/messages-research/artifacts")
        self.assertEqual(observed["authorization"], "Bearer test-token")
        self.assertIn("multipart/form-data", str(observed["content_type"]))
        body_text = bytes(observed["body"]).decode("utf-8", errors="replace")
        self.assertIn("source_bucket", body_text)
        self.assertIn("approved", body_text)
        self.assertIn("yes,phone-1", body_text)
        self.assertNotIn("upload_decision", body_text)
        self.assertNotIn("phone-2", body_text)

    def test_migrate_review_schema_canonicalizes_buckets_and_adds_network_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            contacts = tmp / "contacts.csv"
            review = tmp / "research_review.csv"
            out = tmp / "final.csv"
            contacts.write_text(
                "phone,name,source,is_in_group_chats,group_names,message_count,imessage_message_count,whatsapp_message_count,last_message,imessage_last_message,whatsapp_last_message,skip,match_status,matched_person_id,matched_name,matched_linkedin_url,match_confidence,match_method,match_reason\n"
                "+14155550101,Ada,imessage,false,,7,7,,2026-01-01,2026-01-01,,,matched,p1,Ada Lovelace,https://www.linkedin.com/in/ada,0.99,name_exact,exact\n"
                "+14155550202,Bob,whatsapp,false,,3,,3,2026-01-02,,2026-01-02,,matched,p2,Bob Smith,https://www.linkedin.com/in/bob,0.98,name_exact,exact\n",
                encoding="utf-8",
            )
            review.write_text(
                "bucket,handle,full_name,phone_e164,total_messages,short_reason,signals,exclude,enrich_decision\n"
                "medium,phone-4155550101,Ada,+14155550101,7,Some signal,founder,,\n"
                "review,phone-4155550999,Carol,+14155550999,1,Needs review,,,\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    "python3",
                    str(MIGRATE_REVIEW),
                    "migrate",
                    "--review-csv", str(review),
                    "--contacts-csv", str(contacts),
                    "--output-csv", str(out),
                    "--no-backup",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            )
            payload = json.loads(result.stdout)
            self.assertEqual(payload["rows_in"], 2)
            self.assertEqual(payload["rows_written"], 3)
            self.assertEqual(payload["in_network_added"], 1)
            self.assertEqual(payload["research_bucket_counts"], {"maybe": 1, "no": 0, "yes": 0})
            self.assertEqual(payload["tab_counts"], {"in_network": 2, "maybe": 1, "no": 0, "yes": 0})

            with out.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            by_phone = {row["phone_e164"]: row for row in rows}
            self.assertEqual(by_phone["+14155550101"]["bucket"], "maybe")
            self.assertEqual(by_phone["+14155550101"]["in_network"], "true")
            self.assertEqual(by_phone["+14155550101"]["network_person_id"], "p1")
            self.assertEqual(by_phone["+14155550202"]["review_source"], "in_network_match")
            self.assertEqual(by_phone["+14155550999"]["bucket"], "maybe")

            upload_result = subprocess.run(
                ["python3", str(UPLOAD_REVIEW), "summarize", "--csv", str(out)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            )
            upload_payload = json.loads(upload_result.stdout)
            self.assertEqual(upload_payload["approved_count"], 0)
            self.assertEqual(upload_payload["skipped_unapproved_count"], 3)

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
    MERGE = ROOT / "packs/messages/primitives/merge_message_contacts/merge_message_contacts.py"
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
                rows = list(csv.DictReader(h))
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
            self.assertIn("packs/messages/schemas/contacts-csv.md", combined)
            self.assertIn("display_name/full_name -> name", combined)


class PrepareResearchQueueTests(unittest.TestCase):
    PREPARE = ROOT / "packs/messages/primitives/prepare_research_queue/prepare_research_queue.py"
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

    def test_filters_research_queue(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            contacts = tmp / "contacts.csv"
            output = tmp / "queue.csv"
            self._write(
                contacts,
                [
                    # Included: cross-channel + high volume
                    {
                        "phone": "+14155550101",
                        "name": "Jane Doe",
                        "source": "imessage,whatsapp",
                        "message_count": "1500",
                        "last_message": "2026-04-01T00:00:00+00:00",
                    },
                    # Included: single channel, high volume
                    {
                        "phone": "+14155550202",
                        "name": "Bob Smith",
                        "source": "imessage",
                        "message_count": "300",
                        "last_message": "2025-01-01T00:00:00+00:00",
                    },
                    # Included despite low message count: default is to research every eligible contact.
                    {
                        "phone": "+14155550303",
                        "name": "Carol Lopez",
                        "source": "whatsapp",
                        "message_count": "2",
                        "last_message": "2020-01-01T00:00:00+00:00",
                    },
                    # Filtered: already matched in Powerset
                    {
                        "phone": "+14155550404",
                        "name": "Dan Smith",
                        "source": "imessage",
                        "message_count": "10",
                        "matched_person_id": "p99",
                        "matched_name": "Dan Smith",
                        "match_status": "matched",
                    },
                    # Filtered: LLM said skip
                    {
                        "phone": "+14155550505",
                        "name": "Plumber Mike",
                        "source": "imessage",
                        "message_count": "5",
                        "skip": "yes",
                    },
                    # Filtered: no name
                    {"phone": "+14155550606", "name": "", "source": "imessage", "message_count": "1"},
                    # Filtered: single-token name (un-LinkedIn-searchable)
                    {
                        "phone": "+14155550707",
                        "name": "Tanner",
                        "source": "imessage,whatsapp",
                        "message_count": "50",
                        "last_message": "2026-03-01T00:00:00+00:00",
                    },
                    # Filtered: blocked last-name token from old phone_prune_config.
                    {
                        "phone": "+14155550808",
                        "name": "Alice Hinge",
                        "source": "imessage",
                        "message_count": "100",
                    },
                    # Filtered: name is just the phone number.
                    {
                        "phone": "+14155550909",
                        "name": "5550909",
                        "source": "imessage",
                        "message_count": "100",
                    },
                ],
            )
            result = subprocess.run(
                ["python3", str(self.PREPARE), "prepare", "-i", str(contacts), "-o", str(output)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            )
            manifest = json.loads(result.stdout)
            self.assertEqual(manifest["counts"]["input_rows"], 9)
            self.assertEqual(manifest["counts"]["eligible_rows"], 3)
            self.assertEqual(manifest["counts"]["filtered_no_name"], 1)
            self.assertEqual(manifest["counts"]["filtered_unsearchable_name"], 1)
            self.assertEqual(manifest["counts"]["filtered_blocked_name_token"], 1)
            self.assertEqual(manifest["counts"]["filtered_name_is_phone"], 1)
            self.assertEqual(manifest["counts"]["filtered_skipped"], 1)
            self.assertEqual(manifest["counts"]["filtered_already_matched"], 1)
            self.assertEqual(manifest["counts"]["filtered_low_messages"], 0)

            with output.open(newline="") as h:
                rows = list(csv.DictReader(h))
            self.assertEqual(len(rows), 3)
            # Sorted by message count desc, then name.
            self.assertEqual(rows[0]["display_name"], "Jane Doe")
            self.assertEqual(rows[0]["first_name"], "Jane")
            self.assertEqual(rows[0]["last_name"], "Doe")
            self.assertEqual(rows[0]["phone_e164"], "+14155550101")
            self.assertEqual(rows[0]["phone_last4"], "0101")
            self.assertEqual(rows[0]["area_code"], "415")
            self.assertEqual(rows[0]["source_channel"], "phone")
            self.assertEqual(rows[0]["message_source"], "imessage,whatsapp")
            self.assertEqual(rows[0]["handle"], "phone-4155550101")

            self.assertEqual(rows[1]["display_name"], "Bob Smith")
            self.assertEqual(rows[2]["display_name"], "Carol Lopez")

    def test_limit_after_sort(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            contacts = tmp / "contacts.csv"
            output = tmp / "queue.csv"
            rows = []
            first_pool = ["Alex", "Brooke", "Casey", "Drew", "Eli"]
            last_pool = ["Jones", "Brown", "Lopez", "Patel", "Cohen"]
            for i in range(20):
                rows.append(
                    {
                        "phone": f"+1415555{i:04d}",
                        "name": f"{first_pool[i % 5]} High {last_pool[i % 5]}",
                        "source": "imessage,whatsapp",
                        "message_count": "200",
                        "last_message": "2026-04-01T00:00:00+00:00",
                    }
                )
            for i in range(5):
                rows.append(
                    {
                        "phone": f"+1415556{i:04d}",
                        "name": f"{first_pool[i]} {last_pool[i]} Low",
                        "source": "imessage",
                        "message_count": "1",
                        "last_message": "2020-01-01T00:00:00+00:00",
                    }
                )
            self._write(contacts, rows)
            result = subprocess.run(
                [
                    "python3",
                    str(self.PREPARE),
                    "prepare",
                    "-i",
                    str(contacts),
                    "-o",
                    str(output),
                    "--limit",
                    "10",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            )
            manifest = json.loads(result.stdout)
            self.assertEqual(manifest["rows_written"], 10)
            with output.open(newline="") as h:
                queue = list(csv.DictReader(h))
            self.assertEqual(len(queue), 10)
            self.assertTrue(all(r["total_messages"] == "200" for r in queue))


class PrepareRetargetQueueTests(unittest.TestCase):
    PREPARE = ROOT / "packs/messages/primitives/prepare_retarget_queue/prepare_retarget_queue.py"

    def test_only_new_feedback_hashes_are_queued(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            review_csv = tmp / "research_review.csv"
            base_queue = tmp / "research_queue.csv"
            output = tmp / "retarget_queue.csv"
            ledger = tmp / "retarget_attempts.json"
            out_dir = tmp / "research_retarget"

            with review_csv.open("w", newline="") as h:
                w = csv.DictWriter(h, fieldnames=["handle", "full_name", "phone_e164", "total_messages", "retarget_hint"])
                w.writeheader()
                w.writerow({"handle": "phone-1", "full_name": "Jane Doe", "phone_e164": "+14155550101", "total_messages": "5", "retarget_hint": "LinkedIn: https://linkedin.test/jane"})
                w.writerow({"handle": "phone-2", "full_name": "Bob Smith", "phone_e164": "+14155550202", "total_messages": "0", "retarget_hint": ""})

            with base_queue.open("w", newline="") as h:
                fields = ["handle", "display_name", "first_name", "last_name", "phone_e164", "total_messages", "source_channel", "retarget_hint"]
                w = csv.DictWriter(h, fieldnames=fields)
                w.writeheader()
                w.writerow({"handle": "phone-1", "display_name": "Jane Doe", "first_name": "Jane", "last_name": "Doe", "phone_e164": "+14155550101", "total_messages": "5", "source_channel": "phone"})

            cmd = [
                "python3", str(self.PREPARE), "prepare",
                "--review-csv", str(review_csv),
                "--base-queue", str(base_queue),
                "--output", str(output),
                "--ledger", str(ledger),
                "--retarget-output-dir", str(out_dir),
            ]
            first = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=True)
            first_manifest = json.loads(first.stdout)
            self.assertEqual(first_manifest["rows_written"], 1)
            self.assertEqual(first_manifest["counts"]["with_feedback"], 1)
            with output.open(newline="") as h:
                rows = list(csv.DictReader(h))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["retarget_source_handle"], "phone-1")
            self.assertIn("__retarget_", rows[0]["handle"])
            self.assertEqual(rows[0]["retarget_hint"], "LinkedIn: https://linkedin.test/jane")

            second = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=True)
            second_manifest = json.loads(second.stdout)
            self.assertEqual(second_manifest["rows_written"], 0)
            self.assertEqual(second_manifest["counts"]["skipped_already_attempted"], 1)

            # Changing feedback text creates a new hash/attempt.
            with review_csv.open("w", newline="") as h:
                w = csv.DictWriter(h, fieldnames=["handle", "full_name", "phone_e164", "total_messages", "retarget_hint"])
                w.writeheader()
                w.writerow({"handle": "phone-1", "full_name": "Jane Doe", "phone_e164": "+14155550101", "total_messages": "5", "retarget_hint": "Jane Doe at Acme"})
            third = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=True)
            third_manifest = json.loads(third.stdout)
            self.assertEqual(third_manifest["rows_written"], 1)

            with output.open(newline="") as h:
                rerun_rows = list(csv.DictReader(h))
            retarget_handle = rerun_rows[0]["handle"]
            profile_dir = out_dir / retarget_handle
            profile_dir.mkdir(parents=True)
            (profile_dir / "01_research_parallel.json").write_text(json.dumps({
                "person": {"full_name": "Jane Acme", "confidence": 0.93},
                "social": {"linkedin_url": "https://linkedin.test/jane-acme"},
                "location": {"city": "San Francisco", "country": "United States"},
                "positions": [{"title": "Founder", "company_name": "Acme"}],
                "education": [{"school_name": "MIT"}],
                "summary": {"text": "Retargeted profile."},
                "metadata": {"research_notes": "Matched user feedback."},
            }), encoding="utf-8")
            marked = subprocess.run([
                "python3", str(self.PREPARE), "mark-completed",
                "--ledger", str(ledger),
                "--retarget-output-dir", str(out_dir),
                "--review-csv", str(review_csv),
            ], cwd=ROOT, capture_output=True, text=True, check=True)
            marked_manifest = json.loads(marked.stdout)
            self.assertEqual(marked_manifest["review_rows_merged"], 1)
            with review_csv.open(newline="") as h:
                reviewed = list(csv.DictReader(h))
            self.assertEqual(reviewed[0]["retarget_status"], "re_researched")
            self.assertEqual(reviewed[0]["retarget_profile_status"], "new_profile")
            self.assertEqual(reviewed[0]["retarget_linkedin_url"], "https://linkedin.test/jane-acme")
            self.assertEqual(reviewed[0]["top_title_company_pairs"], "Founder @ Acme")

    def test_merge_cached_retarget_result_without_existing_attempt(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            review_csv = tmp / "research_review.csv"
            ledger = tmp / "retarget_attempts.json"
            out_dir = tmp / "research_retarget"
            hint = "Jane Doe at Acme"
            with review_csv.open("w", newline="") as h:
                w = csv.DictWriter(h, fieldnames=["handle", "full_name", "phone_e164", "total_messages", "retarget_hint"])
                w.writeheader()
                w.writerow({"handle": "phone-1", "full_name": "Jane Doe", "phone_e164": "+14155550101", "total_messages": "5", "retarget_hint": hint})
            h = hashlib.sha256(hint.lower().encode("utf-8")).hexdigest()[:16]
            retarget_handle = f"phone-1__retarget_{h[:10]}"
            profile_dir = out_dir / retarget_handle
            profile_dir.mkdir(parents=True)
            (profile_dir / "01_research_parallel.json").write_text(json.dumps({
                "person": {"full_name": "Jane Acme", "confidence": 0.93},
                "social": {"linkedin_url": "https://linkedin.test/jane-acme"},
                "positions": [{"title": "Founder", "company_name": "Acme"}],
                "metadata": {"research_notes": "cached retarget"},
            }), encoding="utf-8")

            result = subprocess.run([
                "python3", str(self.PREPARE), "merge-cached",
                "--ledger", str(ledger),
                "--retarget-output-dir", str(out_dir),
                "--review-csv", str(review_csv),
            ], cwd=ROOT, capture_output=True, text=True, check=True)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["completed_recorded"], 1)
            self.assertEqual(payload["review_rows_merged"], 1)
            with review_csv.open(newline="") as h:
                row = next(csv.DictReader(h))
            self.assertEqual(row["retarget_status"], "re_researched")
            self.assertEqual(row["retarget_linkedin_url"], "https://linkedin.test/jane-acme")


class RefreshRetargetLinkedInProfilesTests(unittest.TestCase):
    MODULE_PATH = ROOT / "packs/messages/primitives/refresh_retarget_linkedin_profiles/refresh_retarget_linkedin_profiles.py"

    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location("refresh_retarget_linkedin_profiles", cls.MODULE_PATH)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)  # type: ignore[union-attr]

    def test_exact_linkedin_hint_overwrites_stale_cached_profile(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            review_csv = tmp / "research_review.csv"
            ledger = tmp / "retarget_attempts.json"
            out_dir = tmp / "research_retarget"
            hint = "new profile: https://www.linkedin.com/in/charles-lin/"
            with review_csv.open("w", newline="", encoding="utf-8") as h:
                writer = csv.DictWriter(h, fieldnames=["handle", "full_name", "retarget_hint"])
                writer.writeheader()
                writer.writerow({"handle": "phone-1", "full_name": "Old Charles", "retarget_hint": hint})

            hsh = hashlib.sha256(hint.lower().encode("utf-8")).hexdigest()[:16]
            retarget_handle = f"phone-1__retarget_{hsh[:10]}"
            profile_dir = out_dir / retarget_handle
            profile_dir.mkdir(parents=True)
            (profile_dir / "01_research_parallel.json").write_text(json.dumps({
                "person": {"full_name": "Old Charles", "confidence": 0.4},
                "social": {"linkedin_url": "https://www.linkedin.com/in/old-charles"},
                "positions": [{"title": "Old Role", "company_name": "OldCo"}],
                "metadata": {"research_method": "parallel-core2x"},
            }), encoding="utf-8")

            rapid_response = {
                "data": {
                    "full_name": "Charles Lin",
                    "headline": "Founder at NewCo",
                    "linkedin_url": "https://www.linkedin.com/in/charles-lin/",
                    "city": "San Francisco",
                    "country": "United States",
                    "experiences": [
                        {"title": "Founder", "company_name": "NewCo", "starts_at": {"year": 2022}},
                    ],
                    "education": [
                        {"school_name": "Stanford University", "degree_name": "BS"},
                    ],
                }
            }
            args = SimpleNamespace(
                review_csv=review_csv,
                ledger=ledger,
                retarget_output_dir=out_dir,
                manifest=None,
                force=False,
                sleep_seconds=0,
            )
            with mock.patch.dict(os.environ, {"RAPIDAPI_LINKEDIN_KEY": "test-key"}, clear=False), \
                    mock.patch.object(self.mod, "rapidapi_profile", return_value={"status_code": 200, "data": rapid_response, "error": ""}):
                self.assertEqual(self.mod.cmd_run(args), 0)

            profile = json.loads((profile_dir / "01_research_parallel.json").read_text(encoding="utf-8"))
            self.assertEqual(profile["person"]["full_name"], "Charles Lin")
            self.assertEqual(profile["social"]["linkedin_url"], "https://www.linkedin.com/in/charles-lin")
            self.assertEqual(profile["positions"][0]["title"], "Founder")
            self.assertEqual(profile["positions"][0]["company_name"], "NewCo")
            self.assertEqual(profile["education"][0]["school_name"], "Stanford University")
            self.assertEqual(profile["metadata"]["research_method"], "rapidapi-linkedin-retarget")

            attempts = json.loads(ledger.read_text(encoding="utf-8"))["attempts"]["phone-1"]
            self.assertEqual(attempts[-1]["status"], "completed")
            self.assertEqual(attempts[-1]["provider"], "rapidapi")
            rows, counts = self.mod.candidate_rows(review_csv, out_dir)
            self.assertEqual(rows, [])
            self.assertEqual(counts["skipped_already_refreshed"], 1)

    def test_empty_rapidapi_profile_does_not_overwrite_cached_profile(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            review_csv = tmp / "research_review.csv"
            ledger = tmp / "retarget_attempts.json"
            out_dir = tmp / "research_retarget"
            hint = "https://www.linkedin.com/in/arthur-lam/"
            with review_csv.open("w", newline="", encoding="utf-8") as h:
                writer = csv.DictWriter(h, fieldnames=["handle", "full_name", "retarget_hint"])
                writer.writeheader()
                writer.writerow({"handle": "phone-2", "full_name": "Arthur Lam", "retarget_hint": hint})

            hsh = hashlib.sha256(hint.lower().encode("utf-8")).hexdigest()[:16]
            retarget_handle = f"phone-2__retarget_{hsh[:10]}"
            profile_dir = out_dir / retarget_handle
            profile_dir.mkdir(parents=True)
            cached = {
                "person": {"full_name": "Cached Arthur", "confidence": 0.8},
                "social": {"linkedin_url": "https://www.linkedin.com/in/cached-arthur"},
                "positions": [{"title": "Founder", "company_name": "CachedCo"}],
                "metadata": {"research_method": "parallel-core2x"},
            }
            (profile_dir / "01_research_parallel.json").write_text(json.dumps(cached), encoding="utf-8")

            args = SimpleNamespace(
                review_csv=review_csv,
                ledger=ledger,
                retarget_output_dir=out_dir,
                manifest=None,
                force=False,
                sleep_seconds=0,
            )
            with mock.patch.dict(os.environ, {"RAPIDAPI_LINKEDIN_KEY": "test-key"}, clear=False), \
                    mock.patch.object(self.mod, "rapidapi_profile", return_value={"status_code": 200, "data": {"data": {}}, "error": ""}):
                self.assertEqual(self.mod.cmd_run(args), 0)

            profile = json.loads((profile_dir / "01_research_parallel.json").read_text(encoding="utf-8"))
            self.assertEqual(profile, cached)
            manifest = json.loads((out_dir / "_rapidapi_retarget_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "ok_with_failures")
            self.assertEqual(manifest["failed"], 1)


class LlmReviewContactsTests(unittest.TestCase):
    LLM = ROOT / "packs/messages/primitives/llm_review_contacts/llm_review_contacts.py"

    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location("llm_review_contacts", cls.LLM)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)  # type: ignore[union-attr]

    def test_estimate_respects_batch_size(self) -> None:
        contacts = [{"name": f"Person {i}", "message_count": "1"} for i in range(45)]
        estimate = self.mod.estimate_cost(contacts, "anthropic/claude-sonnet-4-6", batch_size=20)
        self.assertEqual(estimate["batches"], 3)

    def test_review_uses_parallel_workers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            contacts = tmp / "contacts.csv"
            fields = ["phone", "name", "source", "is_in_group_chats", "group_names", "message_count", "last_message", "skip", "match_status", "matched_person_id"]
            with contacts.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for idx in range(5):
                    writer.writerow({
                        "phone": f"+1555000000{idx}",
                        "name": f"Person {idx}",
                        "source": "imessage",
                        "message_count": "1",
                    })
            args = type("Args", (), {
                "api_key": "test-key",
                "dry_run": False,
                "input": str(contacts),
                "all": False,
                "include_skipped": False,
                "model": "anthropic/claude-sonnet-4-6",
                "batch_size": 2,
                "max_workers": 2,
                "max_retries": 0,
                "timeout": 1,
                "results": str(tmp / "results.jsonl"),
                "manifest": str(tmp / "manifest.json"),
            })()

            def fake_call(_api_key, contacts_json, _model, *, timeout, max_retries):
                batch = json.loads(contacts_json)
                return (
                    [{"idx": item["idx"], "verdict": "ENRICH", "reason": "full name"} for item in batch],
                    10,
                    5,
                    None,
                )

            with mock.patch.object(self.mod, "call_openrouter_with_retries", side_effect=fake_call) as call, \
                 mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                rc = self.mod.cmd_review(args)
            self.assertEqual(rc, 0)
            self.assertEqual(call.call_count, 3)
            self.assertIn("[llm_review_contacts] started 0/3 batches", stderr.getvalue())
            self.assertIn("[llm_review_contacts] completed 3/3 batches", stderr.getvalue())
            manifest = json.loads((tmp / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["batch_size"], 2)
            self.assertEqual(manifest["max_workers"], 2)
            self.assertEqual(manifest["counts"]["verdicts"], 5)

    def test_review_clears_skip_when_llm_enriches_normal_full_names(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            contacts = tmp / "contacts.csv"
            fields = ["phone", "name", "source", "is_in_group_chats", "group_names", "message_count", "last_message", "skip", "match_status", "matched_person_id"]
            with contacts.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow({
                    "phone": "+15550000001",
                    "name": "Hind Bahwan",
                    "source": "imessage",
                    "message_count": "",
                    "skip": "yes",
                    "match_status": "unmatched",
                })
            args = type("Args", (), {
                "api_key": "test-key",
                "dry_run": False,
                "input": str(contacts),
                "all": False,
                "include_skipped": True,
                "model": "anthropic/claude-sonnet-4-6",
                "batch_size": 20,
                "max_workers": 2,
                "max_retries": 0,
                "timeout": 1,
                "results": str(tmp / "results.jsonl"),
                "manifest": str(tmp / "manifest.json"),
            })()

            def fake_call(_api_key, contacts_json, _model, *, timeout, max_retries):
                batch = json.loads(contacts_json)
                return (
                    [{"idx": item["idx"], "verdict": "ENRICH", "reason": "normal full name"} for item in batch],
                    10,
                    5,
                    None,
                )

            with mock.patch.object(self.mod, "call_openrouter_with_retries", side_effect=fake_call) as call:
                rc = self.mod.cmd_review(args)
            self.assertEqual(rc, 0)
            call.assert_called_once()

            with contacts.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["skip"], "")
            manifest = json.loads((tmp / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["counts"]["enrich"], 1)


class DeepResearchContactsTests(unittest.TestCase):
    DR = ROOT / "packs/messages/primitives/deep_research_contacts/deep_research_contacts.py"

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


class BuildResearchReviewCsvTests(unittest.TestCase):
    BUILD = ROOT / "packs/messages/primitives/build_research_review_csv/build_research_review_csv.py"

    def _load_build_module(self):
        spec = importlib.util.spec_from_file_location("build_research_review_csv_test", self.BUILD)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        return module

    def _write_research_artifact(self, root, handle, *, real_name, linkedin, name_conf, positions, city="", country=""):
        d = root / handle
        d.mkdir(parents=True, exist_ok=True)
        (d / "00_parallel_raw.json").write_text(
            json.dumps(
                {
                    "real_name": real_name,
                    "name_confidence": name_conf,
                    "linkedin_url": linkedin,
                }
            )
        )
        (d / "01_research_parallel.json").write_text(
            json.dumps(
                {
                    "person": {"full_name": real_name or "", "confidence": name_conf, "notes": ""},
                    "social": {"linkedin_url": linkedin},
                    "location": {"city": city, "country": country},
                    "positions": positions,
                    "education": [],
                    "summary": {"text": ""},
                    "metadata": {"research_notes": ""},
                }
            )
        )

    def test_network_review_uses_openai_key_when_openrouter_missing(self) -> None:
        build = self._load_build_module()
        args = SimpleNamespace(api_key=None, model="openai/gpt-4.1")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-openai"}, clear=True):
            provider, key, model = build.llm_provider_and_key(args)
        self.assertEqual(provider, "openai")
        self.assertEqual(key, "test-openai")
        self.assertEqual(model, "gpt-4.1")

    def test_network_review_retries_502_and_succeeds(self) -> None:
        build = self._load_build_module()
        calls = {"count": 0}

        def fake_chat(*_args, **_kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                return None, "HTTP 502: upstream"
            return {"parsed": {"bucket": "yes", "short_reason": "ok", "identity_risk": "", "signals": []}, "usage": {}}, None

        with mock.patch.object(build, "_chat_completion", side_effect=fake_chat), \
             mock.patch.object(build.time, "sleep", return_value=None):
            response, err = build._chat_completion_with_retries(
                "key", "model", [], provider="openrouter", max_retries=1
            )
        self.assertIsNone(err)
        self.assertEqual(response["parsed"]["bucket"], "yes")
        self.assertEqual(calls["count"], 2)

    def test_network_review_cache_buckets_and_tui_columns(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            research_dir = tmp / "research"
            queue = tmp / "queue.csv"
            output = tmp / "review.csv"

            def write_network_review(handle: str, bucket: str, reason: str, risk: str, signals: list[str]) -> None:
                (research_dir / handle / "03_network_review.json").write_text(
                    json.dumps({
                        "handle": handle,
                        "model": "openai/gpt-4.1",
                        "review": {
                            "bucket": bucket,
                            "short_reason": reason,
                            "identity_risk": risk,
                            "signals": signals,
                        },
                    }),
                    encoding="utf-8",
                )

            self._write_research_artifact(
                research_dir,
                "phone-1111111111",
                real_name="Jane Marie Doe",
                linkedin="https://l/in/jane",
                name_conf=0.95,
                positions=[{"title": "Director", "company_name": "Roblox"}],
                city="San Francisco",
                country="United States",
            )
            write_network_review("phone-1111111111", "confident", "Strong operator signal.", "", ["linkedin", "operator"])

            self._write_research_artifact(
                research_dir,
                "phone-2222222222",
                real_name="Bob Smith",
                linkedin=None,
                name_conf=0.7,
                positions=[{"title": "Engineer", "company_name": "Acme"}],
            )
            write_network_review("phone-2222222222", "medium", "Some career signal.", "identity_uncertain", ["career"])

            self._write_research_artifact(
                research_dir,
                "phone-3333333333",
                real_name="Anita Kapadia",
                linkedin="https://l/in/anita",
                name_conf=0.9,
                positions=[{"title": "VP", "company_name": "Bigco"}],
            )
            write_network_review("phone-3333333333", "review", "Likely wrong person.", "wrong_person", ["name_mismatch"])

            self._write_research_artifact(
                research_dir,
                "phone-4444444444",
                real_name=None,
                linkedin=None,
                name_conf=0.0,
                positions=[],
            )
            write_network_review("phone-4444444444", "no", "No real name surfaced.", "no_real_name", [])

            # Queue rows for the first 3 handles. Last handle deliberately
            # missing from queue to exercise --allow-missing-queue.
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
            with queue.open("w", newline="") as h:
                w = csv.DictWriter(h, fieldnames=rq_headers)
                w.writeheader()
                for handle, name, phone in [
                    ("phone-1111111111", "Jane Doe", "+14155551111"),
                    ("phone-2222222222", "Bob Smith", "+14155552222"),
                    # Input name "Eric Ting" vs returned "Anita Kapadia" → wrong_person
                    ("phone-3333333333", "Eric Ting", "+14155553333"),
                ]:
                    w.writerow(
                        {k: "" for k in rq_headers}
                        | {
                            "handle": handle,
                            "display_name": name,
                            "phone_e164": phone,
                            "area_code": "415",
                            "source_channel": "phone",
                            "message_source": "imessage,whatsapp",
                            "total_messages": "100",
                        }
                    )

            result = subprocess.run(
                [
                    "python3",
                    str(self.BUILD),
                    "build",
                    "--research-dir",
                    str(research_dir),
                    "--queue-csv",
                    str(queue),
                    "--output-csv",
                    str(output),
                    "--allow-missing-queue",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            manifest = json.loads(result.stdout)
            self.assertIn("[build_research_review_csv] started 0/4 handles", result.stderr)
            self.assertIn("[build_research_review_csv] completed 4/4 handles", result.stderr)
            self.assertEqual(manifest["rows_written"], 4)
            self.assertEqual(manifest["bucket_counts"], {"yes": 1, "maybe": 2, "no": 1})

            with output.open(newline="") as h:
                fieldnames = next(csv.reader(h))
            # The contact-exporter TUI _is_research_csv requires these 4 keys
            required = {"bucket", "full_name", "phone_e164", "top_title_company_pairs", "exclude", "enrich_decision"}
            self.assertTrue(
                required.issubset(set(fieldnames)), f"missing TUI-routing columns: {required - set(fieldnames)}"
            )

            with output.open(newline="") as h:
                rows = list(csv.DictReader(h))
            by_handle = {r["handle"]: r for r in rows}

            jane = by_handle["phone-1111111111"]
            self.assertEqual(jane["bucket"], "yes")
            self.assertEqual(jane["top_title_company_pairs"], "Director @ Roblox")
            self.assertEqual(jane["location_city"], "San Francisco")

            bob = by_handle["phone-2222222222"]
            self.assertEqual(bob["bucket"], "maybe")

            mismatch = by_handle["phone-3333333333"]
            self.assertEqual(mismatch["bucket"], "maybe")
            self.assertEqual(mismatch["identity_risk"], "wrong_person")

            empty = by_handle["phone-4444444444"]
            self.assertEqual(empty["bucket"], "no")
            self.assertEqual(empty["identity_risk"], "no_real_name")

    def test_network_review_cache_is_used_without_llm(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            research_dir = tmp / "research"
            queue = tmp / "queue.csv"
            output = tmp / "review.csv"

            handle = "phone-5555555555"
            self._write_research_artifact(
                research_dir,
                handle,
                real_name="Sheikh Saoud Salem Abdulaziz Al-Sabah",
                linkedin=None,
                name_conf=0.35,
                positions=[{"title": "Managing Director", "company_name": "Kuwait Investment Authority"}],
                city="Kuwait City",
                country="Kuwait",
            )
            (research_dir / handle / "03_network_review.json").write_text(
                json.dumps(
                    {
                        "handle": handle,
                        "full_name": "Sheikh Saoud Salem Abdulaziz Al-Sabah",
                        "model": "gpt-4.1",
                        "review": {
                            "bucket": "confident",
                            "short_reason": "Sovereign wealth leadership; prioritize despite phone ambiguity.",
                            "identity_risk": "Plausible but phone link unconfirmed.",
                            "signals": ["sovereign wealth", "royal-family signal"],
                        },
                    }
                )
            )
            with queue.open("w", newline="") as h:
                writer = csv.DictWriter(h, fieldnames=["handle", "display_name", "phone_e164", "area_code"])
                writer.writeheader()
                writer.writerow(
                    {
                        "handle": handle,
                        "display_name": "Saoud Al-Sabah",
                        "phone_e164": "+96599011511",
                        "area_code": "965",
                    }
                )

            result = subprocess.run(
                [
                    "python3",
                    str(self.BUILD),
                    "build",
                    "--research-dir",
                    str(research_dir),
                    "--queue-csv",
                    str(queue),
                    "--output-csv",
                    str(output),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            manifest = json.loads(result.stdout)
            self.assertEqual(manifest["counts"]["scored_via_network_review"], 1)

            with output.open(newline="") as h:
                rows = list(csv.DictReader(h))
            self.assertEqual(rows[0]["bucket"], "yes")
            self.assertEqual(rows[0]["short_reason"], "Sovereign wealth leadership; prioritize despite phone ambiguity.")
            self.assertIn("sovereign wealth", rows[0]["signals"])

    def test_previous_review_decisions_are_carried_forward(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            research_dir = tmp / "research"
            queue = tmp / "queue.csv"
            previous = tmp / "previous_review.csv"
            output = tmp / "review.csv"
            handle = "phone-5555551212"

            self._write_research_artifact(
                research_dir,
                handle,
                real_name="Rina Investor",
                linkedin="https://www.linkedin.com/in/rina-investor/",
                name_conf=0.93,
                positions=[{"title": "Partner", "company_name": "Example Capital"}],
            )
            (research_dir / handle / "03_network_review.json").write_text(
                json.dumps(
                    {
                        "handle": handle,
                        "model": "openai/gpt-4.1",
                        "review": {
                            "bucket": "medium",
                            "short_reason": "Investor signal, needs context.",
                            "identity_risk": "",
                            "signals": ["investor"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            with queue.open("w", newline="") as h:
                writer = csv.DictWriter(h, fieldnames=["handle", "display_name", "phone_e164", "area_code"])
                writer.writeheader()
                writer.writerow({
                    "handle": handle,
                    "display_name": "Rina Investor",
                    "phone_e164": "+14155551212",
                    "area_code": "415",
                })
            with previous.open("w", newline="") as h:
                writer = csv.DictWriter(
                    h,
                    fieldnames=["bucket", "handle", "phone_e164", "exclude", "enrich_decision", "retarget_hint"],
                )
                writer.writeheader()
                writer.writerow({
                    "bucket": "review",
                    "handle": handle,
                    "phone_e164": "+14155551212",
                    "exclude": "no",
                    "enrich_decision": "yes",
                    "retarget_hint": "https://www.linkedin.com/in/rina-investor/",
                })

            result = subprocess.run(
                [
                    "python3",
                    str(self.BUILD),
                    "build",
                    "--research-dir",
                    str(research_dir),
                    "--queue-csv",
                    str(queue),
                    "--output-csv",
                    str(output),
                    "--previous-csv",
                    str(previous),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            manifest = json.loads(result.stdout)
            self.assertEqual(manifest["counts"]["previous_review_decision_rows"], 1)
            self.assertEqual(manifest["counts"]["previous_review_decisions_applied"], 1)

            with output.open(newline="") as h:
                rows = list(csv.DictReader(h))
            self.assertEqual(rows[0]["bucket"], "maybe")
            self.assertEqual(rows[0]["exclude"], "no")
            self.assertEqual(rows[0]["enrich_decision"], "yes")
            self.assertEqual(rows[0]["retarget_hint"], "https://www.linkedin.com/in/rina-investor/")

    def test_previous_yes_no_buckets_are_carried_forward_as_explicit_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            research_dir = tmp / "research"
            queue = tmp / "queue.csv"
            previous = tmp / "previous_review.csv"
            output = tmp / "review.csv"
            handle = "phone-5555599999"

            self._write_research_artifact(
                research_dir,
                handle,
                real_name="Nora Builder",
                linkedin="https://www.linkedin.com/in/nora-builder/",
                name_conf=0.9,
                positions=[{"title": "Founder", "company_name": "Example"}],
            )
            (research_dir / handle / "03_network_review.json").write_text(
                json.dumps({"review": {"bucket": "medium", "short_reason": "ok", "identity_risk": "", "signals": []}}),
                encoding="utf-8",
            )
            with queue.open("w", newline="") as h:
                writer = csv.DictWriter(h, fieldnames=["handle", "display_name", "phone_e164"])
                writer.writeheader()
                writer.writerow({"handle": handle, "display_name": "Nora Builder", "phone_e164": "+14155559999"})
            with previous.open("w", newline="") as h:
                writer = csv.DictWriter(h, fieldnames=["bucket", "handle", "phone_e164", "retarget_hint"])
                writer.writeheader()
                writer.writerow({"bucket": "no", "handle": handle, "phone_e164": "+14155559999", "retarget_hint": "wrong person"})

            subprocess.run(
                [
                    "python3", str(self.BUILD), "build",
                    "--research-dir", str(research_dir),
                    "--queue-csv", str(queue),
                    "--output-csv", str(output),
                    "--previous-csv", str(previous),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            with output.open(newline="") as h:
                rows = list(csv.DictReader(h))
            self.assertEqual(rows[0]["exclude"], "yes")
            self.assertEqual(rows[0]["retarget_hint"], "wrong person")

    def test_llm_bucket_writes_upstream_network_review_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            research_dir = tmp / "research"
            queue = tmp / "queue.csv"
            output = tmp / "review.csv"
            handle = "phone-6666666666"
            self._write_research_artifact(
                research_dir,
                handle,
                real_name="Aisha Founder",
                linkedin="https://www.linkedin.com/in/aisha-founder/",
                name_conf=0.91,
                positions=[{"title": "Founder", "company_name": "Example AI"}],
                city="San Francisco",
                country="United States",
            )
            with queue.open("w", newline="") as h:
                writer = csv.DictWriter(h, fieldnames=[
                    "handle",
                    "display_name",
                    "phone_e164",
                    "source_channel",
                    "message_source",
                    "total_messages",
                    "imessage_message_count",
                    "whatsapp_message_count",
                ])
                writer.writeheader()
                writer.writerow({
                    "handle": handle,
                    "display_name": "Aisha Founder",
                    "phone_e164": "+14155556666",
                    "source_channel": "phone",
                    "message_source": "imessage,whatsapp",
                    "total_messages": "30",
                    "imessage_message_count": "11",
                    "whatsapp_message_count": "19",
                })

            spec = importlib.util.spec_from_file_location("build_research_review_csv", self.BUILD)
            assert spec and spec.loader
            build_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(build_mod)
            args = type("Args", (), {
                "research_dir": research_dir,
                "queue_csv": str(queue),
                "output_csv": str(output),
                "manifest": None,
                "model": "openai/gpt-4.1",
                "api_key": "test-key",
                "refresh_cache": False,
                "allow_missing_queue": False,
            })()

            llm_response = {
                "parsed": {
                    "bucket": "yes",
                    "short_reason": "Founder with credible AI startup signal.",
                    "identity_risk": "",
                    "signals": ["founder", "venture-network"],
                },
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            }
            with mock.patch.object(build_mod, "_chat_completion_with_retries", return_value=(llm_response, None)), \
                    mock.patch.object(build_mod, "emit"):
                rc = build_mod.cmd_build(args)

            self.assertEqual(rc, 0)
            network_review = json.loads((research_dir / handle / "03_network_review.json").read_text(encoding="utf-8"))
            self.assertEqual(network_review["handle"], handle)
            self.assertEqual(network_review["public_identifier"], "aisha-founder")
            self.assertEqual(network_review["source_channel"], "imessage,whatsapp")
            self.assertEqual(network_review["message_source"], "imessage,whatsapp")
            self.assertEqual(network_review["model"], "openai/gpt-4.1")
            self.assertEqual(network_review["review"]["bucket"], "yes")
            with output.open(newline="") as h:
                rows = list(csv.DictReader(h))
            self.assertEqual(rows[0]["message_source"], "imessage,whatsapp")
            self.assertEqual(rows[0]["total_messages"], "30")
            self.assertEqual(rows[0]["imessage_message_count"], "11")
            self.assertEqual(rows[0]["whatsapp_message_count"], "19")
            manifest = json.loads(output.with_suffix(output.suffix + ".manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["review_source"], "network_review_llm")
            self.assertEqual(manifest["counts"]["network_review_written"], 1)

    def test_llm_review_failure_does_not_fall_back_to_heuristic(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            research_dir = tmp / "research"
            queue = tmp / "queue.csv"
            output = tmp / "review.csv"
            handle = "phone-7777777777"
            self._write_research_artifact(
                research_dir,
                handle,
                real_name="Aisha Founder",
                linkedin="https://www.linkedin.com/in/aisha-founder/",
                name_conf=0.91,
                positions=[{"title": "Founder", "company_name": "Example AI"}],
                city="San Francisco",
                country="United States",
            )
            with queue.open("w", newline="") as h:
                writer = csv.DictWriter(h, fieldnames=["handle", "display_name", "phone_e164", "source_channel"])
                writer.writeheader()
                writer.writerow({
                    "handle": handle,
                    "display_name": "Aisha Founder",
                    "phone_e164": "+14155557777",
                    "source_channel": "phone",
                })

            spec = importlib.util.spec_from_file_location("build_research_review_csv", self.BUILD)
            assert spec and spec.loader
            build_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(build_mod)
            args = type("Args", (), {
                "research_dir": research_dir,
                "queue_csv": str(queue),
                "output_csv": str(output),
                "manifest": None,
                "model": "openai/gpt-4.1",
                "api_key": "test-key",
                "refresh_cache": False,
                "allow_missing_queue": False,
            })()

            emitted = []
            with mock.patch.object(build_mod, "_chat_completion_with_retries", return_value=(None, "HTTP 500")), \
                    mock.patch.object(build_mod, "emit", side_effect=emitted.append):
                rc = build_mod.cmd_build(args)

            self.assertEqual(rc, 1)
            self.assertEqual(emitted[-1]["status"], "failed")
            self.assertIn("network review failed", emitted[-1]["error"])
            self.assertFalse((research_dir / handle / "03_network_review.json").exists())


class ReviewContactsWebTests(unittest.TestCase):
    WEB = ROOT / "packs/messages/primitives/review_contacts_web/review_contacts_web.py"

    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location("review_contacts_web", cls.WEB)
        assert spec and spec.loader
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def test_default_selection_drops_bad_names_but_keeps_matches(self) -> None:
        rows = [
            {"name": "", "phone": "+14155550101", "skip": ""},
            {"name": "Alice Hinge", "phone": "+14155550202", "skip": ""},
            {"name": "5550303", "phone": "+14155550303", "skip": ""},
            {"name": "Tanner", "phone": "+14155550404", "skip": ""},
            {"name": "Jane Doe", "phone": "+14155550505", "skip": ""},
            {"name": "", "phone": "+14155550606", "skip": "", "matched_person_id": "p1", "match_status": "matched"},
            {"name": "Bob Smith", "phone": "+14155550707", "skip": "true"},
            {"name": "Charlie Raya", "phone": "+14155550808", "skip": "", "enrich_decision": "yes"},
        ]
        selected = [self.mod.contact_selected(row) for row in rows]
        self.assertEqual(selected, [False, False, False, False, True, True, False, True])
        self.assertEqual(self.mod.drop_reason(rows[0]), "no name")
        self.assertEqual(self.mod.drop_reason(rows[1]), "blocked name token")
        self.assertEqual(self.mod.drop_reason(rows[2]), "name is phone")
        self.assertEqual(self.mod.drop_reason(rows[3]), "bad name")
        summary = self.mod.summarize(rows)
        self.assertEqual(summary["selected"], 3)


class ReviewResearchWebTests(unittest.TestCase):
    WEB = ROOT / "packs/messages/primitives/review_research_web/review_research_web.py"

    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location("review_research_web", cls.WEB)
        assert spec and spec.loader
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def test_decision_semantics_match_contact_exporter_upload(self) -> None:
        rows = [
            {"bucket": "confident", "exclude": ""},
            {"bucket": "medium", "exclude": ""},
            {"bucket": "review", "exclude": ""},
            {"bucket": "medium", "exclude": "no"},
            {"bucket": "confident", "exclude": "yes"},
        ]
        selected = [self.mod.is_selected(row) for row in rows]
        self.assertEqual(selected, [True, False, False, True, False])
        summary = self.mod.summarize(rows)
        self.assertEqual(summary["in_network"], 0)
        self.assertEqual(summary["yes"], 2)
        self.assertEqual(summary["maybe"], 2)
        self.assertEqual(summary["no"], 1)

    def test_in_network_rows_have_separate_review_tab(self) -> None:
        rows = [
            {"bucket": "maybe", "exclude": "", "in_network": "true", "network_person_id": "p1"},
            {"bucket": "yes", "exclude": "", "in_network": "", "network_person_id": ""},
            {"bucket": "maybe", "exclude": "", "in_network": "", "network_person_id": ""},
        ]
        summary = self.mod.summarize(rows)
        self.assertEqual(summary, {"in_network": 1, "yes": 1, "maybe": 1, "no": 0})
        self.assertTrue(self.mod.is_selected(rows[0]))
        self.assertTrue(self.mod.is_selected(rows[1]))
        self.assertFalse(self.mod.is_selected(rows[2]))

    def test_review_defaults_to_yes_tab(self) -> None:
        rows = [
            {"bucket": "maybe", "exclude": "", "in_network": "true", "network_person_id": "p1"},
            {"bucket": "yes", "exclude": "", "in_network": "", "network_person_id": ""},
            {"bucket": "maybe", "exclude": "", "in_network": "", "network_person_id": ""},
        ]
        self.assertTrue(self.mod.matches_filter(rows[1], {}, None))
        self.assertFalse(self.mod.matches_filter(rows[0], {}, None))

    def test_review_search_matches_name_only_and_preserves_query_across_tabs(self) -> None:
        row = {
            "bucket": "yes",
            "exclude": "",
            "full_name": "Alice Founder",
            "top_title_company_pairs": "CEO @ Acme Labs",
            "schools": "Stanford University",
            "signals": "repeat_founder",
        }

        self.assertTrue(self.mod.matches_filter(row, {"tab": ["yes"], "q": ["Alice"]}, None))
        self.assertFalse(self.mod.matches_filter(row, {"tab": ["yes"], "q": ["Acme"]}, None))
        self.assertFalse(self.mod.matches_filter(row, {"tab": ["yes"], "q": ["Stanford"]}, None))

        html = self.mod.page_html(Path("review.csv"), [row], {"tab": ["yes"], "q": ["Alice"]}, None).decode("utf-8")
        self.assertIn("placeholder='Search by name…'", html)
        self.assertIn("aria-label='Search by name'", html)
        self.assertIn("href='/?q=Alice&amp;tab=maybe'", html)
        self.assertNotIn("<strong>source</strong>", html)
        self.assertNotIn("<button type='submit'>Filter</button>", html)
        self.assertNotIn("class='filters'", html)

    def test_review_render_uses_warm_design_tokens(self) -> None:
        html = self.mod.page_html(Path("review.csv"), [{"bucket": "yes", "exclude": "", "full_name": "Jane Doe"}], {"tab": ["yes"]}, None).decode("utf-8")

        for token in ["--bg:#F7F3EE", "--surface:#FDFAF7", "--border:#E8DDD6", "--red:#F2502A", "--success-border:#BBF7D0"]:
            self.assertIn(token, html)
        for old_token in ["--bg:#f5f6f8", "--panel:#fff", "--line:#d8dee6"]:
            self.assertNotIn(old_token, html)
        self.assertIn("These contacts are strong candidates for your Personal Network.", html)
        self.assertIn("background:#0A66C2", html)
        self.assertIn(".card.selected{background:var(--surface);border-color:var(--success-border)}", html)
        self.assertIn(".tab.yes.active,.tab.in_network.active{background:var(--success-tint);border-color:var(--success-border);color:var(--success)}", html)
        self.assertIn(".selected .decision{background:var(--success-tint);color:var(--success);border:1px solid var(--success-border)}", html)
        self.assertNotIn("rgba(34,197,94,.045)", html)
        self.assertIn("<div class='decision'>Yes</div>", html)
        self.assertNotIn("<div class='decision'>Included</div>", html)
        self.assertNotIn("<div class='decision'>Excluded</div>", html)
        self.assertNotIn("class='bucket", html)
        self.assertNotIn("<span class='bucket", html)
        self.assertNotIn("&middot; <strong>msgs</strong>", html)
        self.assertIn("<strong>phone</strong>", html)
        self.assertIn("<strong>msgs</strong>", html)

    def test_health_source_hash_identifies_running_ui_code(self) -> None:
        self.assertEqual(self.mod.SOURCE_SHA256, hashlib.sha256(self.WEB.read_bytes()).hexdigest())

    def test_health_endpoint_includes_source_hash(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            csv_path = Path(td) / "research_review.csv"
            csv_path.write_text("bucket,full_name\nyes,Jane Doe\n", encoding="utf-8")
            server = ThreadingHTTPServer(("127.0.0.1", 0), self.mod.make_handler(csv_path, None))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                _, port = server.server_address
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload["status"], "ok")
                self.assertEqual(payload["csv"], str(csv_path.resolve()))
                self.assertEqual(payload["source_sha256"], self.mod.SOURCE_SHA256)
            finally:
                server.shutdown()
                server.server_close()

    def test_bulk_in_network_selection_targets_all_network_rows(self) -> None:
        rows = [
            {"bucket": "maybe", "exclude": "", "in_network": "true", "network_person_id": "p1"},
            {"bucket": "maybe", "exclude": "yes", "in_network": "true", "network_person_id": "p2"},
            {"bucket": "maybe", "exclude": "", "in_network": "", "network_person_id": ""},
        ]
        changed = self.mod.apply_bulk_selection(rows, "in_network", False)
        self.assertEqual(changed, 1)
        self.assertEqual([row["exclude"] for row in rows], ["yes", "yes", ""])
        self.assertEqual(self.mod.summarize(rows), {"in_network": 0, "yes": 0, "maybe": 1, "no": 0})

        changed = self.mod.apply_bulk_selection(rows, "in_network", True)
        self.assertEqual(changed, 2)
        self.assertEqual([row["exclude"] for row in rows], ["no", "no", ""])
        self.assertEqual(self.mod.summarize(rows), {"in_network": 2, "yes": 0, "maybe": 1, "no": 0})

    def test_retargeted_cards_blank_feedback_without_new_profile_badge(self) -> None:
        row = {
            "bucket": "yes",
            "exclude": "",
            "handle": "phone-1",
            "full_name": "Jane Acme",
            "retarget_status": "re_researched",
            "retarget_profile_status": "new_profile",
            "retarget_hint": "old feedback should be hidden",
            "retarget_linkedin_url": "https://linkedin.test/jane-acme",
        }
        html = self.mod.page_html(Path("review.csv"), [row], {"tab": ["yes"]}, None).decode("utf-8")
        self.assertIn("re-researched", html)
        self.assertNotIn("new profile", html)
        self.assertIn("latest result</strong> showing latest re-researched profile", html)
        self.assertIn("new feedback", html)
        self.assertIn("Save feedback", html)
        self.assertNotIn("old feedback should be hidden", html)

    def test_profile_fields_are_loaded_from_research_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            handle = "phone-123"
            d = tmp / handle
            d.mkdir(parents=True)
            (d / "01_research_parallel.json").write_text(
                json.dumps(
                    {
                        "person": {"full_name": "Jane Doe"},
                        "social": {"linkedin_url": "https://linkedin.test/jane", "github_url": ""},
                        "location": {"city": "New York", "country": "United States"},
                        "positions": [{"title": "Founder", "company_name": "Acme"}],
                        "education": [{"school_name": "MIT"}],
                        "summary": {"text": "Founder profile"},
                        "metadata": {"research_notes": "public evidence"},
                    }
                )
            )
            view = self.mod.row_view({"handle": handle, "full_name": "Input Name"}, tmp)
            self.assertEqual(view["name"], "Jane Doe")
            self.assertEqual(view["location"], "New York, United States")
            self.assertEqual(view["title_pairs"], "Founder @ Acme")
            self.assertEqual(view["schools"], "MIT")
            self.assertEqual(view["linkedin_url"], "https://linkedin.test/jane")


class ImportContactsPipelineReviewServerTests(unittest.TestCase):
    PIPELINE = ROOT / "packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py"

    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location("import_contacts_pipeline", cls.PIPELINE)
        assert spec and spec.loader
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def test_review_server_reuse_requires_current_ui_source_hash(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_csv = Path(td) / "research_review.csv"
            review_csv.write_text("bucket,full_name\nyes,Jane Doe\n", encoding="utf-8")
            args = SimpleNamespace(review_host="127.0.0.1", review_port=8787, review_csv=review_csv)
            current_hash = "current-ui-hash"
            base_health = {
                "status": "ok",
                "csv": str(review_csv.resolve()),
                "source_sha256": current_hash,
            }

            with mock.patch.object(self.mod, "current_review_research_web_sha256", return_value=current_hash):
                with mock.patch.object(self.mod, "read_review_server_health", return_value=base_health):
                    self.assertTrue(self.mod.review_server_matches_current_csv(args))
                with mock.patch.object(self.mod, "read_review_server_health", return_value={**base_health, "source_sha256": "old-ui-hash"}):
                    self.assertFalse(self.mod.review_server_matches_current_csv(args))
                stale_health = {k: v for k, v in base_health.items() if k != "source_sha256"}
                with mock.patch.object(self.mod, "read_review_server_health", return_value=stale_health):
                    self.assertFalse(self.mod.review_server_matches_current_csv(args))
                other_csv = Path(td) / "other.csv"
                other_csv.write_text("bucket,full_name\nyes,Other Person\n", encoding="utf-8")
                with mock.patch.object(self.mod, "read_review_server_health", return_value={**base_health, "csv": str(other_csv.resolve())}):
                    self.assertFalse(self.mod.review_server_matches_current_csv(args))

    def test_review_server_source_summary_records_path_and_hash(self) -> None:
        summary = self.mod.review_server_source_summary()
        path = Path(summary["source_path"])
        self.assertEqual(path, self.mod.review_research_web_path())
        self.assertEqual(summary["source_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
