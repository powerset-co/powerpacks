import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/compare-local-duckdb.py"


def load_module():
    spec = importlib.util.spec_from_file_location("compare_local_duckdb", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


compare_local_duckdb = load_module()


def create_database(path: Path, rows: list[tuple[str, str, list[float]]]) -> None:
    con = duckdb.connect(str(path))
    try:
        con.execute(
            """
            CREATE TABLE local_people_positions (
                id VARCHAR,
                position_id VARCHAR,
                person_id VARCHAR,
                base_id VARCHAR,
                position_title VARCHAR,
                company_id VARCHAR,
                allowed_operator_ids VARCHAR[],
                vector DOUBLE[]
            )
            """
        )
        con.executemany(
            "INSERT INTO local_people_positions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(row_id, row_id, row_id, row_id, title, "company-1", ["op-test"], vector) for row_id, title, vector in rows],
        )
    finally:
        con.close()


class CompareLocalDuckDBTests(unittest.TestCase):
    def test_equivalent_databases_ignore_physical_row_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            left = root / "left.duckdb"
            right = root / "right.duckdb"
            rows = [("p1", "Engineer", [1.0, 0.0]), ("p2", "Designer", [0.0, 1.0])]
            create_database(left, rows)
            create_database(right, list(reversed(rows)))

            report = compare_local_duckdb.compare_databases(left, right)

            self.assertTrue(report["match"])
            self.assertTrue(report["tables"]["local_people_positions"]["schema_match"])
            self.assertTrue(report["tables"]["local_people_positions"]["match"])
            self.assertTrue(report["search"]["match"])

    def test_equivalent_duplicate_ids_ignore_only_tied_physical_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            left = root / "left.duckdb"
            right = root / "right.duckdb"
            rows = [("dup", "Engineer", [1.0, 0.0]), ("dup", "Designer", [0.0, 1.0])]
            create_database(left, rows)
            create_database(right, list(reversed(rows)))

            report = compare_local_duckdb.compare_databases(left, right)

            self.assertTrue(report["tables"]["local_people_positions"]["match"])
            self.assertTrue(report["search"]["match"])
            self.assertTrue(report["match"])

    def test_vector_change_is_a_content_and_search_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            left = root / "left.duckdb"
            right = root / "right.duckdb"
            create_database(left, [("p1", "Engineer", [1.0, 0.0]), ("p2", "Designer", [0.0, 1.0])])
            create_database(right, [("p1", "Engineer", [0.0, 1.0]), ("p2", "Designer", [0.0, 1.0])])

            report = compare_local_duckdb.compare_databases(left, right)

            self.assertFalse(report["match"])
            table = report["tables"]["local_people_positions"]
            self.assertEqual(table["row_count"], {"left": 2, "right": 2})
            self.assertNotEqual(table["content_checksum"]["left"], table["content_checksum"]["right"])

    def test_logical_json_equivalence_allows_struct_null_fill_and_json_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            left = root / "left.duckdb"
            right = root / "right.duckdb"
            left_con = duckdb.connect(str(left))
            right_con = duckdb.connect(str(right))
            try:
                left_con.execute("CREATE TABLE local_person_profiles (id VARCHAR, payload STRUCT(name VARCHAR, missing VARCHAR))")
                left_con.execute("INSERT INTO local_person_profiles VALUES ('p1', {'name': 'Arthur', 'missing': NULL})")
                right_con.execute("CREATE TABLE local_person_profiles (payload JSON, id VARCHAR)")
                right_con.execute("INSERT INTO local_person_profiles VALUES ('{\"name\":\"Arthur\"}', 'p1')")
            finally:
                left_con.close()
                right_con.close()

            report = compare_local_duckdb.compare_databases(left, right)

            self.assertTrue(report["match"])
            self.assertFalse(report["physical_schema_match"])
            self.assertTrue(report["tables"]["local_person_profiles"]["columns_match"])
            self.assertTrue(report["tables"]["local_person_profiles"]["match"])

    def test_cli_returns_nonzero_and_writes_report_on_schema_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            left = root / "left.duckdb"
            right = root / "right.duckdb"
            output = root / "report.json"
            create_database(left, [("p1", "Engineer", [1.0, 0.0])])
            create_database(right, [("p1", "Engineer", [1.0, 0.0])])
            con = duckdb.connect(str(right))
            try:
                con.execute("ALTER TABLE local_people_positions ADD COLUMN extra VARCHAR")
            finally:
                con.close()

            proc = subprocess.run(
                [sys.executable, str(SCRIPT), str(left), str(right), "--output", str(output)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 1)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "mismatch")
            self.assertFalse(report["tables"]["local_people_positions"]["schema_match"])


if __name__ == "__main__":
    unittest.main()
