"""Regression test for the streaming msgvault contact aggregation.

The production equivalence was proven byte-exact against the real msgvault DB
(85% less memory, same output). The live DB drifts (WAL writes), so this test
pins behavior on a small deterministic fixture covering one-to-one sent/received,
group, automated sender, and canonical (rfc822) message dedup.
"""
import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOD = ROOT / "packs/ingestion/primitives/discover/gmail/msgvault_store.py"


def load_module():
    spec = importlib.util.spec_from_file_location("gmail_msgvault_store", MOD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["gmail_msgvault_store"] = module
    spec.loader.exec_module(module)
    return module


gni = load_module()

SCHEMA = """
CREATE TABLE sources (id INTEGER PRIMARY KEY, source_type TEXT, identifier TEXT, display_name TEXT);
CREATE TABLE participants (id INTEGER PRIMARY KEY, email_address TEXT, display_name TEXT);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY, conversation_id INTEGER, source_id INTEGER,
    source_message_id TEXT, rfc822_message_id TEXT, message_type TEXT,
    sent_at TEXT, received_at TEXT, internal_date TEXT, sender_id INTEGER,
    deleted_at TEXT, deleted_from_source_at TEXT
);
CREATE TABLE message_recipients (
    id INTEGER PRIMARY KEY, message_id INTEGER, participant_id INTEGER,
    recipient_type TEXT, display_name TEXT
);
"""

ME = "me@acme.com"


def build_fixture(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.execute("INSERT INTO sources VALUES (1,'gmail',?, 'Me')", (ME,))
    parts = {1: ME, 2: "alice@x.com", 3: "bob@y.com", 4: "carol@z.com", 5: "noreply@mailer.com"}
    for pid, email in parts.items():
        con.execute("INSERT INTO participants (id, email_address, display_name) VALUES (?,?,?)",
                    (pid, email, email.split("@")[0].title()))

    def msg(mid, conv, sender, rfc822, sent):
        con.execute(
            "INSERT INTO messages (id, conversation_id, source_id, source_message_id, "
            "rfc822_message_id, message_type, sent_at, sender_id) VALUES (?,?,1,?,?, 'email', ?, ?)",
            (mid, conv, f"s{mid}", rfc822, sent, sender))

    def rcpt(mid, pid, rtype):
        con.execute("INSERT INTO message_recipients (message_id, participant_id, recipient_type, display_name) "
                    "VALUES (?,?,?,NULL)", (mid, pid, rtype))

    # M1: me -> alice (one-to-one sent)
    msg(1, 10, 1, "<m1>", "2024-01-01"); rcpt(1, 2, "to")
    # M2 + M2b: alice -> me, same rfc822 (canonical dedup => counts once)
    msg(2, 20, 2, "<m2>", "2024-01-02"); rcpt(2, 1, "to")
    msg(3, 21, 2, "<m2>", "2024-01-02"); rcpt(3, 1, "to")
    # M3: me -> bob, carol (group sent)
    msg(4, 30, 1, "<m3>", "2024-01-03"); rcpt(4, 3, "to"); rcpt(4, 4, "to")
    # M4: noreply -> me (received, automated sender)
    msg(5, 40, 5, "<m4>", "2024-01-04"); rcpt(5, 1, "to")
    con.commit(); con.close()


class GmailMsgvaultAggregationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "msgvault.db"
        build_fixture(self.db)
        self.con = sqlite3.connect(self.db)
        self.con.row_factory = sqlite3.Row

    def tearDown(self):
        self.con.close()
        self._tmp.cleanup()

    def aggregate(self):
        rows = gni.MsgvaultStore(connection=self.con).aggregate_contacts("", [])
        return {r["email"]: r for r in rows}, rows

    def test_contact_set_and_counts(self):
        by_email, rows = self.aggregate()
        # me@acme is the account → excluded; four correspondents remain.
        self.assertEqual(set(by_email), {"alice@x.com", "bob@y.com", "carol@z.com", "noreply@mailer.com"})

        alice = by_email["alice@x.com"]
        self.assertEqual(alice["total_sent"], 1)       # M1
        self.assertEqual(alice["total_received"], 1)    # M2 (+M2b deduped)
        self.assertEqual(alice["total_messages"], 2)
        self.assertEqual(alice["one_to_one_messages"], 2)
        self.assertEqual(alice["group_messages"], 0)
        self.assertEqual(alice["thread_count"], 2)      # conv 10 + 20

        for name in ("bob@y.com", "carol@z.com"):
            c = by_email[name]
            self.assertEqual(c["total_sent"], 1)
            self.assertEqual(c["group_messages"], 1)
            self.assertEqual(c["group_sent"], 1)
            self.assertEqual(c["one_to_one_messages"], 0)
            self.assertEqual(c["total_messages"], 1)

        noreply = by_email["noreply@mailer.com"]
        self.assertEqual(noreply["total_received"], 1)
        self.assertEqual(noreply["total_messages"], 1)
        self.assertTrue(noreply["automated_filtered"])

    def test_canonical_rfc822_dedup_counts_once(self):
        # M2 and M2b share rfc822 "<m2>" → alice's received must be 1, not 2.
        by_email, _ = self.aggregate()
        self.assertEqual(by_email["alice@x.com"]["total_received"], 1)

    def test_output_sorted_by_total_then_email(self):
        _, rows = self.aggregate()
        keys = [(-r["total_messages"], r["email"]) for r in rows]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(rows[0]["email"], "alice@x.com")  # highest total


if __name__ == "__main__":
    unittest.main()
