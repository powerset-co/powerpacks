from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRIMITIVES = ROOT / "packs/search/primitives"
LIB = PRIMITIVES / "lib"
SHARED = PRIMITIVES / "shared"
TURBOPUFFER = PRIMITIVES / "turbopuffer"
for _path in [LIB, SHARED, TURBOPUFFER]:
    sys.path.insert(0, str(_path))

SCRIPT = TURBOPUFFER / "turbopuffer_resolve_education.py"


def load_module():
    spec = importlib.util.spec_from_file_location("turbopuffer_resolve_education", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


resolve_education = load_module()


class ResolveEducationTests(unittest.TestCase):
    def test_affiliated_school_queries_for_root_university(self) -> None:
        self.assertEqual(resolve_education.affiliated_school_queries("Stanford University"), ["stanford"])
        self.assertEqual(resolve_education.affiliated_school_queries("Harvard University"), ["harvard"])

    def test_affiliated_school_queries_keeps_specific_school_specific(self) -> None:
        self.assertEqual(resolve_education.affiliated_school_queries("Stanford Graduate School of Business"), [])
        self.assertEqual(resolve_education.affiliated_school_queries("University of Pennsylvania"), [])

    def test_affiliated_candidate_requires_same_leading_token(self) -> None:
        self.assertTrue(resolve_education.is_affiliated_candidate(["stanford"], "Stanford Graduate School of Business"))
        self.assertTrue(resolve_education.is_affiliated_candidate(["stanford"], "Stanford Continuing Studies"))
        self.assertFalse(resolve_education.is_affiliated_candidate(["stanford"], "Samford University"))

    def test_without_alias_drops_the_alias_parenthetical(self) -> None:
        self.assertEqual(resolve_education.without_alias("University of California, Los Angeles (UCLA / Bruins)"),
                         "University of California, Los Angeles")
        self.assertEqual(resolve_education.without_alias("KTH Royal Institute of Technology"), "KTH Royal Institute of Technology")

    def test_exact_word_beats_a_larger_school_that_only_starts_with_it(self) -> None:
        schools = [Row("huji", school_name="Hebrew University (Tumor Immunology)", person_count=89),
                   Row("tum", school_name="Technical University of Munich (TUM School of Management)", person_count=73)]
        with FakeNamespaces({"schools": schools, "education": []}):
            result = asyncio.run(resolve_education.resolve_name("TUM", limit=5000))
        self.assertEqual(result["resolved_ids"], ["tum"])

    def test_an_alias_does_not_join_another_schools_family(self) -> None:
        schools = [Row("columbia", school_name="Columbia University (Columbia Business School)", person_count=900),
                   Row("gwu", school_name="George Washington University (Columbian College)", person_count=300),
                   Row("sc", school_name="University of South Carolina (Columbia Campus)", person_count=70),
                   Row("barnard", school_name="Barnard College, Columbia University", person_count=20)]
        with FakeNamespaces({"schools": schools, "education": []}):
            result = asyncio.run(resolve_education.resolve_name("Columbia University", limit=5000))
        self.assertEqual(result["resolved_ids"], ["columbia", "barnard"])

    def test_resolve_name_adds_the_ids_people_are_filed_under(self) -> None:
        # The schools index knows KTH only by its Swedish name under the id most people are filed under;
        # the English name is a second id with a few people. The query must resolve both.
        schools = [Row("368edcd5", school_name="KTH Royal Institute of Technology", person_count=32)]
        people = [Row(f"e{i}", canonical_education_id="0819246e", school_name="KTH Royal Institute of Technology")
                  for i in range(37)]
        people += [Row(f"f{i}", canonical_education_id="368edcd5", school_name="KTH Royal Institute of Technology")
                   for i in range(3)]
        with FakeNamespaces({"schools": schools, "education": people}) as fake:
            result = asyncio.run(resolve_education.resolve_name("KTH Royal Institute of Technology", limit=5000))
        self.assertEqual(result["resolved_ids"], ["368edcd5", "0819246e"])
        self.assertEqual(fake.queries["education"], [("school_name", "In", ["KTH Royal Institute of Technology"])])
        added = result["candidates"][-1]
        self.assertEqual((added["id"], added["matched_rows"], added["included"], added["from_people"]),
                         ("0819246e", 37, True, True))

    def test_resolve_name_finds_a_school_missing_from_the_schools_index(self) -> None:
        people = [Row(f"e{i}", canonical_education_id="5a42ef6e", school_name="Reichman University") for i in range(6)]
        with FakeNamespaces({"schools": [], "education": people}):
            result = asyncio.run(resolve_education.resolve_name("Reichman University", limit=5000))
        self.assertEqual(result["resolved_ids"], ["5a42ef6e"])
        self.assertEqual(result["candidates"][0]["name"], "Reichman University")

    def test_resolve_name_walks_every_page_so_a_minority_id_is_not_crowded_out(self) -> None:
        # Ids sort before the majority rows on purpose: "a…" rows fill the first pages, the lone "z" row
        # for the second id sits past them. One capped page would never see it.
        people = [Row(f"a{i:03d}", canonical_education_id="majority", school_name="Harvard University") for i in range(10)]
        people.append(Row("z", canonical_education_id="minority", school_name="Harvard University"))
        schools = [Row("majority", school_name="Harvard University", person_count=9000)]
        with FakeNamespaces({"schools": schools, "education": people}) as fake:
            result = asyncio.run(resolve_education.resolve_name("Harvard University", limit=4))
        self.assertEqual(result["resolved_ids"], ["majority", "minority"])
        self.assertEqual(len(fake.queries["education"]), 3)  # 4 + 4 + 3 rows
        self.assertEqual(fake.queries["education"][1],
                         ("And", [("school_name", "In", ["Harvard University"]), ("id", "Gt", "a003")]))

    def test_resolve_name_keeps_one_id_when_people_agree_with_the_index(self) -> None:
        schools = [Row("acd99729", school_name="Stanford University (Stanford)", person_count=5000)]
        people = [Row(f"e{i}", canonical_education_id="acd99729", school_name="Stanford University") for i in range(4)]
        with FakeNamespaces({"schools": schools, "education": people}) as fake:
            result = asyncio.run(resolve_education.resolve_name("Stanford University", limit=5000))
        self.assertEqual(result["resolved_ids"], ["acd99729"])
        self.assertEqual(fake.queries["education"], [("school_name", "In", ["Stanford University"])])


class Row:
    def __init__(self, row_id: str, **attrs: object) -> None:
        self.id = row_id
        self.model_extra = attrs


class _Response:
    def __init__(self, rows: list[Row]) -> None:
        self.rows = rows


class _Namespace:
    def __init__(self, logical: str, rows: list[Row], queries: dict[str, list[tuple]]) -> None:
        self.logical, self.rows, self.queries = logical, rows, queries

    def query(self, *, filters: tuple, top_k: int, include_attributes: list[str], consistency: dict,
              rank_by: tuple | None = None) -> _Response:
        self.queries.setdefault(self.logical, []).append(filters)
        if self.logical != "education":
            return _Response(self.rows)
        by_name, after = (filters[1][0], filters[1][1][2]) if filters[0] == "And" else (filters, None)
        names = set(by_name[2])
        rows = sorted((row for row in self.rows if row.model_extra["school_name"] in names), key=lambda row: row.id)
        rows = [row for row in rows if after is None or row.id > after]
        return _Response(rows[:top_k])


class FakeNamespaces:
    """Stands in for turbopuffer_backend.namespace: rows per logical namespace, every filter recorded."""

    def __init__(self, rows: dict[str, list[Row]]) -> None:
        self.rows = rows
        self.queries: dict[str, list[tuple]] = {}

    def __enter__(self) -> "FakeNamespaces":
        self._real = resolve_education.turbopuffer_backend.namespace
        resolve_education.turbopuffer_backend.namespace = lambda logical: _Namespace(logical, self.rows[logical], self.queries)
        return self

    def __exit__(self, *exc: object) -> None:
        resolve_education.turbopuffer_backend.namespace = self._real


if __name__ == "__main__":
    unittest.main()
