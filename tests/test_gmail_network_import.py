import csv
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
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
            with targeted.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["primary_email"], "jane@example.com")
            self.assertEqual(rows[0]["display_name"], "Jane Example")
            self.assertEqual(rows[0]["total_sent"], "1")
            self.assertEqual(rows[0]["total_received"], "1")
            self.assertNotIn("private subject", targeted.read_text(encoding="utf-8"))
            with people.open(newline="", encoding="utf-8") as handle:
                people_rows = list(csv.DictReader(handle))
            self.assertEqual(people_rows[0]["primary_email"], "jane@example.com")
            self.assertEqual(people_rows[0]["source_channels"], "gmail_msgvault")

    def test_powerset_gmail_oauth_commands_are_not_exposed(self):
        parser = gmail_network_import.build_parser()
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["connect", "--no-open"])
            with self.assertRaises(SystemExit):
                parser.parse_args(["accounts"])


if __name__ == "__main__":
    unittest.main()
