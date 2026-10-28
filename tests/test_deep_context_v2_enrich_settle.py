"""Settle preserves worth except for the owner's own LinkedIn connections; synthetic stores only."""
from __future__ import annotations

import json

from test_people_worth import LATER, NOW, StoreTests
from packs.ingestion.primitives.deep_context_v2.db import queries_enrich
from packs.ingestion.primitives.deep_context_v2.enrich.settle import OWN_CONNECTION_REASON, settle


class SettleTests(StoreTests):
    def setUp(self) -> None:
        super().setUp()
        self._parent("p:a", "c1", "c2")
        self._facts("c1", "Jordan Bravo")

    def _worth(self, worth: str, decided_by: str = "machine", candidate: str = "c1") -> None:
        self.conn.execute("INSERT INTO worth (candidate_id, worth, decided_by, reason, labels_json, input_fingerprint, created_at) "
                          "VALUES (?, ?, ?, 'original', ?, 'test', ?)",
                          (candidate, worth, decided_by, json.dumps({"relationship_kind": "colleague"}), NOW))

    def _connection(self, verdict: str = "confirmed") -> None:
        queries_enrich.insert_linkedin(self.conn, (
            "c1", "https://www.linkedin.com/in/jordan-bravo", "42", "linkedin_network", verdict, "machine",
            "test", None, "test", NOW))

    def test_without_a_profile_or_messages_every_worth_is_preserved(self) -> None:
        for worth in ("yes", "no"):
            with self.subTest(worth=worth):
                self._worth(worth)
                before = [tuple(row) for row in self.conn.execute("SELECT * FROM worth")]
                self.assertEqual(settle(self.conn, LATER), 0)
                self.assertEqual([tuple(row) for row in self.conn.execute("SELECT * FROM worth")], before)

    def test_an_own_connection_keeps_labels_and_sets_every_member_to_yes_once(self) -> None:
        self._worth("no")
        self._connection()
        self.assertEqual(settle(self.conn, LATER), 2)
        self.assertEqual([tuple(row) for row in self.conn.execute(
            "SELECT candidate_id, worth, reason, labels_json FROM worth ORDER BY seq DESC LIMIT 2")],
            [(candidate, "yes", OWN_CONNECTION_REASON, json.dumps({"relationship_kind": "colleague"}))
             for candidate in ("c2", "c1")])
        self.assertEqual(settle(self.conn, LATER), 0)

    def test_a_human_no_on_another_member_wins_over_an_own_connection(self) -> None:
        self._worth("no")
        self._worth("no", "human", "c2")
        self._connection()
        self.assertEqual(settle(self.conn, LATER), 0)
        self.assertEqual(tuple(self.conn.execute("SELECT worth, decided_by FROM current_worth").fetchone()),
                         ("no", "human"))

    def test_a_wrong_person_own_connection_preserves_worth(self) -> None:
        self._worth("no")
        self._connection("wrong_person")
        self.assertEqual(settle(self.conn, LATER), 0)
        self.assertEqual(self.conn.execute("SELECT worth FROM current_worth").fetchone()[0], "no")
