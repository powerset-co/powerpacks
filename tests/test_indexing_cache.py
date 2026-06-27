import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PEOPLE = ROOT / "tests/fixtures/indexing/people.csv"
PIPELINE = ROOT / "packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py"


def run_json(args: list[str]) -> dict:
    proc = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"command failed: {proc.stderr}\nstdout={proc.stdout}")
    return json.loads(proc.stdout)


class IndexingCacheTests(unittest.TestCase):
    def test_second_run_hits_cache_and_rewrites_current_run_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / ".powerpacks/search-index"
            first = run_json([str(PIPELINE), "run", "--output-dir", str(out), "--run-id", "first", "--input", str(FIXTURE_PEOPLE), "--force"])
            second = run_json([str(PIPELINE), "run", "--output-dir", str(out), "--run-id", "second", "--input", str(FIXTURE_PEOPLE)])
            self.assertFalse(first["cache_hit"])
            self.assertTrue(second["cache_hit"])
            run_dir = out / "second"
            ledger_text = (run_dir / "ledger.json").read_text(encoding="utf-8")
            manifest_text = (run_dir / "index-manifest.json").read_text(encoding="utf-8")
            ledger = json.loads(ledger_text)
            manifest = json.loads(manifest_text)
            self.assertEqual(ledger["run_id"], "second")
            self.assertEqual(ledger["run_dir"], str(run_dir))
            self.assertEqual(manifest["run_id"], "second")
            self.assertEqual(manifest["run_dir"], str(run_dir))
            self.assertEqual(manifest["status"], "ready")
            self.assertIn(str(run_dir), manifest_text)
            self.assertNotIn(str(out / "first"), ledger_text)
            self.assertNotIn(str(out / "first"), manifest_text)
            self.assertTrue((run_dir / "records/people.records.jsonl").exists())
            self.assertTrue((run_dir / "summaries/summary_records.jsonl").exists())
            self.assertTrue((out / "local-search.duckdb").exists())

    def test_cache_hit_rebuilds_when_cached_duckdb_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / ".powerpacks/search-index"
            first = run_json([str(PIPELINE), "run", "--output-dir", str(out), "--run-id", "first", "--input", str(FIXTURE_PEOPLE), "--force"])
            cache_dir = Path(first["cache_dir"])
            (cache_dir / "local-search.duckdb").unlink()
            second = run_json([str(PIPELINE), "run", "--output-dir", str(out), "--run-id", "second", "--input", str(FIXTURE_PEOPLE)])
            self.assertFalse(second["cache_hit"])
            self.assertTrue(second["ready"])

    def test_cache_hit_rebuilds_when_cached_checksum_is_mismatched(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / ".powerpacks/search-index"
            first = run_json([str(PIPELINE), "run", "--output-dir", str(out), "--run-id", "first", "--input", str(FIXTURE_PEOPLE), "--force"])
            cache_dir = Path(first["cache_dir"])
            (cache_dir / "local-search.duckdb.sha256").write_text("bad\n", encoding="utf-8")
            second = run_json([str(PIPELINE), "run", "--output-dir", str(out), "--run-id", "second", "--input", str(FIXTURE_PEOPLE)])
            self.assertFalse(second["cache_hit"])
            self.assertTrue(second["ready"])

    def test_status_not_ready_when_materialized_duckdb_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / ".powerpacks/search-index"
            run_json([str(PIPELINE), "run", "--output-dir", str(out), "--run-id", "status", "--input", str(FIXTURE_PEOPLE), "--force"])
            run_dir = out / "status"
            (run_dir / "local-search.duckdb").unlink()
            status = run_json([str(PIPELINE), "status", "--ledger", str(run_dir / "ledger.json")])
            self.assertFalse(status["ready"])


if __name__ == "__main__":
    unittest.main()
