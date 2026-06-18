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
        -- Thread 100: 10 Jane->me + 11 me->Jane (SAME thread -> dedupe to one, prefer Jane's).
        -- Thread 200: 20 Jane->me (contact).  Thread 300: 30 me->Jane (mine).
        -- 13 Bob->Jane+me group (third party; Jane a co-recipient -> dropped).
        INSERT INTO messages (id, source_id, conversation_id, message_type, sent_at, sender_id, subject, snippet) VALUES
            (10, 1, 100, 'email', '2026-01-01T00:00:00Z', 1, 'Hello &amp; welcome', 'It&#39;s   great   to   meet'),
            (11, 1, 100, 'email', '2026-01-02T00:00:00Z', 2, 'Re: Hello', 'Thanks &quot;Jane&quot;'),
            (13, 1, 102, 'email', '2026-01-04T00:00:00Z', 3, 'Bob announces a thing', 'I work at Acme as a security analyst'),
            (20, 1, 200, 'email', '2026-01-05T00:00:00Z', 1, 'My new role', 'Joined Acme as Staff Engineer'),
            (30, 1, 300, 'email', '2026-01-06T00:00:00Z', 2, 'Intro to you', 'Meet my friend');
        INSERT INTO message_recipients (message_id, participant_id, recipient_type, display_name) VALUES
            (10, 2, 'to', 'Me'),
            (11, 1, 'to', 'Jane Example'),
            (13, 1, 'to', 'Jane Example'),
            (13, 2, 'cc', 'Me'),
            (20, 2, 'to', 'Me'),
            (30, 1, 'to', 'Jane Example');
        INSERT INTO message_bodies (message_id, body_text) VALUES
            (10, 'Hi Arthur, I am a product designer at Acme.' || char(10) ||
                 'Best, Jane' || char(10) || 'Product Designer, Acme' || char(10) || '+1 555-1234' || char(10) ||
                 'On Dec 31, 2025, Arthur Chen wrote:' || char(10) || '> quoted history that must be cut' || char(10) || '> more quoted'),
            (20, 'STARTMARK ' || replace(hex(zeroblob(150)),'0','x') || ' ENDMARK');
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

    def _jane(self, **kw):
        return bec.recent_emails_for(self.con, "jane@example.com", accounts=self.accounts, **kw)

    def test_one_email_per_thread(self):
        rows, _ = self._jane(per_person=5, snippet_chars=100)
        subjects = [r["subject"] for r in rows]
        # 3 distinct threads (100/200/300); thread 100's 'Re: Hello' is deduped out.
        self.assertEqual(set(subjects), {"My new role", "Hello & welcome", "Intro to you"})
        self.assertNotIn("Re: Hello", subjects)

    def test_thread_rep_prefers_contact(self):
        rows, _ = self._jane(per_person=5, snippet_chars=100)
        by_subject = {r["subject"]: r["from_role"] for r in rows}
        # thread 100 had both Jane's (10) and mine (11); rep is Jane's own email.
        self.assertEqual(by_subject["Hello & welcome"], "contact")

    def test_contact_threads_surface_first(self):
        rows, _ = self._jane(per_person=5, snippet_chars=100)
        roles = [r["from_role"] for r in rows]
        self.assertEqual(roles, ["contact", "contact", "me"])  # contact-sent threads before mine

    def test_third_party_sender_dropped(self):
        rows, dropped = self._jane(per_person=5, snippet_chars=100)
        self.assertNotIn("Bob announces a thing", [r["subject"] for r in rows])
        self.assertEqual(dropped, 1)

    def test_html_entities_unescaped(self):
        rows, _ = self._jane(per_person=5, snippet_chars=100)
        snippets = [r["snippet"] for r in rows]
        self.assertIn("It's great to meet", snippets)
        self.assertNotIn('Thanks "Jane"', snippets)  # msg 11 deduped out of thread 100

    def test_per_person_cap(self):
        rows, _ = self._jane(per_person=1, snippet_chars=100)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subject"], "My new role")  # newest contact-sent thread

    def test_snippet_truncation(self):
        rows, _ = self._jane(per_person=5, snippet_chars=10)
        self.assertTrue(rows)
        for r in rows:
            self.assertLessEqual(len(r["snippet"]), 10)

    def test_body_mode_strips_quotes_keeps_signature(self):
        rows, _ = self._jane(per_person=5, snippet_chars=200, source="body", head_chars=200, tail_chars=200)
        body = {r["subject"]: r["snippet"] for r in rows}["Hello & welcome"]
        self.assertIn("product designer at Acme", body)
        self.assertIn("+1 555-1234", body)               # signature/footer kept
        self.assertNotIn("quoted history", body)         # quoted reply chain cut
        self.assertNotIn(">", body)

    def test_body_mode_head_tail_truncation(self):
        rows, _ = self._jane(per_person=5, snippet_chars=200, source="body", head_chars=15, tail_chars=12)
        body = {r["subject"]: r["snippet"] for r in rows}["My new role"]
        self.assertTrue(body.startswith("STARTMARK"))
        self.assertTrue(body.endswith("ENDMARK"))
        self.assertIn(" … ", body)                        # middle elided


if __name__ == "__main__":
    unittest.main()
