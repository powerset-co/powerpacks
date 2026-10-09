"""Small v2 regressions with synthetic local stores and a cached owner profile."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
import warnings
from pathlib import Path

from packs.ingestion.primitives.deep_context_v2.collect.gmail import fetch_recent_rows
from packs.ingestion.primitives.deep_context_v2.db import queries
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.lookup import matches
from packs.ingestion.primitives.deep_context_v2.names import names_can_match, written_names_can_match
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import OwnedIdentifiers, SynthesizedFacts

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-10-08T00:00:00Z"


class NameGuardTests(unittest.TestCase):
    def test_identical_one_word_names_never_pair(self) -> None:
        self.assertFalse(names_can_match(("jordan",), ("jordan",)))
        self.assertFalse(written_names_can_match("Jordan", "Jordan"))
        self.assertFalse(names_can_match(("jordan",), ("jordan", "bravo")))
        self.assertTrue(written_names_can_match("Jordan Bravo", "Jordan Bravo"))
        self.assertTrue(written_names_can_match("Jordan Bravo", "Bravo, Jordan"))


class LookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = open_store(Path(self.tmp.name) / "store.sqlite")
        self.addCleanup(self.conn.close)
        queries.upsert_candidates(self.conn, [
            ("c1", "Jordan Alpha", 0, "{}", NOW),
            ("c2", "Casey Bravo", 0, "{}", NOW),
        ])
        for candidate, canonical in (("c1", "Taylor Delta"), ("c2", "Morgan Echo")):
            facts = SynthesizedFacts(canonical, (), (), "", "", "", "", "friend", "personal",
                                     (), (), (), OwnedIdentifiers((), (), ()), (), 0.9, False)
            queries.upsert_facts(self.conn, candidate, json.dumps(facts.to_payload()), "f", "synthetic", "low", NOW)
            self.conn.execute(
                "INSERT INTO candidate_parent (candidate_id, parent_id, reason, verdict_ref, created_at) "
                "VALUES (?, 'p:1', 'singleton', NULL, ?)", (candidate, NOW))
        self.conn.execute(
            "INSERT INTO candidate_identifiers (candidate_id, kind, normalized_value, display_value) "
            "VALUES ('c1', 'email', 'jordan@example.com', 'jordan@example.com')")
        self.conn.commit()

    def test_blank_name_matches_nothing(self) -> None:
        for name in ("", " ", "\t", ", ,"):
            with self.subTest(name=name):
                self.assertEqual(matches(self.conn, name=name), [])
        self.assertEqual([m.parent_id for m in matches(self.conn, name=" ", email="jordan@example.com")], ["p:1"])

    def test_name_words_must_belong_to_one_written_or_canonical_name(self) -> None:
        for name in ("Jordan Alpha", "Taylor Delta", "Casey Bravo", "Morgan Echo"):
            with self.subTest(name=name):
                self.assertEqual([m.parent_id for m in matches(self.conn, name=name)], ["p:1"])
        for name in ("Jordan Bravo", "Jordan Delta", "Taylor Echo", "Nobody Here"):
            with self.subTest(name=name):
                self.assertEqual(matches(self.conn, name=name), [])


class GmailBindingTests(unittest.TestCase):
    def test_recent_mail_uses_portable_bindings_and_deduplicates_participation(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE participants (id INTEGER, email_address TEXT);
            CREATE TABLE messages (id INTEGER, sender_id INTEGER, sent_at TEXT, received_at TEXT,
                internal_date TEXT, conversation_id TEXT, subject TEXT, snippet TEXT,
                message_type TEXT, deleted_at TEXT, deleted_from_source_at TEXT);
            CREATE TABLE message_bodies (message_id INTEGER, body_text TEXT);
            CREATE TABLE message_recipients (message_id INTEGER, participant_id INTEGER);
            INSERT INTO participants VALUES (1, 'jordan@example.com'), (2, 'owner@example.com'), (3, 'other@example.com');
            INSERT INTO messages VALUES
                (1, 1, '2026-10-01', NULL, NULL, 't1', 'contact mail', 'one', 'email', NULL, NULL),
                (2, 2, '2026-10-02', NULL, NULL, 't2', 'owner reply', 'two', 'email', NULL, NULL),
                (3, 3, '2026-10-03', NULL, NULL, 't3', 'unrelated', 'three', 'email', NULL, NULL),
                (4, 1, '2026-10-04', NULL, NULL, 't4', 'deleted', 'four', 'email', 'deleted', NULL),
                (5, 1, '2026-10-05', NULL, NULL, 't5', 'source deleted', 'five', 'email', NULL, 'deleted');
            INSERT INTO message_recipients VALUES (1, 1), (2, 1), (2, 1);
            INSERT INTO message_bodies VALUES (2, 'synthetic reply');
        """)
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            rows = fetch_recent_rows(conn, "JORDAN@example.com", 10)
            limited = fetch_recent_rows(conn, "jordan@example.com", 1)
            absent = fetch_recent_rows(conn, "missing@example.com", 10)
        self.assertEqual([r["subject"] for r in rows], ["owner reply", "contact mail"])
        self.assertEqual(rows[0]["body_text"], "synthetic reply")
        self.assertEqual([r["sender_email"] for r in rows], ["owner@example.com", "jordan@example.com"])
        self.assertEqual([r["subject"] for r in limited], ["owner reply"])
        self.assertEqual(absent, [])


class OwnerCLITests(unittest.TestCase):
    def test_cached_owner_cli_persists_row_visible_to_independent_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp)
            cache = data_root / "network-import/profile_cache_v2/jordan-owner.json"
            cache.parent.mkdir(parents=True)
            cache.write_text(json.dumps({
                "fetched_at": NOW, "public_identifier": "jordan-owner",
                "linkedin_url": "https://www.linkedin.com/in/jordan-owner", "raw_response": {},
                "normalized_profile": {"success": True, "member_id": "synthetic-owner-1",
                    "full_name": "Jordan Owner", "headline": "", "location_str": "Springfield",
                    "city": "", "state": "", "country": "",
                    "experiences": [{"company_name": "Example Labs", "title": "Founder",
                                     "starts_at": {"year": 2020}, "ends_at": None}], "education": []},
            }))
            chat_db = data_root / "chat.db"
            with sqlite3.connect(chat_db) as chat:
                chat.executescript("""
                    CREATE TABLE chat (account_login TEXT);
                    CREATE TABLE message (is_from_me INTEGER, destination_caller_id TEXT);
                    INSERT INTO chat VALUES ('P:+15550100001');
                """)
            result = subprocess.run([
                str(ROOT / "bin/deep-context-v2"), "owner", "--data-root", str(data_root),
                "--linkedin-url", "https://www.linkedin.com/in/jordan-owner",
                "--email", "owner@example.com", "--chat-db", str(chat_db),
            ], cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            emitted = json.loads(result.stdout)
            self.assertEqual(emitted["fetched"], 0)
            self.assertEqual(emitted["name"], "Jordan Owner")
            payload = json.loads(Path(emitted["owner_json"]).read_text())
            self.assertEqual(payload["emails"], ["owner@example.com"])
            self.assertEqual(payload["phones"], ["+15550100001"])
            with sqlite3.connect(store_path(data_root)) as independent:
                row = independent.execute("SELECT payload_json FROM owner").fetchone()
            self.assertIsNotNone(row, "owner CLI emitted owner.json but committed no owner row")
            self.assertEqual(json.loads(row[0]), payload)


if __name__ == "__main__":
    unittest.main()
