import csv
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NORMALIZE = ROOT / "packs/messages/primitives/normalize_message_contacts/normalize_message_contacts.py"
HARNESS = ROOT / "packs/messages/primitives/powerset_contacts_harness/powerset_contacts_harness.py"
IMESSAGE = ROOT / "packs/messages/primitives/extract_imessage_contacts/extract_imessage_contacts.py"


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
                writer.writerow({
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
                })
                writer.writerow({
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
                })

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
            self.assertEqual(rows[0]["message_count"], 20)
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
                    INSERT INTO ZABCDRECORD (Z_PK, ZFIRSTNAME, ZLASTNAME) VALUES (1, 'Jane', 'Doe');
                    INSERT INTO ZABCDPHONENUMBER (ZOWNER, ZFULLNUMBER) VALUES (1, '(415) 555-0101');
                    """
                )

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
            self.assertEqual(summary["counts"]["contacts"], 1)
            rows = [json.loads(line) for line in output_jsonl.read_text().splitlines()]
            self.assertEqual(rows[0]["phone"], "+14155550101")
            self.assertEqual(rows[0]["name"], "Jane Doe")
            self.assertEqual(rows[0]["message_count"], 2)
            self.assertTrue(rows[0]["is_in_group_chats"])
            self.assertEqual(rows[0]["group_names"], ["Founders"])


if __name__ == "__main__":
    unittest.main()
