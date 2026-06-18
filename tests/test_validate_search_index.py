import importlib.util
import tempfile
import unittest
from pathlib import Path

import duckdb


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
