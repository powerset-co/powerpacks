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
            (10, 'Hi Test, I am a product designer at Acme.' || char(10) ||
                 'Best, Jane' || char(10) || 'Product Designer, Acme' || char(10) || '+1 555-1234' || char(10) ||
                 'On Dec 31, 2025, Test Contact wrote:' || char(10) || '> quoted history that must be cut' || char(10) || '> more quoted'),
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


class SignalScoreTests(unittest.TestCase):
    def test_signature_outscores_one_liner(self):
        sig = "Nadine Choe, Founder at Metagloss. +1 310-779-0107. metagloss.io"
        self.assertGreater(bec.signal_score(sig), bec.signal_score("Thanks, sounds good!"))

    def test_rewards_phone_title_license(self):
        self.assertGreaterEqual(bec.signal_score("Realtor, Compass — DRE #01972930, 310-425-9847"), 6)
        self.assertEqual(bec.signal_score("ok"), 0)


class HighestSignalSelectionTests(unittest.TestCase):
    def test_title_bearing_email_ranks_first(self):
        con = make_con()
        self.addCleanup(con.close)
        rows, _ = bec.recent_emails_for(con, "jane@example.com", per_person=5, snippet_chars=100,
                                        accounts=bec.account_emails(con))
        # 'My new role' snippet ("…Staff Engineer") carries a title -> highest signal.
        self.assertEqual(rows[0]["subject"], "My new role")


class NearDupTests(unittest.TestCase):
    def test_jaccard_identical_and_disjoint(self):
        a = bec.shingles("product designer at Acme Corp in San Francisco")
        self.assertEqual(bec.jaccard(a, a), 1.0)
        self.assertEqual(bec.jaccard(bec.shingles("alpha beta gamma delta"),
                                     bec.shingles("one two three four")), 0.0)

    def test_near_dup_emails_collapsed(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(SCHEMA)
        con.executescript("""
            INSERT INTO sources (id, source_type, identifier) VALUES (1, 'gmail', 'me@gmail.com');
            INSERT INTO participants (id, email_address) VALUES (1, 'jane@example.com'), (2, 'me@gmail.com');
            INSERT INTO messages (id, source_id, conversation_id, message_type, sent_at, sender_id, subject, snippet) VALUES
                (1, 1, 500, 'email', '2026-02-01T00:00:00Z', 1, 'Chat A', 'catching up about the weekend plans and dinner soon'),
                (2, 1, 501, 'email', '2026-02-02T00:00:00Z', 1, 'Chat B', 'catching up about the weekend plans and dinner soon'),
                (3, 1, 502, 'email', '2026-02-03T00:00:00Z', 1, 'Bio', 'Founder at Metagloss, phone 310-555-0000, decade in private equity');
            INSERT INTO message_recipients (message_id, participant_id, recipient_type) VALUES (1, 2, 'to'), (2, 2, 'to'), (3, 2, 'to');
        """)
        con.commit()
        self.addCleanup(con.close)
        rows, _ = bec.recent_emails_for(con, "jane@example.com", per_person=5, snippet_chars=200, accounts={"me@gmail.com"})
        subjects = [r["subject"] for r in rows]
        self.assertEqual(len(rows), 2)            # 3 distinct threads -> 1 near-dup collapsed
        self.assertIn("Bio", subjects)            # the distinct, high-signal email kept
        self.assertTrue(("Chat A" in subjects) ^ ("Chat B" in subjects))  # exactly one of the dup pair


class AccountEmailsTests(unittest.TestCase):
    def test_lowercases_identifiers(self):
        con = make_con()
        self.addCleanup(con.close)
        self.assertEqual(bec.account_emails(con), {"me@gmail.com"})


class OwnerIdentityTests(unittest.TestCase):
    def test_derives_name_and_emails_from_msgvault(self):
        con = make_con()
        self.addCleanup(con.close)
        owner = bec.owner_identity(con)
        # emails come from sources (lowercased); name from the participant row.
        self.assertEqual(owner["emails"], ["me@gmail.com"])
        self.assertEqual(owner["name"], "Me")

    def test_blank_when_no_sources(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(SCHEMA)
        self.addCleanup(con.close)
        self.assertEqual(bec.owner_identity(con), {"name": "", "emails": []})


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


class DepthSelectionTests(unittest.TestCase):
    """max_per_thread: default 1 = today's one-rep-per-thread; None = keep the back-and-forth."""

    def setUp(self):
        self.con = make_con()
        self.addCleanup(self.con.close)
        self.accounts = bec.account_emails(self.con)

    def _jane(self, **kw):
        return bec.recent_emails_for(self.con, "jane@example.com", accounts=self.accounts, **kw)

    def test_depth_keeps_thread_back_and_forth(self):
        # Thread 100 has Jane's msg 10 AND my reply 11. Default keeps one; depth keeps both.
        default, _ = self._jane(per_person=10, snippet_chars=100)
        deep, _ = self._jane(per_person=10, snippet_chars=100, max_per_thread=None)
        self.assertEqual(len(default), 3)                       # one per thread (100/200/300)
        self.assertEqual(len(deep), 4)                          # thread 100 now contributes 10 AND 11
        self.assertNotIn("Re: Hello", [r["subject"] for r in default])
        self.assertIn("Re: Hello", [r["subject"] for r in deep])

    def test_breadth_before_depth(self):
        deep, _ = self._jane(per_person=10, snippet_chars=100, max_per_thread=None)
        subjects = [r["subject"] for r in deep]
        # every thread's leader appears before any thread's extra message
        self.assertEqual(set(subjects[:3]), {"My new role", "Hello & welcome", "Intro to you"})
        self.assertEqual(subjects[-1], "Re: Hello")             # the depth message comes last

    def test_budget_bounds_and_is_breadth_first(self):
        # Budget 2 with depth on: still 2 distinct thread leaders, NOT a thread's depth.
        deep, _ = self._jane(per_person=2, snippet_chars=100, max_per_thread=None)
        self.assertEqual(len(deep), 2)
        self.assertNotIn("Re: Hello", [r["subject"] for r in deep])

    def test_default_is_one_per_thread(self):
        a, _ = self._jane(per_person=10, snippet_chars=100)                       # omitted arg
        b, _ = self._jane(per_person=10, snippet_chars=100, max_per_thread=1)     # explicit
        self.assertEqual([r["subject"] for r in a], [r["subject"] for r in b])
        self.assertEqual(len(a), 3)

    def test_near_dup_collapses_even_with_depth(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(SCHEMA)
        con.executescript("""
            INSERT INTO sources (id, source_type, identifier) VALUES (1, 'gmail', 'me@gmail.com');
            INSERT INTO participants (id, email_address) VALUES (1, 'jane@example.com'), (2, 'me@gmail.com');
            -- All ONE thread (600): two near-identical Jane messages + one distinct high-signal.
            INSERT INTO messages (id, source_id, conversation_id, message_type, sent_at, sender_id, subject, snippet) VALUES
                (1, 1, 600, 'email', '2026-02-01T00:00:00Z', 1, 'A', 'catching up about the weekend plans and dinner soon'),
                (2, 1, 600, 'email', '2026-02-02T00:00:00Z', 1, 'B', 'catching up about the weekend plans and dinner soon'),
                (3, 1, 600, 'email', '2026-02-03T00:00:00Z', 1, 'C', 'Founder at Metagloss, phone 310-555-0000, decade in private equity');
            INSERT INTO message_recipients (message_id, participant_id, recipient_type) VALUES (1, 2, 'to'), (2, 2, 'to'), (3, 2, 'to');
        """)
        con.commit()
        self.addCleanup(con.close)
        deep, _ = bec.recent_emails_for(con, "jane@example.com", per_person=10, snippet_chars=200,
                                        accounts={"me@gmail.com"}, max_per_thread=None)
        # Depth keeps the thread's messages, but the two near-dups still collapse to one.
        self.assertEqual(len(deep), 2)

    def test_count_messages_for_excludes_third_party(self):
        n = bec.count_messages_for(self.con, "jane@example.com", self.accounts)
        # Jane-sent (10, 20) + owner->Jane (11, 30) = 4; Bob's third-party msg 13 excluded.
        self.assertEqual(n, 4)

    def test_count_messages_for_unknown_email_is_zero(self):
        self.assertEqual(bec.count_messages_for(self.con, "nobody@nowhere.com", self.accounts), 0)


class StreamContactGroupsTests(unittest.TestCase):
    """The all-contacts windowed/streamed path must match the per-contact path."""

    def setUp(self):
        self.con = make_con()
        self.addCleanup(self.con.close)
        self.accounts = bec.account_emails(self.con)

    def test_streamed_selection_matches_per_contact(self):
        emails = ["jane@example.com", "bob@example.com"]
        bec.create_candidate_pid_table(self.con, emails)
        fetch_limit = 5 * bec.FETCH_MULTIPLIER
        streamed = {}
        for cemail, rows in bec.stream_contact_groups(self.con, fetch_limit):
            kept, _ = bec.select_emails_from_rows(
                rows, cemail, per_person=5, snippet_chars=100, accounts=self.accounts
            )
            streamed[cemail] = [(e["subject"], e["from_role"]) for e in kept]
        # Jane via the per-contact path — must be identical.
        per_contact, _ = bec.recent_emails_for(
            self.con, "jane@example.com", per_person=5, snippet_chars=100, accounts=self.accounts
        )
        self.assertEqual(streamed["jane@example.com"],
                         [(e["subject"], e["from_role"]) for e in per_contact])
        self.assertEqual(set(s for s, _ in streamed["jane@example.com"]),
                         {"My new role", "Hello & welcome", "Intro to you"})

    def test_candidate_table_maps_only_known_emails(self):
        n = bec.create_candidate_pid_table(self.con, ["jane@example.com", "nobody@nowhere.com"])
        self.assertEqual(n, 1)  # only jane resolves to a participant id

    def test_body_mode_streamed(self):
        bec.create_candidate_pid_table(self.con, ["jane@example.com"])
        groups = dict(bec.stream_contact_groups(self.con, 5 * bec.FETCH_MULTIPLIER))
        kept, _ = bec.select_emails_from_rows(
            groups["jane@example.com"], "jane@example.com", per_person=5,
            snippet_chars=200, accounts=self.accounts, source="body", head_chars=200, tail_chars=200,
        )
        body = {e["subject"]: e["snippet"] for e in kept}["Hello & welcome"]
        self.assertIn("product designer at Acme", body)
        self.assertIn("+1 555-1234", body)
        self.assertNotIn("quoted history", body)


if __name__ == "__main__":
    unittest.main()
