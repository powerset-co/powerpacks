from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from deep_context_sqlite_test_helpers import query, scalar
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.share.store import TAG_VOCABULARY, TagStore


class VocabularyTests(unittest.TestCase):
    def test_the_vocabulary_is_the_boolean_labels_plus_the_two_human_decisions(self) -> None:
        self.assertIn("private", TAG_VOCABULARY)
        self.assertIn("share", TAG_VOCABULARY)
        self.assertIn("is_family", TAG_VOCABULARY)
        self.assertNotIn("warmth", TAG_VOCABULARY)
        self.assertNotIn("relationship_kind", TAG_VOCABULARY)


class TagStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Db(Path(self.temp.name) / "deep-context.sqlite")
        self.store = TagStore(self.db)

    def test_apply_upserts_one_row_per_person(self) -> None:
        self.store.apply("person-a", add={"private"}, remove=set(), note="family")
        self.store.apply("person-a", add={"is_family"}, remove=set(), note=None)
        self.store.apply("person-b", add={"share"}, remove=set(), note=None)
        rows = self.store.load()
        self.assertEqual(sorted(rows), ["person-a", "person-b"])
        self.assertEqual(rows["person-a"].tags, {"private", "is_family"})
        self.assertEqual(scalar(self.db, "SELECT count(*) FROM person_tags"), 2)

    def test_a_note_is_kept_until_a_new_one_replaces_it(self) -> None:
        self.store.apply("person-a", add={"private"}, remove=set(), note="family")
        self.assertEqual(self.store.load()["person-a"].note, "family")
        self.store.apply("person-a", add={"share"}, remove=set(), note=None)
        self.assertEqual(self.store.load()["person-a"].note, "family")
        self.store.apply("person-a", add=set(), remove=set(), note="changed my mind")
        self.assertEqual(self.store.load()["person-a"].note, "changed my mind")

    def test_remove_wins_over_add_in_one_call(self) -> None:
        self.store.apply("person-a", add={"private", "share"}, remove={"private"}, note=None)
        self.assertEqual(self.store.load()["person-a"].tags, {"share"})

    def test_an_untagged_store_is_empty_not_missing(self) -> None:
        self.assertEqual(self.store.load(), {})
        self.assertEqual(query(self.db, "SELECT * FROM person_tags"), [])


if __name__ == "__main__":
    unittest.main()
