import subprocess
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PEOPLE = ROOT / "tests/fixtures/indexing/people.csv"
PIPELINE = ROOT / "packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py"
PARITY = ROOT / "packs/indexing/primitives/validate_index_parity/validate_index_parity.py"


def run_json(args: list[str]) -> dict:
    proc = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"command failed: {proc.stderr}\nstdout={proc.stdout}")
    return json.loads(proc.stdout)


class IndexParityTests(unittest.TestCase):
    def test_parity_reports_counts_checksums_and_people_csv_only_vectors_na(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / ".powerpacks/search-index"
            run_json([str(PIPELINE), "run", "--output-dir", str(out), "--run-id", "parity", "--input", str(FIXTURE_PEOPLE), "--force"])
            run_dir = out / "parity"
            report = run_json([str(PARITY), "run", "--people-csv", str(FIXTURE_PEOPLE), "--run-dir", str(run_dir), "--db", str(run_dir / "local-search.duckdb")])
            self.assertTrue(report["ok"], report.get("errors"))
            self.assertEqual(report["errors"], [])
            self.assertEqual(report["people_csv"]["rows"], 4)
            self.assertEqual(report["profiles"]["rows"], 4)
            self.assertEqual(report["records"]["people.records.jsonl"]["rows"], 3)
            self.assertGreater(report["tables"]["local_people_positions"]["rows"], 0)
            self.assertIsNotNone(report["tables"]["local_profiles"]["checksum"])
            self.assertEqual(report["vector_parity"]["status"], "not_applicable")

    def test_parity_fails_on_missing_duckdb_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / ".powerpacks/search-index"
            run_json([str(PIPELINE), "run", "--output-dir", str(out), "--run-id", "bad-parity", "--input", str(FIXTURE_PEOPLE), "--force"])
            run_dir = out / "bad-parity"
            import duckdb

            con = duckdb.connect(str(run_dir / "local-search.duckdb"))
            try:
                con.execute("delete from local_people_positions")
            finally:
                con.close()
            proc = subprocess.run(
                [sys.executable, str(PARITY), "run", "--people-csv", str(FIXTURE_PEOPLE), "--run-dir", str(run_dir), "--db", str(run_dir / "local-search.duckdb")],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0)
            report = json.loads(proc.stdout)
            self.assertFalse(report["ok"])
            self.assertTrue(any("people.records.jsonl" in error for error in report["errors"]))

    def test_parity_fails_on_same_row_count_duckdb_content_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / ".powerpacks/search-index"
            run_json([str(PIPELINE), "run", "--output-dir", str(out), "--run-id", "corrupt-parity", "--input", str(FIXTURE_PEOPLE), "--force"])
            run_dir = out / "corrupt-parity"
            import duckdb

            con = duckdb.connect(str(run_dir / "local-search.duckdb"))
            try:
                before = con.execute("select count(*) from local_people_positions").fetchone()[0]
                con.execute("update local_people_positions set position_title = 'CORRUPTED TITLE' where id = (select id from local_people_positions limit 1)")
                after = con.execute("select count(*) from local_people_positions").fetchone()[0]
            finally:
                con.close()
            self.assertEqual(before, after)
            proc = subprocess.run(
                [sys.executable, str(PARITY), "run", "--people-csv", str(FIXTURE_PEOPLE), "--run-dir", str(run_dir), "--db", str(run_dir / "local-search.duckdb")],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0)
            report = json.loads(proc.stdout)
            self.assertFalse(report["ok"])
            self.assertIn("content checksum mismatch: local_people_positions", report["errors"])

    def test_parity_fails_on_same_row_count_duckdb_company_id_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / ".powerpacks/search-index"
            run_json([str(PIPELINE), "run", "--output-dir", str(out), "--run-id", "corrupt-company-parity", "--input", str(FIXTURE_PEOPLE), "--force"])
            run_dir = out / "corrupt-company-parity"
            import duckdb

            con = duckdb.connect(str(run_dir / "local-search.duckdb"))
            try:
                before = con.execute("select count(*) from local_people_positions").fetchone()[0]
                con.execute("update local_people_positions set company_id = 'CORRUPTED-COMPANY' where id = (select id from local_people_positions limit 1)")
                after = con.execute("select count(*) from local_people_positions").fetchone()[0]
            finally:
                con.close()
            self.assertEqual(before, after)
            proc = subprocess.run(
                [sys.executable, str(PARITY), "run", "--people-csv", str(FIXTURE_PEOPLE), "--run-dir", str(run_dir), "--db", str(run_dir / "local-search.duckdb")],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0)
            report = json.loads(proc.stdout)
            self.assertFalse(report["ok"])
            self.assertIn("content checksum mismatch: local_people_positions", report["errors"])

    def test_parity_fails_on_same_row_count_duckdb_summary_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / ".powerpacks/search-index"
            run_json([str(PIPELINE), "run", "--output-dir", str(out), "--run-id", "corrupt-summary-parity", "--input", str(FIXTURE_PEOPLE), "--force"])
            run_dir = out / "corrupt-summary-parity"
            import duckdb

            con = duckdb.connect(str(run_dir / "local-search.duckdb"))
            try:
                before = con.execute("select count(*) from local_summaries").fetchone()[0]
                con.execute("update local_summaries set summary = 'CORRUPTED SUMMARY' where id = (select id from local_summaries limit 1)")
                after = con.execute("select count(*) from local_summaries").fetchone()[0]
            finally:
                con.close()
            self.assertEqual(before, after)
            proc = subprocess.run(
                [sys.executable, str(PARITY), "run", "--people-csv", str(FIXTURE_PEOPLE), "--run-dir", str(run_dir), "--db", str(run_dir / "local-search.duckdb")],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0)
            report = json.loads(proc.stdout)
            self.assertFalse(report["ok"])
            self.assertIn("content checksum mismatch: local_summaries", report["errors"])

    def test_parity_fails_with_structured_report_on_missing_duckdb_table(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / ".powerpacks/search-index"
            run_json([str(PIPELINE), "run", "--output-dir", str(out), "--run-id", "missing-table-parity", "--input", str(FIXTURE_PEOPLE), "--force"])
            run_dir = out / "missing-table-parity"
            import duckdb

            con = duckdb.connect(str(run_dir / "local-search.duckdb"))
            try:
                con.execute("drop table local_summaries")
            finally:
                con.close()
            proc = subprocess.run(
                [sys.executable, str(PARITY), "run", "--people-csv", str(FIXTURE_PEOPLE), "--run-dir", str(run_dir), "--db", str(run_dir / "local-search.duckdb")],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(proc.stderr, "")
            report = json.loads(proc.stdout)
            self.assertFalse(report["ok"])
            self.assertTrue(report["tables"]["local_summaries"]["missing"])
            self.assertIsNone(report["content_parity"]["actual"]["local_summaries"])
            self.assertIn("missing DuckDB table: local_summaries", report["errors"])


if __name__ == "__main__":
    unittest.main()
