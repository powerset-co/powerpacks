"""Enrich step 4a: an email-confirmed pre-match becomes a confirmed LinkedIn without the judge.

Created: 2026-10-07, after a fresh install crashed here: the confirm call omitted the verdict's
confidence and reason once they were added to the ledger (TypeError, enrich.py:89).
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context_v2.db import queries
from packs.ingestion.primitives.deep_context_v2.db.store import open_store
from packs.ingestion.primitives.deep_context_v2.enrich.enrich import EMAIL_CONFIRMED_REASON, confirm_by_email
from packs.ingestion.primitives.deep_context_v2.enrich.family import Family
from packs.ingestion.primitives.deep_context_v2.enrich.profiles import Profile, Profiles
from packs.ingestion.primitives.deep_context_v2.enrich.proposals import Proposals

URL = "https://www.linkedin.com/in/jordan-bravo"
NOW = "2026-10-07T20:00:00Z"


def family(parent_id: str, candidates: tuple[str, ...]) -> Family:
    """A family as confirm reads it: its parent, its candidates and its facts fingerprints. The judges'
    evidence (members, worth, facts, verdicts) is not read on this path."""
    return Family(parent_id=parent_id, candidates=candidates, members=(), facts_fingerprints=("f1",),
                  worth=None, names=("Jordan Bravo",), identifiers=(), facts=None, messages=3,  # type: ignore[arg-type]
                  verdicts=())


def profile(member_id: str) -> Profile:
    return Profile(linkedin_url=URL, public_identifier="jordan-bravo", member_id=member_id, full_name="Jordan Bravo",
                   headline="Founder", location="Springfield", experiences=("Founder @ Example Labs, 2020-",),
                   education=(), fetched_at=NOW)


class ConfirmByEmailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = open_store(Path(self.tmp.name) / "store.sqlite")
        queries.upsert_candidates(self.conn, [("c1", "Jordan Bravo", 0, "{}", NOW), ("c2", "J. Bravo", 0, "{}", NOW)])
        self.conn.executemany(
            "INSERT INTO candidate_parent (candidate_id, parent_id, reason, verdict_ref, created_at) VALUES (?, 'p:1', 'singleton', NULL, ?)",
            [("c1", NOW), ("c2", NOW)])
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_a_confirmed_email_match_writes_the_verdict_and_moves_every_member(self) -> None:
        found = Proposals(families=[family("p:1", ("c1", "c2"))], by_email={"p:1": URL}, proposed={}, research={})
        counts: dict[str, int] = {}
        parents = confirm_by_email(self.conn, found, Profiles(found={URL: profile("4242")}, missing=0, fetched=0), NOW, counts)
        self.conn.commit()
        self.assertEqual(counts, {"email_confirmed": 1, "email_confirm_no_profile": 0})
        self.assertEqual(len(parents), 2)
        verdicts = self.conn.execute(
            "SELECT candidate_id, linkedin_url, member_id, verdict, decided_by, confidence, reason FROM candidate_linkedins ORDER BY candidate_id"
        ).fetchall()
        self.assertEqual([tuple(row) for row in verdicts], [
            ("c1", URL, "4242", "confirmed", "machine", None, EMAIL_CONFIRMED_REASON),
            ("c2", URL, "4242", "confirmed", "machine", None, EMAIL_CONFIRMED_REASON),
        ])
        self.assertEqual({row[0] for row in self.conn.execute("SELECT parent_id FROM current_parent")}, {"li:4242"})

    def test_a_match_without_a_profile_is_counted_and_left_alone(self) -> None:
        found = Proposals(families=[family("p:1", ("c1", "c2"))], by_email={"p:1": URL}, proposed={}, research={})
        counts: dict[str, int] = {}
        parents = confirm_by_email(self.conn, found, Profiles(found={}, missing=1, fetched=0), NOW, counts)
        self.assertEqual(parents, [])
        self.assertEqual(counts, {"email_confirmed": 0, "email_confirm_no_profile": 1})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM candidate_linkedins").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
