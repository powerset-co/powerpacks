import importlib.util
import tempfile
import unittest
from pathlib import Path

import duckdb

from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS
from packs.shared.csv_io import CsvIO


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packs/indexing/primitives/validate_search_index" / "validate_search_index.py"


def load_module():
    spec = importlib.util.spec_from_file_location("validate_search_index", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


vsi = load_module()


def build_db(path: Path, *, tables: dict[str, int], profile_table: str = "local_person_profiles") -> None:
    """Create a DuckDB with the given tables, each holding `count` id rows.

    `tables` maps table name -> row count. The profile table is added unless its
    name is passed as None.
    """
    con = duckdb.connect(str(path))
    try:
        spec = dict(tables)
        if profile_table is not None:
            spec.setdefault(profile_table, spec.get(profile_table, 1))
        for name, count in spec.items():
            con.execute(f'create table "{name}" (id VARCHAR)')
            for i in range(count):
                con.execute(f'insert into "{name}" values (?)', [f"{name}-{i}"])
    finally:
        con.close()


# All required tables populated; build a fully-healthy index for the base case.
HEALTHY = {
    "local_person_profiles": 3,
    "local_people_positions": 9,
    "local_summaries": 3,
    "local_companies": 5,
    "local_people_education": 4,
    "local_education": 2,
    "local_company_signals": 1,
}


class ValidateSearchIndexTest(unittest.TestCase):
    def _validate(self, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "local-search.duckdb"
            build_db(db, **kwargs)
            return vsi.validate(db)

    def test_healthy_index_is_ok(self):
        payload = self._validate(tables=HEALTHY)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["errors"], [])
        self.assertEqual(payload["warnings"], [])
        self.assertEqual(payload["total_people"], 3)
        self.assertEqual(payload["profile_table"], "local_person_profiles")

    def test_missing_db_is_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = vsi.validate(Path(tmp) / "does-not-exist.duckdb")
        self.assertEqual(payload["status"], "missing")
        self.assertTrue(payload["errors"])

    def test_empty_required_table_fails(self):
        tables = dict(HEALTHY)
        tables["local_summaries"] = 0
        payload = self._validate(tables=tables)
        self.assertEqual(payload["status"], "fail")
        self.assertTrue(any("local_summaries" in e for e in payload["errors"]))

    def test_missing_required_table_fails(self):
        tables = dict(HEALTHY)
        tables.pop("local_companies")
        payload = self._validate(tables=tables)
        self.assertEqual(payload["status"], "fail")
        self.assertTrue(any("local_companies" in e and "missing" in e for e in payload["errors"]))

    def test_zero_profiles_fails(self):
        tables = dict(HEALTHY)
        tables["local_person_profiles"] = 0
        payload = self._validate(tables=tables, profile_table="local_person_profiles")
        self.assertEqual(payload["status"], "fail")
        self.assertEqual(payload["total_people"], 0)

    def test_missing_profile_table_fails(self):
        # Omit any profile table entirely.
        tables = {k: v for k, v in HEALTHY.items() if k != "local_person_profiles"}
        payload = self._validate(tables=tables, profile_table=None)
        self.assertEqual(payload["status"], "fail")
        self.assertIsNone(payload["profile_table"])

    def test_alternate_profile_table_name_accepted(self):
        tables = {k: v for k, v in HEALTHY.items() if k != "local_person_profiles"}
        tables["local_people_profiles"] = 3
        payload = self._validate(tables=tables, profile_table="local_people_profiles")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["profile_table"], "local_people_profiles")

    def test_empty_optional_table_is_warning_not_failure(self):
        tables = dict(HEALTHY)
        tables["local_people_education"] = 0
        payload = self._validate(tables=tables)
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(any("local_people_education" in w for w in payload["warnings"]))

    def test_empty_info_table_does_not_warn(self):
        # local_company_signals is expected-empty in this flow: report it, never warn.
        tables = dict(HEALTHY)
        tables["local_company_signals"] = 0
        payload = self._validate(tables=tables)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["warnings"], [])
        signals = next(t for t in payload["tables"] if t["name"] == "local_company_signals")
        self.assertEqual(signals["tier"], "info")

    def test_missing_optional_table_is_warning_not_failure(self):
        tables = dict(HEALTHY)
        tables.pop("local_education")
        payload = self._validate(tables=tables)
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(any("local_education" in w and "missing" in w for w in payload["warnings"]))


if __name__ == "__main__":
    unittest.main()


class WorkHistoryReachesTheIndexTest(unittest.TestCase):
    """A person with work history in merged/people.csv must have positions in the index."""

    def _validate(self, positions_for: list[str]):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "local-search.duckdb"
            build_db(db, tables=HEALTHY)
            con = duckdb.connect(str(db))
            con.execute("alter table local_people_positions add column base_id VARCHAR")
            for person_id in positions_for:
                con.execute("insert into local_people_positions values (?, ?)", [f"pos-{person_id}", person_id])
            con.close()
            people = Path(tmp) / "people.csv"
            blank = {column: "" for column in PEOPLE_SCHEMA_COLUMNS}
            CsvIO.write_dict_rows(people, PEOPLE_SCHEMA_COLUMNS, [
                {**blank, "id": "p-jordan", "public_identifier": "jordan-bravo", "work_experiences": '[{"title": "Founder"}]'},
                {**blank, "id": "p-casey", "public_identifier": "casey-delta", "work_experiences": '[{"title": "Engineer"}]'},
                {**blank, "id": "p-riley", "work_experiences": "[]"},
                # No LinkedIn: the index re-derives this id, so it is not checked here.
                {**blank, "id": "candidate:email:morgan@example.com", "work_experiences": '[{"title": "CFO"}]'},
            ])
            return vsi.validate(db, people_csv=people)

    def test_people_whose_work_history_did_not_reach_the_index_fail(self):
        payload = self._validate(positions_for=["p-jordan"])
        self.assertEqual(payload["status"], "fail")
        self.assertEqual(payload["people_missing_positions"], 1)
        self.assertIn("1 of 2 people with work history have no positions in the index", payload["errors"])

    def test_every_person_with_work_history_has_positions(self):
        payload = self._validate(positions_for=["p-jordan", "p-casey"])
        self.assertEqual((payload["status"], payload["people_missing_positions"]), ("ok", 0))
