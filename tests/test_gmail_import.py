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

from packs.ingestion.schemas.people_schema import generate_person_id
from packs.shared.csv_io import CsvIO

MODULE_PATH = Path(__file__).resolve().parents[1] / "packs/ingestion/primitives/discover_contacts_pipeline/gmail/discover_engine.py"
spec = importlib.util.spec_from_file_location("gmail_import", MODULE_PATH)
gmail_import = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = gmail_import
spec.loader.exec_module(gmail_import)

STORE_PATH = Path(__file__).resolve().parents[1] / "packs/ingestion/primitives/discover_contacts_pipeline/gmail/msgvault_store.py"
store_spec = importlib.util.spec_from_file_location("gmail_msgvault_store", STORE_PATH)
msgvault_store = importlib.util.module_from_spec(store_spec)
assert store_spec.loader is not None
sys.modules[store_spec.name] = msgvault_store
store_spec.loader.exec_module(msgvault_store)


class GmailDiscoverEngineTests(unittest.TestCase):
    def invoke(self, argv):
        buf = StringIO()
        with redirect_stdout(buf):
            code = gmail_import.main(argv)
        output = buf.getvalue().strip()
        payload = json.loads(output) if output else {}
        return code, payload

    def test_parse_email_header_port(self):
        parsed = msgvault_store.parse_email_header('"Jane Example" <jane@example.com>, john@example.org')
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
            ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "completed")
            self.assertFalse(payload["privacy"]["message_subjects_included"])
            artifacts = payload["artifacts"]
            targeted = Path(artifacts["targeted_emails_csv"])
            people = Path(artifacts["people_csv"])
            queue = Path(artifacts["linkedin_resolution_queue_csv"])
            expected_dir = Path(tmp) / "out" / "discover" / "gmail" / "me-gmail.com"
            self.assertEqual(Path(payload["artifact_dir"]), expected_dir)
            self.assertEqual(targeted, expected_dir / "targeted_emails.csv")
            self.assertEqual(people, expected_dir / "people.csv")
            self.assertEqual(queue, expected_dir / "linkedin_resolution_queue.csv")
            self.assertNotIn("msgvault-test", str(targeted))
            with targeted.open(newline="", encoding="utf-8") as handle:
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["primary_email"], "jane@example.com")
            self.assertEqual(rows[0]["display_name"], "Jane Example")
            self.assertEqual(rows[0]["total_sent"], "1")
            self.assertEqual(rows[0]["total_received"], "1")
            self.assertEqual(payload["counts"]["one_way_filtered"], 0)
            self.assertNotIn("private subject", targeted.read_text(encoding="utf-8"))
            with people.open(newline="", encoding="utf-8") as handle:
                people_rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(people_rows[0]["primary_email"], "jane@example.com")
            self.assertEqual(people_rows[0]["source_channels"], "gmail_msgvault")
            with queue.open(newline="", encoding="utf-8") as handle:
                queue_rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(queue_rows[0]["handle"], "jane@example.com")
            self.assertEqual(queue_rows[0]["source"], "gmail_msgvault")

    def test_msgvault_dedupes_rfc822_copies_across_conversations(self):
        """The same RFC822 message stored under multiple msgvault
        conversation_ids/rows must count once, not once per copy.
        Repro shape: contact counts inflated (e.g. 23 raw rows vs 13 real
        messages) because msgvault keeps thread copies of identical emails."""
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript("""
            CREATE TABLE sources (id INTEGER PRIMARY KEY, source_type TEXT, identifier TEXT, display_name TEXT);
            CREATE TABLE participants (id INTEGER PRIMARY KEY, email_address TEXT, display_name TEXT, domain TEXT);
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                source_id INTEGER,
                conversation_id INTEGER,
                rfc822_message_id TEXT,
                source_message_id TEXT,
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
                (1, 'me@gmail.com', 'Me', 'gmail.com'),
                (2, 'pat@example.com', 'Pat', 'example.com');
            -- Same RFC822 message duplicated under two conversation_ids (100, 200)
            INSERT INTO messages (id, source_id, conversation_id, rfc822_message_id, message_type, sent_at) VALUES
                (10, 1, 100, '<intro-1@mail.example>', 'email', '2026-01-01T00:00:00Z'),
                (11, 1, 200, '<intro-1@mail.example>', 'email', '2026-01-01T00:00:00Z');
            -- A second real message, only in conversation 100
            INSERT INTO messages (id, source_id, conversation_id, rfc822_message_id, message_type, sent_at) VALUES
                (12, 1, 100, '<intro-2@mail.example>', 'email', '2026-01-02T00:00:00Z');
            -- Dup copy without rfc822 id but with the same source_message_id
            INSERT INTO messages (id, source_id, conversation_id, rfc822_message_id, source_message_id, message_type, sent_at) VALUES
                (13, 1, 100, NULL, 'gmail-msg-3', 'email', '2026-01-03T00:00:00Z'),
                (14, 1, 300, NULL, 'gmail-msg-3', 'email', '2026-01-03T00:00:00Z');
            INSERT INTO message_recipients (message_id, participant_id, recipient_type, display_name) VALUES
                (10, 2, 'from', 'Pat'), (10, 1, 'to', 'Me'),
                (11, 2, 'from', 'Pat'), (11, 1, 'to', 'Me'),
                (12, 1, 'from', 'Me'), (12, 2, 'to', 'Pat'),
                (13, 2, 'from', 'Pat'), (13, 1, 'to', 'Me'),
                (14, 2, 'from', 'Pat'), (14, 1, 'to', 'Me');
        """)

        rows = msgvault_store.aggregate_msgvault_contacts(con, "me@gmail.com")
        by_email = {row["email"]: row for row in rows}
        pat = by_email["pat@example.com"]

        # 5 msgvault rows but only 3 real messages (2 received + 1 sent)
        self.assertEqual(pat["total_messages"], 3)
        self.assertEqual(pat["total_received"], 2)
        self.assertEqual(pat["total_sent"], 1)
        con.close()

    def test_msgvault_group_email_counts_direction_at_message_level(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
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
                (1, 'me@gmail.com', 'Me', 'gmail.com'),
                (2, 'alice@example.com', 'Alice', 'example.com'),
                (3, 'bob@example.com', 'Bob', 'example.com'),
                (4, 'carol@example.com', 'Carol', 'example.com');
            INSERT INTO messages (id, source_id, conversation_id, message_type, sent_at) VALUES
                (10, 1, 100, 'email', '2026-01-01T00:00:00Z'),
                (11, 1, 100, 'email', '2026-01-02T00:00:00Z');
            INSERT INTO message_recipients (message_id, participant_id, recipient_type, display_name) VALUES
                (10, 2, 'from', 'Alice'),
                (10, 1, 'to', 'Me'),
                (10, 3, 'to', 'Bob'),
                (10, 4, 'cc', 'Carol'),
                (11, 1, 'from', 'Me'),
                (11, 2, 'to', 'Alice'),
                (11, 3, 'to', 'Bob'),
                (11, 4, 'cc', 'Carol');
        """)

        rows = msgvault_store.aggregate_msgvault_contacts(con, "me@gmail.com")
        by_email = {row["email"]: row for row in rows}

        self.assertEqual(by_email["alice@example.com"]["total_sent"], 1)
        self.assertEqual(by_email["alice@example.com"]["total_received"], 1)
        self.assertEqual(by_email["alice@example.com"]["total_messages"], 2)
        self.assertEqual(by_email["alice@example.com"]["group_sent"], 1)
        self.assertEqual(by_email["alice@example.com"]["group_received"], 1)
        self.assertEqual(by_email["alice@example.com"]["group_messages"], 2)

        self.assertEqual(by_email["bob@example.com"]["total_sent"], 1)
        self.assertEqual(by_email["bob@example.com"]["total_received"], 0)
        self.assertEqual(by_email["bob@example.com"]["total_messages"], 1)
        self.assertEqual(by_email["bob@example.com"]["group_sent"], 1)
        self.assertEqual(by_email["bob@example.com"]["group_received"], 0)

        self.assertEqual(by_email["carol@example.com"]["total_sent"], 1)
        self.assertEqual(by_email["carol@example.com"]["total_received"], 0)
        self.assertEqual(by_email["carol@example.com"]["total_messages"], 1)
        self.assertEqual(by_email["carol@example.com"]["group_sent"], 1)
        self.assertEqual(by_email["carol@example.com"]["group_received"], 0)
        con.close()

    def test_msgvault_import_reruns_upsert_fixed_discover_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "msgvault.db"

            def write_db(contacts):
                if db.exists():
                    db.unlink()
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
                """)
                participant_id = 1
                message_id = 10
                for email, name in contacts:
                    con.execute(
                        "INSERT INTO participants (id, email_address, display_name, domain) VALUES (?, ?, ?, ?)",
                        (participant_id, email, name, email.rsplit("@", 1)[1]),
                    )
                    con.execute(
                        "INSERT INTO messages (id, source_id, conversation_id, message_type, sent_at) VALUES (?, 1, ?, 'email', ?)",
                        (message_id, message_id, f"2026-01-{participant_id:02d}T00:00:00Z"),
                    )
                    con.execute(
                        "INSERT INTO message_recipients (message_id, participant_id, recipient_type, display_name) VALUES (?, ?, 'from', ?)",
                        (message_id, participant_id, name),
                    )
                    message_id += 1
                    con.execute(
                        "INSERT INTO messages (id, source_id, conversation_id, message_type, sent_at) VALUES (?, 1, ?, 'email', ?)",
                        (message_id, message_id, f"2026-01-{participant_id:02d}T01:00:00Z"),
                    )
                    con.execute(
                        "INSERT INTO message_recipients (message_id, participant_id, recipient_type, display_name) VALUES (?, ?, 'to', ?)",
                        (message_id, participant_id, name),
                    )
                    participant_id += 1
                    message_id += 1
                con.commit()
                con.close()

            out_dir = Path(tmp) / "out"
            expected_dir = out_dir / "discover" / "gmail" / "me-gmail.com"
            write_db([("jane@example.com", "Jane Example")])
            code, first = self.invoke([
                "msgvault",
                "--db", str(db),
                "--account-email", "me@gmail.com",
                "--output-dir", str(out_dir),
            ])
            self.assertEqual(code, 0)
            self.assertEqual(Path(first["artifact_dir"]), expected_dir)

            write_db([("john@example.com", "John Example")])
            code, second = self.invoke([
                "msgvault",
                "--db", str(db),
                "--account-email", "me@gmail.com",
                "--output-dir", str(out_dir),
            ])
            self.assertEqual(code, 0)
            self.assertEqual(Path(second["artifact_dir"]), expected_dir)
            self.assertEqual(first["artifacts"]["people_csv"], second["artifacts"]["people_csv"])
            self.assertNotIn("first-run", second["artifacts"]["people_csv"])
            self.assertNotIn("second-run", second["artifacts"]["people_csv"])
            self.assertEqual(second["counts"]["contacts_written"], 1)
            self.assertEqual(second["counts"]["contacts_final"], 2)
            self.assertEqual(second["counts"]["contacts_preserved_existing"], 1)

            with Path(second["artifacts"]["people_csv"]).open(newline="", encoding="utf-8") as handle:
                people_rows = list(CsvIO.dict_reader(handle))
            self.assertEqual([row["primary_email"] for row in people_rows], ["jane@example.com", "john@example.com"])
            with Path(second["artifacts"]["targeted_emails_csv"]).open(newline="", encoding="utf-8") as handle:
                targeted_rows = list(CsvIO.dict_reader(handle))
            self.assertEqual([row["primary_email"] for row in targeted_rows], ["jane@example.com", "john@example.com"])

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
            ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["counts"]["contacts_seen"], 3)
            self.assertEqual(payload["counts"]["contacts_written"], 1)
            self.assertEqual(payload["counts"]["one_way_filtered"], 2)
            self.assertTrue(payload["counts"]["round_trip_required"])
            with Path(payload["artifacts"]["people_csv"]).open(newline="", encoding="utf-8") as handle:
                people_rows = list(CsvIO.dict_reader(handle))
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
                INSERT INTO labels (id, name) VALUES (1, 'CATEGORY_PROMOTIONS'), (2, 'SENT');
                INSERT INTO message_labels (message_id, label_id) VALUES (11, 2), (12, 1), (13, 1), (13, 2);
            """)
            con.commit()
            con.close()

            code, payload = self.invoke([
                "msgvault",
                "--db", str(db),
                "--account-email", "me@gmail.com",
                "--output-dir", str(Path(tmp) / "out"),
            ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["counts"]["excluded_labels"], ["CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS", "CATEGORY_FORUMS", "CATEGORY_UPDATES"])
            with Path(payload["artifacts"]["people_csv"]).open(newline="", encoding="utf-8") as handle:
                people_rows = list(CsvIO.dict_reader(handle))
            self.assertEqual([row["primary_email"] for row in people_rows], ["jane@example.com"])

            code, payload = self.invoke([
                "msgvault",
                "--db", str(db),
                "--account-email", "me@gmail.com",
                "--output-dir", str(Path(tmp) / "out"),
                "--include-category-mail",
            ])
            self.assertEqual(code, 0)
            with Path(payload["artifacts"]["people_csv"]).open(newline="", encoding="utf-8") as handle:
                people_rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(sorted(row["primary_email"] for row in people_rows), ["jane@example.com", "promo.person@example.com"])

    def test_apply_linkedin_resolutions_to_msgvault_people(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = Path(tmp) / "people.csv"
            resolutions = Path(tmp) / "linkedin_resolutions.csv"
            with people.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=gmail_import.PEOPLE_COLUMNS)
                writer.writeheader()
                row = {col: "" for col in gmail_import.PEOPLE_COLUMNS}
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
                writer = csv.DictWriter(handle, fieldnames=gmail_import.LINKEDIN_RESOLUTION_COLUMNS)
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
            ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["resolved"], 1)
            with Path(payload["people_csv"]).open(newline="", encoding="utf-8") as handle:
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(rows[0]["public_identifier"], "jane-example")
            self.assertEqual(rows[0]["linkedin_url"], "https://www.linkedin.com/in/jane-example")
            self.assertEqual(rows[0]["headline"], "Founder at Example")
            self.assertEqual(rows[0]["id"], generate_person_id("jane-example"))

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
        parser = gmail_import.build_parser()
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["connect", "--no-open"])
            with self.assertRaises(SystemExit):
                parser.parse_args(["accounts"])


if __name__ == "__main__":
    unittest.main()
