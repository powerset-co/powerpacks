from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from packs.indexing.primitives.republish_school_parents.republish_school_parents import (
    PAGE_SIZE,
    own_name,
    plan,
    run,
)


def mapping(*items):
    return {school_id: {"id": school_id, "key": f"name:{school_id}", "school_name": name, "parent_id": parent} for school_id, name, parent in items}


def edge(edge_id, person, school):
    return {"id": edge_id, "person_id": person, "education_id": school, "canonical_education_id": school}


class OwnNameTest(unittest.TestCase):
    def test_a_name_a_child_covers_comes_off(self):
        self.assertEqual(own_name("Harvard University (HARVARD COLLEGE)", ["Harvard College"]), "Harvard University")
        self.assertEqual(own_name("UCLA (Bruins / Anderson)", ["The Anderson"]), "UCLA (Bruins)")

    def test_an_acronym_no_child_covers_stays(self):
        self.assertEqual(own_name("China Europe International Business School (CEIBS)", ["CEIBS Online"]), "China Europe International Business School (CEIBS)")
        self.assertEqual(own_name("Universidad Central (CO)", []), "Universidad Central (CO)")


class PlanTest(unittest.TestCase):
    rows = mapping(("kth", "KTH (Kungliga)", None), ("kungliga", "Kungliga", "kth"), ("empty", "Empty Child", "kth"), ("mit", "MIT", None))
    schools = [
        {"id": "kth", "school_name": "KTH (Kungliga)", "person_count": 2},
        {"id": "kungliga", "school_name": "Kungliga", "person_count": 2},
        {"id": "empty", "school_name": "Empty Child", "person_count": 0},
        {"id": "mit", "school_name": "MIT", "person_count": 9},
    ]
    edges = [edge("1", "a", "kth"), edge("2", "b", "kth"), edge("3", "b", "kungliga"), edge("4", "c", "kungliga"), edge("5", "d", "mit")]

    def test_counts_are_distinct_people_and_children_keep_their_docs(self):
        writes = plan(self.edges, self.schools, self.rows)
        self.assertEqual(writes["school_patches"], [
            {"id": "kth", "school_name": "KTH", "person_count": 3},
            {"id": "kungliga", "parent_id": "kth"},
            {"id": "empty", "parent_id": "kth"},
        ])
        self.assertEqual(writes["restores"], [])

    def test_a_missing_doc_is_rebuilt_with_its_own_count(self):
        schools = [school for school in self.schools if school["id"] != "kungliga"]
        writes = plan(self.edges, schools, self.rows)
        self.assertEqual(writes["restores"], [{"id": "kungliga", "school_name": "Kungliga", "person_count": 2, "parent_id": "kth", "canonical_key": "name:kungliga"}])


class FakeNamespace:
    def __init__(self, name, docs, log):
        self.id, self.docs, self.log = name, {doc["id"]: dict(doc) for doc in docs}, log

    def query(self, *, filters, **_):
        after = filters[2] if filters else ""
        ids = sorted(doc_id for doc_id in self.docs if doc_id > after)[:PAGE_SIZE]
        return SimpleNamespace(rows=[SimpleNamespace(to_dict=lambda doc_id=doc_id: dict(self.docs[doc_id])) for doc_id in ids])

    def write(self, upsert_rows=(), patch_rows=()):
        self.log.append(self.id)
        for row in upsert_rows:
            self.docs[row["id"]] = dict(row)
        for patch in patch_rows:
            self.docs[patch["id"]].update(patch)


class RunTest(unittest.TestCase):
    def namespaces(self):
        log = []
        return FakeNamespace("edges", PlanTest.edges, log), FakeNamespace("schools", PlanTest.schools, log), log

    def test_dry_run_writes_nothing(self):
        edges, schools, log = self.namespaces()
        with tempfile.TemporaryDirectory() as tmp:
            report = run(edges, schools, PlanTest.rows, apply=False, backup_dir=Path(tmp))
            self.assertEqual(list(Path(tmp).iterdir()), [])
        self.assertEqual((report["school_patches"], report["restores"]), (3, 0))
        self.assertEqual(log, [])

    def test_apply_backs_up_writes_and_a_rerun_finds_nothing(self):
        edges, schools, log = self.namespaces()
        with tempfile.TemporaryDirectory() as tmp:
            report = run(edges, schools, PlanTest.rows, apply=True, backup_dir=Path(tmp))
            self.assertEqual(len(Path(report["backup"]).read_text().splitlines()), 4)
            rerun = run(edges, schools, PlanTest.rows, apply=False, backup_dir=Path(tmp))
        self.assertEqual(report["remaining"], {"school_patches": 0, "restores": 0})
        self.assertEqual((rerun["school_patches"], rerun["restores"]), (0, 0))
        self.assertEqual(set(log), {"schools"})
        self.assertEqual(sorted(schools.docs), ["empty", "kth", "kungliga", "mit"])
        self.assertEqual(schools.docs["kungliga"]["parent_id"], "kth")
        self.assertEqual(schools.docs["kungliga"]["person_count"], 2)

    def test_a_corrected_mapping_undoes_the_merge(self):
        edges, schools, _ = self.namespaces()
        corrected = {**PlanTest.rows, "kungliga": {**PlanTest.rows["kungliga"], "parent_id": None}}
        with tempfile.TemporaryDirectory() as tmp:
            run(edges, schools, PlanTest.rows, apply=True, backup_dir=Path(tmp))
            report = run(edges, schools, corrected, apply=True, backup_dir=Path(tmp))
        self.assertEqual(report["remaining"], {"school_patches": 0, "restores": 0})
        self.assertIsNone(schools.docs["kungliga"]["parent_id"])
        self.assertEqual(schools.docs["kth"]["school_name"], "KTH (Kungliga)")
        self.assertEqual(schools.docs["kth"]["person_count"], 2)


if __name__ == "__main__":
    unittest.main()
