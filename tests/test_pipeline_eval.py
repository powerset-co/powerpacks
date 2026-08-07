"""Offline tests for the typed pipeline evaluator entry point."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from packs.search.evals import run_pipeline_eval

ROOT = Path(__file__).resolve().parents[1]


class PipelineEvalCliTests(unittest.TestCase):
    def run_eval(self, recall_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
        output_root = ROOT / ".powerpacks" / "search-runs" / f"pipeline-eval-test-{uuid.uuid4().hex}"
        self.addCleanup(shutil.rmtree, output_root, True)
        return subprocess.run(
            [
                sys.executable,
                str(Path(run_pipeline_eval.__file__)),
                "--recall-dir",
                str(recall_dir),
                "--output-root",
                str(output_root),
                *args,
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_list_and_dry_run_use_deterministic_typed_cases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "founders_basic.yaml").write_text("query: current founders in Argentina\nexpected_count: 1\n")
            listed = subprocess.run(
                [sys.executable, str(Path(run_pipeline_eval.__file__)), "--recall-dir", str(root), "--list"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertEqual(json.loads(listed.stdout)[0]["bucket"], "founders")

            dry = subprocess.run(
                [
                    sys.executable,
                    str(Path(run_pipeline_eval.__file__)),
                    "--recall-dir",
                    str(root),
                    "--dry-run",
                    "--set-id",
                    "set-1",
                    "--operator-id",
                    "operator-1",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(dry.returncode, 0, dry.stderr)
            output = json.loads(dry.stdout)
            self.assertEqual(output["mode"], "dry-run")
            spec = output["cases"][0]["search_spec"]
            self.assertEqual(spec["corpus"]["set_id"], "set-1")
            self.assertEqual(spec["corpus"]["operator_ids"], ["operator-1"])
            self.assertEqual(spec["person_filters"]["countries"], ["Argentina"])
            self.assertNotIn("skip_llm", output)

    def test_execution_rejects_output_outside_canonical_search_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recall = root / "recall"
            recall.mkdir()
            proc = subprocess.run(
                [
                    sys.executable,
                    str(Path(run_pipeline_eval.__file__)),
                    "--recall-dir",
                    str(recall),
                    "--output-root",
                    str(root / "outside"),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertIn("must be under .powerpacks/search-runs", proc.stderr)
            self.assertFalse((root / "outside").exists())

    def test_required_unsupported_case_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recall = Path(tmp)
            (recall / "location_europe.yaml").write_text("query: founders in Europe\nexpected_count: 1\n")
            proc = self.run_eval(recall)
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertIn('"status": "unsupported_case"', proc.stdout)
            self.assertIn("macro_regions", proc.stdout)

    def test_required_unsupported_capability_exits_nonzero(self) -> None:
        import duckdb

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recall = root / "recall"
            recall.mkdir()
            (recall / "skills_python.yaml").write_text("query: founders with Python\nexpected_count: 1\n")
            db = root / "empty.duckdb"
            duckdb.connect(str(db)).close()
            proc = self.run_eval(recall, "--backend", "local", "--db-path", str(db))
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertIn('"status": "unsupported_capability"', proc.stdout)
            self.assertIn("tech_skills", proc.stdout)

    def test_explicitly_ignored_case_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recall = Path(tmp)
            (recall / "founders_unscored.yaml").write_text("query: founders\n")
            proc = self.run_eval(recall)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn('"status": "ignored"', proc.stdout)


if __name__ == "__main__":
    unittest.main()
