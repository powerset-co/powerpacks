import csv
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "packs/ingestion/primitives/gmail_network_import/gmail_network_import.py"
spec = importlib.util.spec_from_file_location("gmail_network_import", MODULE_PATH)
gmail_network_import = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = gmail_network_import
spec.loader.exec_module(gmail_network_import)


class GmailNetworkImportTests(unittest.TestCase):
    def invoke(self, argv):
        buf = StringIO()
        with redirect_stdout(buf):
            code = gmail_network_import.main(argv)
        output = buf.getvalue().strip()
        payload = json.loads(output) if output else {}
        return code, payload

    def test_run_writes_powerpacks_local_artifacts_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.json"
            code, payload = self.invoke([
                "run",
                "--email", "Jane.Example@Example.com",
                "--name", "Jane Example",
                "--account-email", "me@gmail.com",
                "--account-id", "gmail-account-1",
                "--operator-id", "operator-12345678",
                "--output-dir", str(Path(tmp) / "out"),
                "--ledger", str(ledger),
                "--run-id", "run-test",
                "--force",
            ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "completed")
            self.assertIn("Local one-person Gmail seed completed", payload["summary"])
            artifacts = payload["artifacts"]
            targeted = Path(artifacts["targeted_emails_csv"])
            aggregated = Path(artifacts["gmail_contacts_aggregated_csv"])
            threads = Path(artifacts["gmail_threads_csv"])
            accounts = Path(artifacts["accounts_csv"])
            for path in (targeted, aggregated, threads, accounts):
                self.assertTrue(path.exists())
                self.assertIn(str(Path(tmp) / "out"), str(path))
            with targeted.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["primary_email"], "jane.example@example.com")
            self.assertEqual(rows[0]["primary_email_type"], "work")
            state = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual(state["steps"]["seed_one"]["status"], "completed")
            self.assertEqual(state["steps"]["prepare_local_workspace"]["status"], "completed")
            self.assertEqual(state["steps"]["write_next_steps"]["status"], "completed")
            self.assertNotIn("aleph_root", state)

    def test_continue_after_completed_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.json"
            self.invoke([
                "run",
                "--email", "jane@example.com",
                "--name", "Jane Example",
                "--output-dir", str(Path(tmp) / "out"),
                "--ledger", str(ledger),
                "--force",
            ])
            code, payload = self.invoke(["continue", "--ledger", str(ledger)])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "completed")

    def test_parse_email_header_port(self):
        parsed = gmail_network_import.parse_email_header('"Jane Example" <jane@example.com>, john@example.org')
        self.assertEqual(parsed, [("Jane Example", "jane@example.com"), ("", "john@example.org")])

    def test_msgvault_import_reads_metadata_without_subjects_or_bodies(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "msgvault.db"
            con = sqlite3.connect(db)
            con.executescript("""
                CREATE TABLE sources (id INTEGER PRIMARY KEY, source_type TEXT, identifier TEXT, display_name TEXT);
                CREATE TABLE participants (id INTEGER PRIMARY KEY, email_address TEXT, display_name TEXT, domain TEXT);
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY,
                    source_id INTEGER,
                    conversation_id INTEGER,
                    message_type TEXT,
                    sent_at TEXT,
                    received_at TEXT,
                    internal_date TEXT,
                    deleted_at TEXT,
                    deleted_from_source_at TEXT,
                    subject TEXT,
                    snippet TEXT
                );
                CREATE TABLE message_recipients (id INTEGER PRIMARY KEY, message_id INTEGER, participant_id INTEGER, recipient_type TEXT, display_name TEXT);
                INSERT INTO sources (id, source_type, identifier, display_name) VALUES (1, 'gmail', 'me@gmail.com', 'Me');
                INSERT INTO participants (id, email_address, display_name, domain) VALUES
                    (1, 'jane@example.com', 'Jane Participant', 'example.com'),
                    (2, 'me@gmail.com', 'Me', 'gmail.com'),
                    (3, 'noreply@example.com', 'No Reply', 'example.com');
                INSERT INTO messages (id, source_id, conversation_id, message_type, sent_at, subject, snippet) VALUES
                    (10, 1, 100, 'email', '2026-01-01T00:00:00Z', 'private subject', 'private snippet'),
                    (11, 1, 101, 'email', '2026-01-02T00:00:00Z', 'private subject 2', 'private snippet 2'),
                    (12, 1, 102, 'email', '2026-01-03T00:00:00Z', 'automated', 'automated');
                INSERT INTO message_recipients (message_id, participant_id, recipient_type, display_name) VALUES
                    (10, 1, 'from', 'Jane Example'),
                    (11, 1, 'to', 'Jane Example'),
                    (11, 2, 'from', 'Me'),
                    (12, 3, 'from', 'No Reply');
            """)
            con.commit()
            con.close()

            code, payload = self.invoke([
                "msgvault",
                "--db", str(db),
                "--account-email", "me@gmail.com",
                "--output-dir", str(Path(tmp) / "out"),
                "--run-id", "msgvault-test",
            ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "completed")
            self.assertFalse(payload["privacy"]["message_subjects_included"])
            artifacts = payload["artifacts"]
            targeted = Path(artifacts["targeted_emails_csv"])
            people = Path(artifacts["people_csv"])
            queue = Path(artifacts["linkedin_resolution_queue_csv"])
            with targeted.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["primary_email"], "jane@example.com")
            self.assertEqual(rows[0]["display_name"], "Jane Example")
            self.assertEqual(rows[0]["total_sent"], "1")
            self.assertEqual(rows[0]["total_received"], "1")
            self.assertEqual(payload["counts"]["one_way_filtered"], 0)
            self.assertNotIn("private subject", targeted.read_text(encoding="utf-8"))
            with people.open(newline="", encoding="utf-8") as handle:
                people_rows = list(csv.DictReader(handle))
            self.assertEqual(people_rows[0]["primary_email"], "jane@example.com")
            self.assertEqual(people_rows[0]["source_channels"], "gmail_msgvault")
            with queue.open(newline="", encoding="utf-8") as handle:
                queue_rows = list(csv.DictReader(handle))
            self.assertEqual(queue_rows[0]["handle"], "jane@example.com")
            self.assertEqual(queue_rows[0]["source"], "gmail_msgvault")

    def test_msgvault_import_requires_round_trip_contacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "msgvault.db"
            con = sqlite3.connect(db)
            con.executescript("""
                CREATE TABLE sources (id INTEGER PRIMARY KEY, source_type TEXT, identifier TEXT, display_name TEXT);
                CREATE TABLE participants (id INTEGER PRIMARY KEY, email_address TEXT, display_name TEXT, domain TEXT);
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY,
                    source_id INTEGER,
                    conversation_id INTEGER,
                    message_type TEXT,
                    sent_at TEXT,
                    received_at TEXT,
                    internal_date TEXT,
                    deleted_at TEXT,
                    deleted_from_source_at TEXT
                );
                CREATE TABLE message_recipients (id INTEGER PRIMARY KEY, message_id INTEGER, participant_id INTEGER, recipient_type TEXT, display_name TEXT);
                INSERT INTO sources (id, source_type, identifier, display_name) VALUES (1, 'gmail', 'me@gmail.com', 'Me');
                INSERT INTO participants (id, email_address, display_name, domain) VALUES
                    (1, 'jane@example.com', 'Jane Example', 'example.com'),
                    (2, 'sent-only@example.com', 'Sent Only', 'example.com'),
                    (3, 'recv-only@example.com', 'Received Only', 'example.com');
                INSERT INTO messages (id, source_id, conversation_id, message_type, sent_at) VALUES
                    (10, 1, 100, 'email', '2026-01-01T00:00:00Z'),
                    (11, 1, 101, 'email', '2026-01-02T00:00:00Z'),
                    (12, 1, 102, 'email', '2026-01-03T00:00:00Z'),
                    (13, 1, 103, 'email', '2026-01-04T00:00:00Z');
                INSERT INTO message_recipients (message_id, participant_id, recipient_type, display_name) VALUES
                    (10, 1, 'from', 'Jane Example'),
                    (11, 1, 'to', 'Jane Example'),
                    (12, 2, 'to', 'Sent Only'),
                    (13, 3, 'from', 'Received Only');
            """)
            con.commit()
            con.close()

            code, payload = self.invoke([
                "msgvault",
                "--db", str(db),
                "--account-email", "me@gmail.com",
                "--output-dir", str(Path(tmp) / "out"),
                "--run-id", "round-trip",
            ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["counts"]["contacts_seen"], 3)
            self.assertEqual(payload["counts"]["contacts_written"], 1)
            self.assertEqual(payload["counts"]["one_way_filtered"], 2)
            self.assertTrue(payload["counts"]["round_trip_required"])
            with Path(payload["artifacts"]["people_csv"]).open(newline="", encoding="utf-8") as handle:
                people_rows = list(csv.DictReader(handle))
            self.assertEqual([row["primary_email"] for row in people_rows], ["jane@example.com"])

    def test_msgvault_import_excludes_gmail_category_labels_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "msgvault.db"
            con = sqlite3.connect(db)
            con.executescript("""
                CREATE TABLE sources (id INTEGER PRIMARY KEY, source_type TEXT, identifier TEXT, display_name TEXT);
                CREATE TABLE participants (id INTEGER PRIMARY KEY, email_address TEXT, display_name TEXT, domain TEXT);
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY,
                    source_id INTEGER,
                    conversation_id INTEGER,
                    message_type TEXT,
                    sent_at TEXT,
                    received_at TEXT,
                    internal_date TEXT,
                    deleted_at TEXT,
                    deleted_from_source_at TEXT
                );
                CREATE TABLE message_recipients (id INTEGER PRIMARY KEY, message_id INTEGER, participant_id INTEGER, recipient_type TEXT, display_name TEXT);
                CREATE TABLE labels (id INTEGER PRIMARY KEY, name TEXT);
                CREATE TABLE message_labels (message_id INTEGER, label_id INTEGER);
                INSERT INTO sources (id, source_type, identifier, display_name) VALUES (1, 'gmail', 'me@gmail.com', 'Me');
                INSERT INTO participants (id, email_address, display_name, domain) VALUES
                    (1, 'jane@example.com', 'Jane Example', 'example.com'),
                    (2, 'promo.person@example.com', 'Promo Person', 'example.com');
                INSERT INTO messages (id, source_id, conversation_id, message_type, sent_at) VALUES
                    (10, 1, 100, 'email', '2026-01-01T00:00:00Z'),
                    (11, 1, 101, 'email', '2026-01-02T00:00:00Z'),
                    (12, 1, 102, 'email', '2026-01-03T00:00:00Z'),
                    (13, 1, 103, 'email', '2026-01-04T00:00:00Z');
                INSERT INTO message_recipients (message_id, participant_id, recipient_type, display_name) VALUES
                    (10, 1, 'from', 'Jane Example'),
                    (11, 1, 'to', 'Jane Example'),
                    (12, 2, 'from', 'Promo Person'),
                    (13, 2, 'to', 'Promo Person');
                INSERT INTO labels (id, name) VALUES (1, 'CATEGORY_PROMOTIONS');
                INSERT INTO message_labels (message_id, label_id) VALUES (12, 1), (13, 1);
            """)
            con.commit()
            con.close()

            code, payload = self.invoke([
                "msgvault",
                "--db", str(db),
                "--account-email", "me@gmail.com",
                "--output-dir", str(Path(tmp) / "out"),
                "--run-id", "filtered",
            ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["counts"]["excluded_labels"], ["CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS", "CATEGORY_FORUMS", "CATEGORY_UPDATES"])
            with Path(payload["artifacts"]["people_csv"]).open(newline="", encoding="utf-8") as handle:
                people_rows = list(csv.DictReader(handle))
            self.assertEqual([row["primary_email"] for row in people_rows], ["jane@example.com"])

            code, payload = self.invoke([
                "msgvault",
                "--db", str(db),
                "--account-email", "me@gmail.com",
                "--output-dir", str(Path(tmp) / "out"),
                "--run-id", "unfiltered",
                "--include-category-mail",
            ])
            self.assertEqual(code, 0)
            with Path(payload["artifacts"]["people_csv"]).open(newline="", encoding="utf-8") as handle:
                people_rows = list(csv.DictReader(handle))
            self.assertEqual(sorted(row["primary_email"] for row in people_rows), ["jane@example.com", "promo.person@example.com"])

    def test_apply_linkedin_resolutions_to_msgvault_people(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            resolutions = Path(tmp) / "linkedin_resolutions.csv"
            with people.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=gmail_network_import.PEOPLE_COLUMNS)
                writer.writeheader()
                row = {col: "" for col in gmail_network_import.PEOPLE_COLUMNS}
                row.update({
                    "id": "gmail:abc",
                    "full_name": "Jane Example",
                    "primary_email": "jane@example.com",
                    "all_emails": json.dumps(["jane@example.com"]),
                    "source_channels": "gmail_msgvault",
                    "source_artifacts": json.dumps(["gmail/people.csv"]),
                })
                writer.writerow(row)
            with resolutions.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=gmail_network_import.LINKEDIN_RESOLUTION_COLUMNS)
                writer.writeheader()
                writer.writerow({
                    "handle": "jane@example.com",
                    "status": "found",
                    "linkedin_url": "https://www.linkedin.com/in/jane-example?trk=test",
                    "confidence": "0.92",
                    "matched_name": "Jane Example",
                    "matched_headline": "Founder at Example",
                    "evidence": "[]",
                    "reasoning": "fixture",
                })
            code, payload = self.invoke([
                "apply-resolutions",
                "--people-csv", str(people),
                "--resolutions-csv", str(resolutions),
                "--output-dir", str(Path(tmp) / "out"),
                "--run-id", "resolved-test",
            ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["resolved"], 1)
            with Path(payload["people_csv"]).open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["public_identifier"], "jane-example")
            self.assertEqual(rows[0]["linkedin_url"], "https://www.linkedin.com/in/jane-example")
            self.assertEqual(rows[0]["headline"], "Founder at Example")
            self.assertEqual(rows[0]["id"], str(uuid.uuid5(uuid.NAMESPACE_URL, "linkedin:jane-example")))

    def test_msgvault_accounts_lists_local_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "msgvault.db"
            con = sqlite3.connect(db)
            con.executescript("""
                CREATE TABLE sources (id INTEGER PRIMARY KEY, source_type TEXT, identifier TEXT, display_name TEXT);
                CREATE TABLE participants (id INTEGER PRIMARY KEY, email_address TEXT, display_name TEXT, domain TEXT);
                CREATE TABLE messages (id INTEGER PRIMARY KEY, source_id INTEGER, message_type TEXT, deleted_at TEXT, deleted_from_source_at TEXT);
                CREATE TABLE message_recipients (id INTEGER PRIMARY KEY, message_id INTEGER, participant_id INTEGER, recipient_type TEXT, display_name TEXT);
                INSERT INTO sources (id, source_type, identifier, display_name) VALUES
                    (1, 'gmail', 'me@gmail.com', 'Me'),
                    (2, 'gmail', 'work@example.com', 'Work');
                INSERT INTO messages (id, source_id, message_type) VALUES (10, 1, 'email'), (11, 1, 'email'), (12, 2, 'email');
            """)
            con.commit()
            con.close()
            code, payload = self.invoke(["msgvault-accounts", "--db", str(db)])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual([row["account_email"] for row in payload["accounts"]], ["me@gmail.com", "work@example.com"])
            self.assertEqual([row["message_count"] for row in payload["accounts"]], [2, 1])

    def test_powerset_gmail_oauth_commands_are_not_exposed(self):
        parser = gmail_network_import.build_parser()
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["connect", "--no-open"])
            with self.assertRaises(SystemExit):
                parser.parse_args(["accounts"])


if __name__ == "__main__":
    unittest.main()
