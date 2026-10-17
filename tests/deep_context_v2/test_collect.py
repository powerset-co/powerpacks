"""Tiny real message archives exercise reads, caps, counts and SQLite persistence."""
from __future__ import annotations

import errno
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

from packs.ingestion.primitives.deep_context_v2.collect import collect, gmail, readers
from packs.ingestion.primitives.deep_context_v2.collect.bundle import (
    CollectionBundle, MessageChannel, MessageDirection, MessageEntry, Person, ThreadParticipants,
)
from packs.ingestion.primitives.deep_context_v2.db.store import open_store


class MessageFixtures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = self.root / "gmail.sqlite"
        with sqlite3.connect(self.archive) as con:
            con.executescript("""
                CREATE TABLE participants(id INTEGER PRIMARY KEY, email_address TEXT, display_name TEXT);
                CREATE TABLE sources(identifier TEXT);
                CREATE TABLE messages(id INTEGER PRIMARY KEY, message_type TEXT, sender_id INTEGER,
                    conversation_id TEXT, sent_at TEXT, received_at TEXT, internal_date TEXT,
                    subject TEXT, snippet TEXT, deleted_at TEXT, deleted_from_source_at TEXT);
                CREATE TABLE message_bodies(message_id INTEGER, body_text TEXT);
                CREATE TABLE message_recipients(message_id INTEGER, participant_id INTEGER, display_name TEXT);
                INSERT INTO sources VALUES('OWNER@example.com'), (NULL), ('');
                INSERT INTO participants VALUES(1, 'JORDAN@example.com', 'Jordan Bravo');
                INSERT INTO participants VALUES(2, 'owner@example.com', 'Owner Bravo');
                INSERT INTO participants VALUES(3, 'other@example.com', 'Casey Delta');
            """)
        self.add_email(1, 1, "Bio", "Founder at Example Labs https://example.com +15550100000", "thread-a", [1, 2])
        self.add_email(2, 2, "Reply", "Please meet tomorrow about a new project", "thread-a", [1, 1, 3])
        self.add_email(3, 3, "Other", "Third party words must not be evidence", "thread-a", [1])
        self.add_email(4, 1, "Duplicate", "Founder at Example Labs https://example.com +15550100000", "thread-b", [2])
        self.add_email(5, 1, "Deleted", "Deleted context", "thread-c", [2], deleted="yes")
        self.add_email(6, 1, "Not email", "Not email context", "thread-d", [2], kind="chat")
        self.chat = self.root / "chat.sqlite"
        with sqlite3.connect(self.chat) as con:
            con.executescript("""
                CREATE TABLE handle(id TEXT);
                CREATE TABLE chat(guid TEXT, display_name TEXT, room_name TEXT, chat_identifier TEXT);
                CREATE TABLE chat_handle_join(chat_id INTEGER, handle_id INTEGER);
                CREATE TABLE chat_message_join(chat_id INTEGER, message_id INTEGER);
                CREATE TABLE message(guid TEXT, text TEXT, attributedBody BLOB, date INTEGER,
                    is_from_me INTEGER, handle_id INTEGER, associated_message_type INTEGER);
                INSERT INTO handle VALUES('+15550100'), ('+15550101');
                INSERT INTO chat VALUES('direct', '', '', '+15550100');
                INSERT INTO chat VALUES('group', ' Example Group ', 'Example Room', 'chat123');
                INSERT INTO chat_handle_join VALUES(1,1), (2,1), (2,2);
                INSERT INTO message VALUES('m1','Direct from Jordan',NULL,1000000000,0,1,0);
                INSERT INTO message VALUES('m2','Direct from owner',NULL,2000000000,1,1,0);
                INSERT INTO message VALUES('m3','Reaction',NULL,3000000000,0,1,2000);
                INSERT INTO message VALUES('m4','Group from Jordan',NULL,4000000000,0,1,0);
                INSERT INTO message VALUES('m5','Group from Casey',NULL,5000000000,0,2,0);
                INSERT INTO message VALUES('m6','Group from owner',NULL,6000000000,1,0,0);
                INSERT INTO message VALUES('m7','',NULL,7000000000,0,1,0);
                INSERT INTO chat_message_join VALUES(1,1),(1,2),(1,3),(2,4),(2,5),(2,6),(1,7);
            """)
        self.whatsapp = self.root / readers.WACLI_DB_RELATIVE
        self.whatsapp.parent.mkdir(parents=True)
        with sqlite3.connect(self.whatsapp) as con:
            con.executescript("""
                CREATE TABLE messages(chat_jid TEXT, text TEXT, display_text TEXT, ts INTEGER, from_me INTEGER);
                INSERT INTO messages VALUES('15550100@s.whatsapp.net','WhatsApp from Jordan','',100,0);
                INSERT INTO messages VALUES('15550100@s.whatsapp.net','WhatsApp from owner','',200,1);
                INSERT INTO messages VALUES('15550100@s.whatsapp.net','','Media display',300,0);
                INSERT INTO messages VALUES('group@g.us','Excluded group','',400,0);
            """)
        self.person = Person("candidate:phone:+15550100", "Jordan Bravo", ("jordan@example.com",),
                             ("+15550100",), ("gmail_msgvault", "imessage", "whatsapp"))

    def add_email(self, ident, sender, subject, body, thread, recipients, *, deleted=None, kind="email"):
        with sqlite3.connect(self.archive) as con:
            con.execute("INSERT INTO messages VALUES(?,?,?,?,?,NULL,NULL,?,?,?,NULL)",
                        (ident, kind, sender, thread, f"2026-01-{ident:02d}", subject, "preview", deleted))
            con.execute("INSERT INTO message_bodies VALUES(?,?)", (ident, body))
            con.executemany("INSERT INTO message_recipients VALUES(?,?, '')", [(ident, recipient) for recipient in recipients])

    def sources(self, *, cap=20, group_size=25, channels=None):
        sources = readers.ContextSources(channels=set(self.person.source_channels) if channels is None else channels,
                    msgvault_db=self.archive, chat_db=self.chat, wacli_db=self.whatsapp,
                    deep_cap=cap, max_group_size=group_size)
        self.addCleanup(sources.close)
        return sources

    def test_gmail_count_deduplicates_recipients_filters_deleted_and_third_party(self):
        con = gmail.open_msgvault(self.archive)
        self.addCleanup(con.close)
        self.assertEqual(gmail.account_emails(con), {"owner@example.com"})
        self.assertEqual(gmail.count_messages_for(con, "JORDAN@example.com", {"owner@example.com"}), 3)
        self.assertEqual(gmail.count_messages_for(con, "jordan@example.com", set()), 2)
        self.assertEqual(gmail.count_messages_for(con, "missing@example.com", set()), 0)
        self.assertEqual(len(gmail.fetch_recent_rows(con, "jordan@example.com", 20)), 4)

    def test_gmail_selection_breadth_signal_neardup_and_roles(self):
        con = gmail.open_msgvault(self.archive)
        self.addCleanup(con.close)
        messages = gmail.recent_emails_for(con, "jordan@example.com", 20, {"owner@example.com"})
        self.assertEqual(len(messages), 2)
        self.assertEqual([entry.from_role for entry in messages], ["contact", "me"])
        self.assertEqual(messages[0].subject, "Duplicate")
        self.assertNotIn("Third party", " ".join(entry.snippet for entry in messages))
        self.assertEqual(gmail.recent_emails_for(con, "jordan@example.com", 0, set()), [])


    def test_gmail_thread_rosters_names_dedup_and_limit(self):
        con = gmail.open_msgvault(self.archive)
        self.addCleanup(con.close)
        rosters = gmail.thread_participant_rosters(con, ["JORDAN@example.com"], 1)
        self.assertEqual(len(rosters), 1)
        self.assertEqual(gmail.thread_participant_rosters(con, ["missing@example.com"], 2), [])
        all_rosters = gmail.thread_participant_rosters(con, ["jordan@example.com"], 20)
        roster = next(row for row in all_rosters if "Jordan Bravo <jordan@example.com>" in row["participants"])
        self.assertEqual(roster["participants"].count("Jordan Bravo <jordan@example.com>"), 1)

    def test_imessage_direct_and_group_directions_names_blank_reactions(self):
        sources = self.sources()
        direct = sources._read_imessage(self.person)
        self.assertEqual([entry.text for entry in direct], ["Direct from owner", "Direct from Jordan"])
        self.assertEqual(sources._count_imessage_dms(self.person), 3)  # blank body still contributes availability
        group = sources._read_imessage_group_messages(self.person)
        self.assertEqual([entry.direction for entry in group], ["from_me", "from_other", "from_them"])
        self.assertEqual(sources.imessage_groups(self.person), ["Example Group", "Example Room"])
        self.assertTrue(all(entry.at for entry in direct + group))

    def test_imessage_small_group_cap_and_missing_handles(self):
        sources = self.sources(group_size=1)
        self.assertEqual(sources._read_imessage_group_messages(self.person), [])
        unknown = Person("unknown", "", (), ("+15550999",), ("imessage",))
        self.assertEqual(sources.collect_person(unknown), ([], 0))
        self.assertEqual(sources.imessage_groups(unknown), [])

    def test_whatsapp_primary_body_only_and_uncapped_total(self):
        sources = self.sources(cap=2)
        self.assertEqual(sources._count_whatsapp(self.person), 3)
        entries = sources._read_whatsapp(self.person)
        self.assertEqual([entry.text for entry in entries], ["WhatsApp from owner"])
        self.assertEqual(entries[0].direction, "from_me")

    def test_collect_cross_channel_chronological_counts_and_caps(self):
        messages, available = self.sources().collect_person(self.person)
        self.assertEqual(available, 12)  # 3 Gmail + 3 direct + 3 group + 3 WhatsApp
        self.assertEqual(len(messages), 9)  # near-duplicate email and blank chat bodies omitted
        self.assertEqual(messages, sorted(messages, key=MessageEntry.content_order_key))
        self.assertEqual({entry.channel for entry in messages}, set(MessageChannel))
        limited, available = self.sources(cap=1).collect_person(self.person)
        self.assertLess(len(limited), 9)
        self.assertEqual(available, 10)  # Gmail/direct/WhatsApp totals, only one retained group row

    def test_no_channels_never_open_missing_stores(self):
        sources = self.sources(channels=set())
        empty = Person("empty", "", (), (), ())
        self.assertEqual(sources.collect_person(empty), ([], 0))
        self.assertEqual(sources.thread_participants(empty), ())
        self.assertEqual(sources.imessage_groups(empty), [])

    def test_collect_cli_empty_local_store(self):
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(collect.main(["--data-root", str(self.root), "--chat-db", str(self.chat), "--limit", "0"]), 0)
        self.assertIn("completed candidates=0 bundles=0", output.getvalue())


    def test_character_cap_keeps_first_oversized_message(self):
        sources = self.sources()
        with patch.object(readers, "SAFETY_CHAR_CAP", 1):
            messages, available = sources.collect_person(self.person)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].channel, "gmail")
        self.assertGreater(len(messages[0].text), 1)
        self.assertEqual(available, 12)

    def test_missing_message_store_and_schema_fail_not_empty(self):
        self.chat.unlink()
        with self.assertRaisesRegex(RuntimeError, "Cannot read"):
            self.sources()._read_imessage(self.person)
        bad = self.root / "bad.sqlite"
        sqlite3.connect(bad).close()
        with self.assertRaisesRegex(RuntimeError, "no such table"):
            readers.ContextSources(channels={"gmail_msgvault"}, msgvault_db=bad, chat_db=self.chat,
                                   wacli_db=self.whatsapp, deep_cap=10, max_group_size=25)

    def test_collect_run_real_sources_owner_excluded_bundle_fingerprint_rerun(self):
        conn = open_store(self.root / "v2.sqlite")
        self.addCleanup(conn.close)
        for ident, owner in ((self.person.person_id, 0), ("owner", 1), ("empty", 0)):
            conn.execute("INSERT INTO candidates(candidate_id,display_name,is_owner,import_json,imported_at) VALUES(?,?,?,'{}','now')",
                         (ident, "Jordan Bravo", owner))
        conn.executemany("INSERT INTO candidate_identifiers VALUES(?,?,?,?)",
                         [(self.person.person_id, "email", "jordan@example.com", "jordan@example.com"),
                          (self.person.person_id, "phone", "+15550100", "+15550100")])
        conn.executemany("INSERT INTO candidate_sources VALUES(?,?)", [(self.person.person_id, channel) for channel in self.person.source_channels])
        conn.commit()
        node = collect.Collect(conn, self.root, 10, self.chat, msgvault_db=self.archive)
        result = node.run()
        rerun = node.run()
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.counts, dict(candidates=2, bundles=1, no_messages=1, capped=1,
                         messages_gmail=2, messages_imessage=2, messages_imessage_group=3, messages_whatsapp=2))
        self.assertEqual(rerun.counts, result.counts)
        row, = conn.execute("SELECT payload_json, content_fingerprint FROM bundles").fetchall()
        self.assertEqual(row["content_fingerprint"], hashlib.sha256(row["payload_json"].encode()).hexdigest())
        bundle = CollectionBundle.from_payload(json.loads(row["payload_json"]))
        self.assertEqual(bundle.to_payload(), json.loads(row["payload_json"]))
        self.assertEqual(bundle.person_id, self.person.person_id)
        self.assertEqual(bundle.messages_available, 12)
        self.assertTrue(bundle.capped)


class BundleTests(unittest.TestCase):
    def bundle(self):
        return CollectionBundle.of(Person("candidate", "Jordan Bravo", ("jordan@example.com",), (), ("gmail_msgvault",)),
            messages=[MessageEntry.of(MessageChannel.GMAIL, "", from_me=False, text="Hello")],
            groups=["Example Group"], thread_participants=(ThreadParticipants("Subject", ("owner@example.com",)),), available=2)


    def test_tuple_payload_arrays_remain_supported(self):
        bundle = self.bundle()
        payload = bundle.to_payload()
        for field in ("emails", "phones", "source_channels", "groups", "thread_participants", "messages"):
            payload[field] = tuple(payload[field])
        payload["thread_participants"][0]["participants"] = tuple(payload["thread_participants"][0]["participants"])
        self.assertEqual(CollectionBundle.from_payload(payload), bundle)


    def test_bundle_round_trip_and_capped_boundary(self):
        person = Person("candidate", "Jordan Bravo", ("jordan@example.com",), (), ("gmail_msgvault",))
        messages = [MessageEntry.of(MessageChannel.GMAIL, "", from_me=False, text="Hello", subject="Subject")]
        threads = (ThreadParticipants("Subject", ("owner@example.com",)),)
        bundle = CollectionBundle.of(person, messages=messages, groups=["Example Group"], thread_participants=threads, available=2)
        self.assertEqual(CollectionBundle.from_payload(json.loads(json.dumps(bundle.to_payload()))), bundle)
        self.assertTrue(bundle.capped)
        self.assertFalse(CollectionBundle.of(person, messages=messages, groups=[], thread_participants=(), available=1).capped)

    def test_message_invalid_enum_missing_required_key(self):
        payload = MessageEntry.of(MessageChannel.WHATSAPP, "", from_me=True, text="Hello").to_payload()
        for key in ("channel", "direction"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                MessageEntry.from_payload({**payload, key: "bad"})
        del payload["text"]
        with self.assertRaises(KeyError):
            MessageEntry.from_payload(payload)
        with self.assertRaises(KeyError):
            CollectionBundle.from_payload({})

    def test_group_direction_owner_precedence_unknown_sender(self):
        self.assertEqual(MessageDirection.of_group(from_me=True, handle_id=1, contact_handle_ids=frozenset({1})), "from_me")
        self.assertEqual(MessageDirection.of_group(from_me=False, handle_id=None, contact_handle_ids=frozenset({1})), "from_other")


class CleaningAndRetryTests(unittest.TestCase):
    def test_body_quotes_boilerplate_html_and_head_tail(self):
        for suffix in ("On Tuesday someone wrote:", "-----Original Message-----", "From: somebody", "Sent from my phone", "Get Outlook for iOS"):
            with self.subTest(suffix=suffix):
                self.assertEqual(gmail.clean_body("Hello &amp; thanks\n> quoted\n" + suffix + "\nold text"), "Hello & thanks")
        self.assertEqual(gmail.clean_body("> quoted\n "), "")
        long = gmail.clean_body("h" * 400 + "middle" * 200 + "t" * 400)
        self.assertEqual(long, "h" * 300 + " … " + "t" * 300)
        self.assertEqual(gmail.clean_text(" A  &amp; B ", 3), "A &")
        self.assertEqual(gmail.clean_text(None), "")

    def test_signal_score_features_and_length_bonus(self):
        self.assertEqual(gmail.signal_score(""), 0)
        self.assertEqual(gmail.signal_score("Founder https://example.com +15550100000 License #123 @handle at Example"), 12)
        self.assertEqual(gmail.signal_score("x" * 1000), 3)

    def test_retry_transient_then_success_without_sleep(self):
        attempts = []
        def read():
            attempts.append(1)
            if len(attempts) < 3:
                raise OSError(errno.EBUSY, "busy")
            return 7
        with patch("time.sleep") as sleep:
            self.assertEqual(readers._read_source(Path("synthetic.sqlite"), read), 7)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(len(attempts), 3)

    def test_retry_exhaustion_preserves_cause(self):
        def read():
            raise OSError(errno.EIO, "temporary I/O")
        with patch("time.sleep") as sleep, self.assertRaisesRegex(RuntimeError, "after 4 attempts") as caught:
            readers._read_source(Path("synthetic.sqlite"), read)
        self.assertEqual(sleep.call_count, 3)
        self.assertIsInstance(caught.exception.__cause__, OSError)

    def test_schema_failure_not_retried(self):
        def read():
            raise sqlite3.OperationalError("no such table: messages")
        with patch("time.sleep") as sleep, self.assertRaisesRegex(RuntimeError, "after 1 attempts"):
            readers._read_source(Path("synthetic.sqlite"), read)
        sleep.assert_not_called()

    def test_apple_epoch_blank(self):
        self.assertEqual(readers.apple_epoch_iso(None), "")
        self.assertTrue(readers.apple_epoch_iso(100000000000).startswith("2001-"))


if __name__ == "__main__":
    unittest.main()
