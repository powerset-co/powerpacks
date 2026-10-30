from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.indexing.lib.artifacts import build_education_corpus, stable_school_uuid
from packs.indexing.lib.identity import school_uuid
from packs.indexing.lib.school_parents import (
    add_schools,
    apply_canonical_names,
    apply_same_names,
    flatten,
    normalize_name,
    read_rows,
    school_rows,
    write_rows,
)
from packs.indexing.primitives.update_school_parents import update_school_parents


def row(school_id, name, key=None, parent_id=None, source=None):
    return {"id": school_id, "key": key, "school_name": name, "parent_id": parent_id, "source": source}


def rows_of(*items):
    return {item["id"]: item for item in items}


class NormalizeNameTest(unittest.TestCase):
    def test_folds_case_accents_punctuation_the_and_alias(self):
        self.assertEqual(normalize_name("The Università-di Padova (UNIPD)"), "universita di padova")


class SameNamesTest(unittest.TestCase):
    def test_name_keyed_school_joins_the_provider_school(self):
        rows = rows_of(row("a", "The Wharton School", "provider:5290", source="seed"), row("b", "Wharton School", "name:wharton"))
        self.assertEqual(apply_same_names(rows, {}), 1)
        self.assertEqual((rows["b"]["parent_id"], rows["b"]["source"]), ("a", "name"))

    def test_two_provider_schools_stay_apart(self):
        rows = rows_of(row("a", "Lincoln High School", "provider:1"), row("b", "Lincoln High School", "provider:2"), row("c", "Lincoln High School", "name:lincoln"))
        self.assertEqual(apply_same_names(rows, {}), 0)

    def test_largest_name_keyed_school_wins_without_a_provider(self):
        rows = rows_of(row("a", "Menlo Atherton High", "name:a"), row("b", "Menlo-Atherton High", "name:b"))
        apply_same_names(rows, {"b": 9, "a": 1})
        self.assertEqual(rows["a"]["parent_id"], "b")
        self.assertIsNone(rows["b"]["parent_id"])

    def test_reviewed_rows_keep_their_decision(self):
        rows = rows_of(row("a", "X", "provider:1"), row("b", "X", "name:x", source="manual"))
        self.assertEqual(apply_same_names(rows, {}), 0)


class CanonicalNamesTest(unittest.TestCase):
    def test_translation_rolls_up_and_unchanged_is_reviewed(self):
        rows = rows_of(row("kth", "KTH Royal Institute of Technology"), row("kung", "Kungliga tekniska högskolan", "provider:4814"))
        apply_canonical_names(rows, ["kth", "kung"], {"Kungliga tekniska högskolan": "KTH Royal Institute of Technology"}, {})
        self.assertEqual(rows["kung"]["parent_id"], "kth")
        self.assertEqual((rows["kth"]["parent_id"], rows["kth"]["source"]), (None, "llm"))

    def test_unknown_canonical_name_leaves_no_parent(self):
        rows = rows_of(row("a", "Some College"))
        apply_canonical_names(rows, ["a"], {"Some College": "Nowhere University"}, {})
        self.assertEqual((rows["a"]["parent_id"], rows["a"]["source"]), (None, "llm"))


class FlattenTest(unittest.TestCase):
    def test_chain_points_at_the_top(self):
        rows = rows_of(row("a", "A", parent_id="b"), row("b", "B", parent_id="c"), row("c", "C"))
        flatten(rows)
        self.assertEqual((rows["a"]["parent_id"], rows["b"]["parent_id"], rows["c"]["parent_id"]), ("c", "c", None))

    def test_loop_keeps_one_top(self):
        rows = rows_of(row("a", "A", parent_id="b"), row("b", "B", parent_id="a"), row("c", "C", parent_id="b"))
        flatten(rows)
        self.assertEqual((rows["a"]["parent_id"], rows["b"]["parent_id"], rows["c"]["parent_id"]), (None, "a", "a"))


class UpdateTest(unittest.TestCase):
    def test_only_new_schools_are_added_and_rerun_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.jsonl"
            write_rows(rows_of(row("a", "Old School", "provider:1", source="seed")), path)
            schools = [
                {"id": "a", "school_name": "Old School", "person_count": 5, "canonical_key": "provider:1"},
                {"id": "b", "school_name": "The Old School", "person_count": 2, "canonical_key": "name:the old school"},
                {"id": "c", "school_name": "New School", "person_count": 1, "canonical_key": "name:new school"},
            ]
            report = update_school_parents.run(schools, path, False, "gpt-6-luna", None)
            self.assertEqual((report["added"], report["same_name"], report["unreviewed"]), (2, 1, 1))
            before = path.read_text()
            report = update_school_parents.run(schools, path, False, "gpt-6-luna", None)
            self.assertEqual((report["added"], report["same_name"]), (0, 0))
            self.assertEqual(path.read_text(), before)

    def test_llm_only_sees_unreviewed_schools_and_saves_the_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.jsonl"
            write_rows(rows_of(row("a", "Harvard University", "provider:1", source="seed"), row("b", "HBS", "provider:2")), path)
            seen = []

            def fake(client, names, model):
                seen.append(names)
                return {"HBS": "Harvard University"}

            with mock.patch.object(update_school_parents, "canonical_names", fake), mock.patch.object(update_school_parents, "OpenAI"):
                report = update_school_parents.run([], path, True, "gpt-6-luna", None)
            self.assertEqual(seen, [["HBS"]])
            self.assertEqual(report["reviewed"], 1)
            self.assertEqual(read_rows(path)["b"]["parent_id"], "a")


class MappingFileTest(unittest.TestCase):
    def test_ids_match_their_keys_and_parents_exist_at_the_top(self):
        rows = school_rows()
        for school in rows.values():
            if school["key"]:
                self.assertEqual(school_uuid(school["key"]), school["id"])
            if school["parent_id"]:
                self.assertIsNone(rows[school["parent_id"]]["parent_id"])

    def test_kth_rolls_up_in_the_built_corpus(self):
        people = [
            {"name": "A", "linkedin_url": "https://linkedin.com/in/a", "education": json.dumps([{"school_id": "4814", "school_name": "Kungliga tekniska högskolan"}])},
            {"name": "B", "linkedin_url": "https://linkedin.com/in/b", "education": json.dumps([{"school_name": "KTH Royal Institute of Technology"}])},
        ]
        result = build_education_corpus(people)
        kth = stable_school_uuid({"school_name": "KTH Royal Institute of Technology"})
        self.assertEqual({rec["canonical_education_id"] for rec in result["education"]}, {kth})
        self.assertEqual(len({rec["education_id"] for rec in result["education"]}), 2)
        self.assertEqual([(s["id"], s["school_name"], s["person_count"]) for s in result["schools"]], [(kth, "KTH Royal Institute of Technology", 2)])


if __name__ == "__main__":
    unittest.main()
