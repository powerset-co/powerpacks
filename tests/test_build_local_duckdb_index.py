import json
import subprocess
import sys
import csv
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PEOPLE = ROOT / "tests/fixtures/indexing/people.csv"
PIPELINE = ROOT / "packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py"
VALIDATE = ROOT / "packs/indexing/primitives/validate_local_search_index/validate_local_search_index.py"


def run_json(args: list[str]) -> dict:
    proc = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"command failed: {proc.stderr}\nstdout={proc.stdout}")
    return json.loads(proc.stdout)


class BuildLocalDuckDBIndexTests(unittest.TestCase):
    def test_pipeline_builds_searchable_duckdb_all_namespaces(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / ".powerpacks/search-index"
            result = run_json([str(PIPELINE), "run", "--output-dir", str(out), "--run-id", "duckdb", "--input", str(FIXTURE_PEOPLE), "--force"])
            self.assertTrue(result["ready"])
            run_dir = out / "duckdb"
            db = run_dir / "local-search.duckdb"
            self.assertTrue(db.exists())

            import duckdb

            con = duckdb.connect(str(db), read_only=True)
            try:
                counts = {table: con.execute(f"select count(*) from {table}").fetchone()[0] for table in ["local_people_positions", "local_summaries", "local_people_education", "local_education", "local_profiles"]}
            finally:
                con.close()
            self.assertEqual(counts["local_profiles"], 4)
            self.assertEqual(counts["local_people_positions"], 3)
            self.assertGreater(counts["local_summaries"], 0)
            self.assertGreater(counts["local_people_education"], 0)
            self.assertGreater(counts["local_education"], 0)

            validation = run_json([str(VALIDATE), "run", "--db", str(db)])
            self.assertTrue(validation["duckdb_opened"])
            self.assertTrue(validation["namespace_probes_ok"])
            for namespace, probe in validation["probes"].items():
                self.assertTrue(probe["ok"], namespace)
                self.assertGreater(probe["rows"], 0, namespace)
            self.assertTrue(validation["probes"]["people"]["base_ids"])
            self.assertTrue(validation["probes"]["summaries"]["base_ids"])
            self.assertIn(validation["probes"]["education"]["seed"]["degree_normalized"], {"Bachelors", "Masters"})
            self.assertTrue(validation["probes"]["education"]["base_ids"])
            self.assertIn(validation["probes"]["schools"]["seed"]["school_name"], validation["probes"]["schools"]["school_names"])

    def test_pipeline_handles_people_without_education_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            people = Path(td) / "people-no-education.csv"
            with FIXTURE_PEOPLE.open(newline="", encoding="utf-8") as src, people.open("w", newline="", encoding="utf-8") as dst:
                reader = csv.DictReader(src)
                writer = csv.DictWriter(dst, fieldnames=reader.fieldnames)
                writer.writeheader()
                for row in reader:
                    row["education"] = "[]"
                    writer.writerow(row)
            out = Path(td) / ".powerpacks/search-index"
            result = run_json([str(PIPELINE), "run", "--output-dir", str(out), "--run-id", "no-edu", "--input", str(people), "--force"])
            self.assertTrue(result["ready"])
            validation = run_json([str(VALIDATE), "run", "--db", str(out / "no-edu/local-search.duckdb")])
            self.assertTrue(validation["namespace_probes_ok"])
            self.assertTrue(validation["probes"]["education"]["skipped_empty"])
            self.assertTrue(validation["probes"]["schools"]["skipped_empty"])

    def test_pipeline_with_zero_core_people_positions_is_not_ready_or_cached(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            people = Path(td) / "people-no-work.csv"
            with FIXTURE_PEOPLE.open(newline="", encoding="utf-8") as src, people.open("w", newline="", encoding="utf-8") as dst:
                reader = csv.DictReader(src)
                writer = csv.DictWriter(dst, fieldnames=reader.fieldnames)
                writer.writeheader()
                for row in reader:
                    row["work_experiences"] = "[]"
                    writer.writerow(row)

            out = Path(td) / ".powerpacks/search-index"
            result = run_json([str(PIPELINE), "run", "--output-dir", str(out), "--run-id", "no-work", "--input", str(people), "--force"])

            self.assertFalse(result["ready"])
            self.assertIsNone(result["cache_dir"])
            self.assertEqual(result["latest"], {})
            self.assertFalse((out / "local-search.duckdb").exists())
            self.assertFalse((out / "latest-manifest.json").exists())
            self.assertFalse((out / "cache").exists())

            validation = result["counts"]["validate_local_search_index"]
            self.assertFalse(validation["namespace_probes_ok"])
            self.assertTrue(validation["probes"]["people"]["required_empty"])
            self.assertEqual(validation["probes"]["people"]["error"], "core people namespace has no searchable position rows")

            status = run_json([str(PIPELINE), "status", "--ledger", str(out / "no-work/ledger.json")])
            self.assertFalse(status["ready"])
            self.assertEqual(status["manifest"]["status"], "partial")


if __name__ == "__main__":
    unittest.main()
