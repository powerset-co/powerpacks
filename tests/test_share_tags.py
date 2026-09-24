from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.share.share import _split_tag_args
from packs.ingestion.primitives.share.tags import TAG_VOCABULARY, TagStore, lookup_targets
from packs.shared.csv_io import CsvIO


class VocabularyTests(unittest.TestCase):
    def test_the_vocabulary_is_the_boolean_labels_plus_the_two_human_decisions(self) -> None:
        self.assertIn("private", TAG_VOCABULARY)
        self.assertIn("share", TAG_VOCABULARY)
        self.assertIn("is_family", TAG_VOCABULARY)
        self.assertNotIn("warmth", TAG_VOCABULARY)
        self.assertNotIn("relationship_kind", TAG_VOCABULARY)


class TagArgumentTests(unittest.TestCase):
    def test_plus_and_minus_words_are_pulled_out_before_argparse(self) -> None:
        rest, add, remove, unknown = _split_tag_args(["tag", "--name", "Jordan Bravo", "+private", "-is_family"])
        self.assertEqual(rest, ["tag", "--name", "Jordan Bravo"])
        self.assertEqual(add, {"private"})
        self.assertEqual(remove, {"is_family"})
        self.assertEqual(unknown, set())

    def test_a_plus_word_outside_the_vocabulary_is_reported_not_parsed(self) -> None:
        rest, add, remove, unknown = _split_tag_args(["tag", "+homie"])
        self.assertEqual(rest, ["tag"])
        self.assertEqual(unknown, {"homie"})
        self.assertEqual(add | remove, set())

    def test_ordinary_flags_survive_the_split(self) -> None:
        rest, add, _, _ = _split_tag_args(["tag", "--person-id", "person-a", "--note", "close", "+share"])
        self.assertEqual(rest, ["tag", "--person-id", "person-a", "--note", "close"])
        self.assertEqual(add, {"share"})


class TagStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "tags.csv"
        self.store = TagStore(Path(self.temp.name))

    def test_apply_upserts_one_row_per_person(self) -> None:
        self.store.apply("person-a", add={"private"}, remove=set(), note="family")
        self.store.apply("person-a", add={"is_family"}, remove=set(), note=None)
        self.store.apply("person-b", add={"share"}, remove=set(), note=None)
        rows = self.store.load()
        self.assertEqual(sorted(rows), ["person-a", "person-b"])
        self.assertEqual(rows["person-a"].tags, {"private", "is_family"})
        self.assertEqual(len(CsvIO.read_dict_rows_normalized(self.path)), 2)

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


class LookupTests(unittest.TestCase):
    def test_a_query_resolves_to_a_person_id_through_the_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "index.json"
            index.write_text(
                json.dumps(
                    {
                        "slugs": {"jordan-bravo-aaaa": {"person_id": "person-a", "name": "Jordan Bravo"}},
                        "by_name": {"jordan bravo": ["jordan-bravo-aaaa"]},
                        "by_email": {"casey@example.com": ["jordan-bravo-aaaa"]},
                        "by_phone": {"15550100": ["jordan-bravo-aaaa"]},
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                [t.person_id for t in lookup_targets(name="Jordan Bravo", index_json=index)], ["person-a"]
            )
            self.assertEqual(lookup_targets(email="casey@example.com", index_json=index)[0].slug, "jordan-bravo-aaaa")
            self.assertEqual(lookup_targets(phone="+15550100", index_json=index)[0].person_id, "person-a")
            self.assertEqual(lookup_targets(name="Nobody Here", index_json=index), ())


if __name__ == "__main__":
    unittest.main()
