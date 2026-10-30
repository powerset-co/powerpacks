from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from packs.indexing.primitives.republish_school_parents.republish_school_parents import (
    PAGE_SIZE,
    merged_name,
    plan,
    run,
)
from packs.search.primitives.turbopuffer.turbopuffer_resolve_education import without_alias


def mapping(*items):
    return {school_id: {"id": school_id, "key": f"name:{school_id}", "school_name": name, "parent_id": parent} for school_id, name, parent in items}


def edge(edge_id, person, school):
    return {"id": edge_id, "person_id": person, "education_id": school, "canonical_education_id": school}


class MergedNameTest(unittest.TestCase):
    def test_children_join_the_existing_alias(self):
        self.assertEqual(merged_name("UCLA (Bruins)", ["Anderson School"]), "UCLA (Bruins / Anderson School)")

    def test_no_new_word_leaves_the_name_alone(self):
        self.assertEqual(merged_name("Harvard University", ["University Harvard", "HARVARD"]), "Harvard University")

    def test_nested_parentheses_still_strip_to_the_base_name(self):
        name = merged_name("Oxford (Univ)", ["Said Business School (SBS)", "Kellogg (Oxford) College"])
        self.assertEqual(name, "Oxford (Univ / Said Business School / SBS / Kellogg Oxford College)")
        self.assertEqual(without_alias(name), "Oxford")


class PlanTest(unittest.TestCase):
    rows = mapping(("kth", "KTH", None), ("kungliga", "Kungliga tekniska högskolan", "kth"), ("empty", "Empty Child", "kth"), ("mit", "MIT", None))
    schools = [
        {"id": "kth", "school_name": "KTH", "person_count": 2},
        {"id": "kungliga", "school_name": "Kungliga tekniska högskolan", "person_count": 2},
        {"id": "empty", "school_name": "Empty Child", "person_count": 0},
        {"id": "mit", "school_name": "MIT", "person_count": 9},
    ]
    edges = [edge("1", "a", "kth"), edge("2", "b", "kth"), edge("3", "b", "kungliga"), edge("4", "c", "kungliga"), edge("5", "d", "mit")]

    def test_rows_move_counts_are_distinct_people_and_children_go(self):
        writes = plan(self.edges, self.schools, self.rows)
        self.assertEqual(writes["edge_patches"], [{"id": "3", "canonical_education_id": "kth"}, {"id": "4", "canonical_education_id": "kth"}])
        self.assertEqual(writes["school_patches"], [{"id": "kth", "school_name": "KTH (Kungliga tekniska högskolan / Empty Child)", "person_count": 3}])
        self.assertEqual(writes["deletes"], ["kungliga", "empty"])


class FakeNamespace:
    def __init__(self, name, docs, log):
        self.id, self.docs, self.log = name, {doc["id"]: dict(doc) for doc in docs}, log

    def query(self, *, filters, **_):
        after = filters[2] if filters else ""
        ids = sorted(doc_id for doc_id in self.docs if doc_id > after)[:PAGE_SIZE]
        return SimpleNamespace(rows=[SimpleNamespace(to_dict=lambda doc_id=doc_id: dict(self.docs[doc_id])) for doc_id in ids])

    def write(self, upsert_rows=(), patch_rows=(), deletes=()):
        self.log.append(self.id)
        for row in upsert_rows:
            self.docs[row["id"]] = dict(row)
        for patch in patch_rows:
            self.docs[patch["id"]].update(patch)
        for doc_id in deletes:
            del self.docs[doc_id]


class RunTest(unittest.TestCase):
    def namespaces(self):
        log = []
        return FakeNamespace("edges", PlanTest.edges, log), FakeNamespace("schools", PlanTest.schools, log), log

    def test_dry_run_writes_nothing(self):
        edges, schools, log = self.namespaces()
        with tempfile.TemporaryDirectory() as tmp:
            report = run(edges, schools, PlanTest.rows, apply=False, backup_dir=Path(tmp))
            self.assertEqual(list(Path(tmp).iterdir()), [])
        self.assertEqual((report["edge_patches"], report["school_patches"], report["deletes"]), (2, 1, 2))
        self.assertEqual(log, [])

    def test_apply_backs_up_writes_and_a_rerun_finds_nothing(self):
        edges, schools, _ = self.namespaces()
        before = {doc_id: dict(doc) for doc_id, doc in edges.docs.items()}
        with tempfile.TemporaryDirectory() as tmp:
            report = run(edges, schools, PlanTest.rows, apply=True, backup_dir=Path(tmp))
            lines = Path(report["backups"][0]).read_text().splitlines()
            self.assertEqual({row["id"]: row for row in map(json.loads, lines)}, before)
            self.assertEqual(len(Path(report["backups"][1]).read_text().splitlines()), 4)
            rerun = run(edges, schools, PlanTest.rows, apply=False, backup_dir=Path(tmp))
        self.assertEqual(report["remaining"], {"edge_patches": 0, "school_patches": 0, "restores": 0, "deletes": 0})
        self.assertEqual((rerun["edge_patches"], rerun["school_patches"], rerun["deletes"]), (0, 0, 0))
        self.assertEqual({doc["canonical_education_id"] for doc_id, doc in edges.docs.items() if doc_id in "34"}, {"kth"})
        self.assertEqual(sorted(schools.docs), ["kth", "mit"])

    def test_a_corrected_mapping_undoes_the_merge(self):
        edges, schools, _ = self.namespaces()
        corrected = {**PlanTest.rows, "kungliga": {**PlanTest.rows["kungliga"], "parent_id": None}}
        with tempfile.TemporaryDirectory() as tmp:
            run(edges, schools, PlanTest.rows, apply=True, backup_dir=Path(tmp))
            report = run(edges, schools, corrected, apply=True, backup_dir=Path(tmp))
        self.assertEqual(report["restores"], 1)
        self.assertEqual(report["remaining"], {"edge_patches": 0, "school_patches": 0, "restores": 0, "deletes": 0})
        self.assertEqual(schools.docs["kungliga"], {"id": "kungliga", "school_name": "Kungliga tekniska högskolan", "person_count": 2, "canonical_key": "name:kungliga"})
        self.assertEqual(schools.docs["kth"]["school_name"], "KTH (Empty Child)")
        self.assertEqual(schools.docs["kth"]["person_count"], 2)
        self.assertEqual({doc["canonical_education_id"] for doc_id, doc in edges.docs.items() if doc_id in "34"}, {"kungliga"})


if __name__ == "__main__":
    unittest.main()
