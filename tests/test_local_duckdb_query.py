import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import duckdb

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packs/search/primitives/local_duckdb_query" / "local_duckdb_query.py"


def load_module():
    spec = importlib.util.spec_from_file_location("local_duckdb_query", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


mod = load_module()


def make_db(path: Path) -> None:
    conn = duckdb.connect(str(path))
    conn.execute(
        "CREATE TABLE local_people_positions (person_id VARCHAR, position_title VARCHAR, company_id VARCHAR, "
        "start_date_epoch BIGINT, end_date_epoch BIGINT, vector DOUBLE[])"
    )
    conn.executemany(
        "INSERT INTO local_people_positions VALUES (?, ?, ?, ?, ?, ?)",
        [
            ["p1", "Engineer", "c1", 100, 200, [0.1] * 64],
            ["p1", "PM", "c2", 300, 0, [0.2] * 64],
            ["p2", "Engineer", "c1", 150, 0, [0.3] * 64],
        ],
    )
    conn.close()


class ValidateSelectOnlyTests(unittest.TestCase):
    def test_accepts_select_and_with(self):
        self.assertTrue(mod.validate_select_only("SELECT 1"))
        self.assertTrue(mod.validate_select_only("WITH t AS (SELECT 1) SELECT * FROM t"))

    def test_accepts_comments_and_trailing_semicolon(self):
        self.assertTrue(mod.validate_select_only("-- a comment\nSELECT 1;"))
        self.assertTrue(mod.validate_select_only("/* block */ SELECT 1"))

    def test_rejects_non_select(self):
        for sql in [
            "DELETE FROM local_people_positions",
            "UPDATE local_people_positions SET person_id = 'x'",
            "DROP TABLE local_people_positions",
            "CREATE TABLE t (x INT)",
            "COPY local_people_positions TO '/tmp/out.csv'",
            "ATTACH '/tmp/other.duckdb'",
            "PRAGMA database_list",
            "INSTALL httpfs",
        ]:
            with self.assertRaises(mod.QueryGuardError):
                mod.validate_select_only(sql)

    def test_rejects_multi_statement_and_empty(self):
        with self.assertRaises(mod.QueryGuardError):
            mod.validate_select_only("SELECT 1; SELECT 2")
        with self.assertRaises(mod.QueryGuardError):
            mod.validate_select_only("   -- nothing\n")


class QueryExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.duckdb"
        make_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli_output(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = mod.main(["--db", str(self.db_path), *argv])
        return code, buffer.getvalue()

    def run_cli(self, argv: list[str]) -> tuple[int, dict]:
        code, output = self.run_cli_output(argv)
        return code, json.loads(output)

    def test_schema_defaults_to_json(self):
        code, payload = self.run_cli(["schema"])
        self.assertEqual(code, 0)
        tables = {t["table"]: t for t in payload["tables"]}
        self.assertIn("local_people_positions", tables)
        self.assertEqual(tables["local_people_positions"]["row_count"], 3)
        column_names = [c["name"] for c in tables["local_people_positions"]["columns"]]
        self.assertIn("person_id", column_names)

    def test_schema_explicit_json(self):
        code, payload = self.run_cli(["schema", "--format", "json"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["tables"][0]["table"], "local_people_positions")

    def test_schema_markdown_is_deterministic(self):
        code, output = self.run_cli_output(["schema", "--format", "markdown"])
        self.assertEqual(code, 0)
        self.assertEqual(
            output,
            "# DuckDB schema\n\n"
            "## `local_people_positions`\n\n"
            "Row count: 3\n\n"
            "| Column | DuckDB type |\n"
            "| --- | --- |\n"
            "| `person_id` | `VARCHAR` |\n"
            "| `position_title` | `VARCHAR` |\n"
            "| `company_id` | `VARCHAR` |\n"
            "| `start_date_epoch` | `BIGINT` |\n"
            "| `end_date_epoch` | `BIGINT` |\n"
            "| `vector` | `DOUBLE[]` |\n",
        )

    def test_schema_rejects_invalid_format(self):
        with self.assertRaises(SystemExit) as raised:
            mod.main(["--db", str(self.db_path), "schema", "--format", "yaml"])
        self.assertEqual(raised.exception.code, 2)

    def test_query_rows_and_truncation(self):
        code, payload = self.run_cli(
            ["query", "--max-rows", "2", "--sql", "SELECT person_id FROM local_people_positions ORDER BY person_id"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["row_count"], 2)
        self.assertTrue(payload["truncated"])

    def test_aggregate_query(self):
        code, payload = self.run_cli(
            [
                "query",
                "--sql",
                "SELECT person_id, count(*) AS n FROM local_people_positions GROUP BY person_id HAVING count(*) >= 2",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(payload["rows"][0]["person_id"], "p1")

    def test_long_numeric_arrays_summarized(self):
        code, payload = self.run_cli(
            ["query", "--max-rows", "1", "--sql", "SELECT vector FROM local_people_positions"]
        )
        self.assertEqual(code, 0)
        self.assertIn("64 numeric values omitted", payload["rows"][0]["vector"])

    def test_write_statement_rejected_with_guard_exit_code(self):
        code, payload = self.run_cli(["query", "--sql", "DELETE FROM local_people_positions"])
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_kind"], "guard")
        conn = duckdb.connect(str(self.db_path), read_only=True)
        self.assertEqual(conn.execute("SELECT count(*) FROM local_people_positions").fetchone()[0], 3)
        conn.close()

    def test_sql_error_reported(self):
        code, payload = self.run_cli(["query", "--sql", "SELECT nope FROM local_people_positions"])
        self.assertEqual(code, 3)
        self.assertEqual(payload["error_kind"], "sql")

    def test_missing_db_reports_error(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = mod.main(["--db", str(Path(self.tmp.name) / "absent.duckdb"), "schema"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(buffer.getvalue())["status"], "error")

    def test_deprecated_environment_db_fails_even_with_explicit_db(self):
        buffer = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"POWERPACKS_LOCAL_SEARCH_DB": "legacy.duckdb"}),
            redirect_stdout(buffer),
        ):
            code = mod.main(["--db", str(self.db_path), "schema"])
        self.assertEqual(code, 1)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["status"], "error")
        self.assertIn("POWERPACKS_LOCAL_SEARCH_DB is deprecated", payload["error"])


if __name__ == "__main__":
    unittest.main()
