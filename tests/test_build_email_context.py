import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "packs/ingestion/primitives/build_email_context/build_email_context.py"
spec = importlib.util.spec_from_file_location("build_email_context", MODULE_PATH)
build_email_context = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = build_email_context
spec.loader.exec_module(build_email_context)

bec = build_email_context

SCHEMA = """
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
    sender_id INTEGER,
    subject TEXT,
    snippet TEXT
);
CREATE TABLE message_recipients (id INTEGER PRIMARY KEY, message_id INTEGER, participant_id INTEGER, recipient_type TEXT, display_name TEXT);
CREATE TABLE message_bodies (id INTEGER PRIMARY KEY, message_id INTEGER, body_text TEXT, body_html TEXT);
"""


def make_con() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.executescript("""
        INSERT INTO sources (id, source_type, identifier, display_name) VALUES (1, 'gmail', 'Me@Gmail.com', 'Me');
        INSERT INTO participants (id, email_address, display_name, domain) VALUES
            (1, 'jane@example.com', 'Jane Example', 'example.com'),
            (2, 'me@gmail.com', 'Me', 'gmail.com'),
            (3, 'bob@example.com', 'Bob Other', 'example.com');
        -- 10: Jane -> me (her words).  11: me -> Jane (my words).  12: Bob -> me (no Jane).
        -- 13: Bob -> Jane+me group (third party; Jane only a co-recipient -> must be dropped).
        INSERT INTO messages (id, source_id, conversation_id, message_type, sent_at, sender_id, subject, snippet) VALUES
            (10, 1, 100, 'email', '2026-01-01T00:00:00Z', 1, 'Hello &amp; welcome', 'It&#39;s   great   to   meet'),
            (11, 1, 100, 'email', '2026-01-02T00:00:00Z', 2, 'Re: Hello', 'Thanks &quot;Jane&quot;'),
            (12, 1, 101, 'email', '2026-01-03T00:00:00Z', 3, 'Third', 'A long snippet body here that should be truncated hard'),
            (13, 1, 102, 'email', '2026-01-04T00:00:00Z', 3, 'Bob announces a thing', 'I work at Acme as a security analyst');
        INSERT INTO message_recipients (message_id, participant_id, recipient_type, display_name) VALUES
            (10, 2, 'to', 'Me'),
            (11, 1, 'to', 'Jane Example'),
            (12, 2, 'to', 'Me'),
            (13, 1, 'to', 'Jane Example'),
            (13, 2, 'cc', 'Me');
        INSERT INTO message_bodies (message_id, body_text) VALUES
            (10, 'Hi Arthur, I am a product designer at Acme.' || char(10) ||
                 'Best, Jane' || char(10) || 'Product Designer, Acme' || char(10) || '+1 555-1234' || char(10) ||
                 'On Dec 31, 2025, Arthur Chen wrote:' || char(10) || '> quoted history that must be cut' || char(10) || '> more quoted'),
            (11, 'STARTMARK ' || replace(hex(zeroblob(150)),'0','x') || ' ENDMARK');
    """)
    con.commit()
    return con


class CleanTextTests(unittest.TestCase):
    def test_unescapes_and_collapses_whitespace(self):
        self.assertEqual(bec.clean_text("It&#39;s   great\n\tto  meet"), "It's great to meet")

    def test_truncates_to_limit(self):
        self.assertEqual(bec.clean_text("abcdefghij", 4), "abcd")

    def test_handles_none(self):
        self.assertEqual(bec.clean_text(None), "")


class AccountEmailsTests(unittest.TestCase):
    def test_lowercases_identifiers(self):
        con = make_con()
        self.addCleanup(con.close)
        self.assertEqual(bec.account_emails(con), {"me@gmail.com"})


class RecentEmailsTests(unittest.TestCase):
    def setUp(self):
        self.con = make_con()
        self.addCleanup(self.con.close)
        self.accounts = bec.account_emails(self.con)

    def test_pulls_subjects_and_snippets_newest_first(self):
        rows, _ = bec.recent_emails_for(self.con, "jane@example.com", per_person=5, snippet_chars=100, accounts=self.accounts)
        # Jane sent 10 + was sent 11 (both conv 100). msg 12 has no Jane; msg 13 is
        # third-party-sent (Bob) so it is dropped despite Jane being a co-recipient.
        subjects = [r["subject"] for r in rows]
        self.assertEqual(subjects, ["Re: Hello", "Hello & welcome"])

    def test_third_party_sender_dropped(self):
        rows, dropped = bec.recent_emails_for(self.con, "jane@example.com", per_person=5, snippet_chars=100, accounts=self.accounts)
        subjects = [r["subject"] for r in rows]
        self.assertNotIn("Bob announces a thing", subjects)         # would mis-attribute "security analyst" to Jane
        self.assertEqual(dropped, 1)

    def test_html_entities_unescaped(self):
        rows, _ = bec.recent_emails_for(self.con, "jane@example.com", per_person=5, snippet_chars=100, accounts=self.accounts)
        snippets = [r["snippet"] for r in rows]
        self.assertIn('Thanks "Jane"', snippets)
        self.assertIn("It's great to meet", snippets)

    def test_from_role_derived_from_sender(self):
        rows, _ = bec.recent_emails_for(self.con, "jane@example.com", per_person=5, snippet_chars=100, accounts=self.accounts)
        by_subject = {r["subject"]: r["from_role"] for r in rows}
        self.assertEqual(by_subject["Re: Hello"], "me")            # sender me@gmail.com
        self.assertEqual(by_subject["Hello & welcome"], "contact") # sender jane@example.com

    def test_per_person_cap(self):
        rows, _ = bec.recent_emails_for(self.con, "jane@example.com", per_person=1, snippet_chars=100, accounts=self.accounts)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subject"], "Re: Hello")

    def test_snippet_truncation(self):
        rows, _ = bec.recent_emails_for(self.con, "jane@example.com", per_person=5, snippet_chars=10, accounts=self.accounts)
        self.assertTrue(rows)
        for r in rows:
            self.assertLessEqual(len(r["snippet"]), 10)

    def test_body_mode_strips_quotes_keeps_signature(self):
        rows, _ = bec.recent_emails_for(
            self.con, "jane@example.com", per_person=5, snippet_chars=200,
            accounts=self.accounts, source="body", head_chars=200, tail_chars=200,
        )
        body = {r["subject"]: r["snippet"] for r in rows}["Hello & welcome"]
        self.assertIn("product designer at Acme", body)
        self.assertIn("+1 555-1234", body)               # signature/footer kept
        self.assertNotIn("quoted history", body)         # quoted reply chain cut
        self.assertNotIn(">", body)

    def test_body_mode_head_tail_truncation(self):
        rows, _ = bec.recent_emails_for(
            self.con, "jane@example.com", per_person=5, snippet_chars=200,
            accounts=self.accounts, source="body", head_chars=15, tail_chars=12,
        )
        body = {r["subject"]: r["snippet"] for r in rows}["Re: Hello"]
        self.assertTrue(body.startswith("STARTMARK"))
        self.assertTrue(body.endswith("ENDMARK"))
        self.assertIn(" … ", body)                        # middle elided


if __name__ == "__main__":
    unittest.main()
