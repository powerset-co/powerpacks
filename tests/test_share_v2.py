"""The share stage on the v2 store: a family's tags and the lookup's name match.

Created: 2026-10-07, from the Astra-medium review of #736.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context_v2.db import queries, queries_share
from packs.ingestion.primitives.deep_context_v2.db.store import open_store
from packs.ingestion.primitives.deep_context_v2.lookup import matches
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import OwnedIdentifiers, SynthesizedFacts
from packs.ingestion.primitives.share.store import TagStore

NOW = "2026-10-07T20:00:00Z"
LATER = "2026-10-07T21:00:00Z"


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = open_store(Path(self.tmp.name) / "store.sqlite")
        queries.upsert_candidates(self.conn, [("c1", "Jordan Bravo", 0, "{}", NOW), ("c2", "Casey Delta", 0, "{}", NOW)])
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _parent(self, parent_id: str, *candidates: str) -> None:
        self.conn.executemany(
            "INSERT INTO candidate_parent (candidate_id, parent_id, reason, verdict_ref, created_at) VALUES (?, ?, 'human', NULL, ?)",
            [(candidate, parent_id, LATER) for candidate in candidates])
        self.conn.commit()

    def _facts(self, candidate_id: str, name: str) -> None:
        facts = SynthesizedFacts(canonical_name=name, aliases=(), employers=(), title="", school="", field_of_study="",
                                 location="", relationship_to_owner="", relationship_category="", topics=(),
                                 notable_events=(), identifiers=(), owned_identifiers=OwnedIdentifiers((), (), ()),
                                 shared_context=(), confidence=0.9, is_owner=False)
        self.conn.execute(
            "INSERT INTO facts (candidate_id, facts_json, input_fingerprint, model, reasoning_effort, synthesized_at) "
            "VALUES (?, ?, 'f', 'm', 'low', ?)", (candidate_id, json.dumps(facts.to_payload()), NOW))
        self.conn.commit()


class FamilyTagsTests(StoreTests):
    def test_a_merged_family_keeps_every_members_human_word(self) -> None:
        # Two people the human tagged separately: one private (older), one later cleared.
        self._parent("p:a", "c1")
        self._parent("p:b", "c2")
        self.conn.executemany("INSERT INTO person_tags (candidate_id, tags, note, updated_at) VALUES (?, ?, ?, ?)",
                              [("c1", "private", "landlord", NOW), ("c2", "", "", LATER)])
        self.conn.commit()
        # Dedupe merges them: the family's tags are the union, so the private stands.
        self._parent("li:42", "c1", "c2")
        held = TagStore(self.conn).load()
        self.assertEqual(set(held), {"li:42"})
        self.assertEqual(held["li:42"].tags, frozenset({"private"}))
        self.assertEqual(queries_share.current_tags(self.conn)["li:42"].tags, "private")

    def test_tags_from_two_members_add_up(self) -> None:
        self._parent("li:42", "c1", "c2")
        self.conn.executemany("INSERT INTO person_tags (candidate_id, tags, note, updated_at) VALUES (?, ?, ?, ?)",
                              [("c1", "is_family", "", NOW), ("c2", "share", "note", LATER)])
        self.conn.commit()
        held = queries_share.current_tags(self.conn)["li:42"]
        self.assertEqual(held.tags, "is_family|share")
        self.assertEqual((held.note, held.updated_at), ("note", LATER))


class LookupTests(StoreTests):
    def test_a_typed_name_matches_one_written_name_not_words_pooled_across_the_family(self) -> None:
        self._parent("li:42", "c1", "c2")
        self._facts("c1", "Jordan Bravo")
        self._facts("c2", "Casey Delta")
        self.assertEqual([match.parent_id for match in matches(self.conn, name="Jordan Bravo")], ["li:42"])
        self.assertEqual([match.parent_id for match in matches(self.conn, name="casey")], ["li:42"])
        self.assertEqual(matches(self.conn, name="Jordan Delta"), [])


if __name__ == "__main__":
    unittest.main()
