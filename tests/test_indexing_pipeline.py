import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PEOPLE = ROOT / "tests/fixtures/indexing/people.csv"
PIPELINE = ROOT / "packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py"
STEPS = [
    "flatten_people",
    "build_roles",
    "embed_role_positions",
    "build_company_corpus",
    "embed_companies",
    "build_education_corpus",
    "build_location_corpus",
    "build_people_records",
    "build_unified_profiles",
    "build_summary_records",
    "embed_summaries",
    "build_vectors",
    "validate_contracts",
]


def run_cli(*args: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(PIPELINE), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"command failed: {proc.stderr}\nstdout={proc.stdout}")
    return json.loads(proc.stdout)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class IndexingPipelineTests(unittest.TestCase):
    def test_full_orchestrator_writes_artifacts_stats_and_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / ".powerpacks"
            result = run_cli("run", "--output-dir", str(base / "search-index"), "--run-id", "test-run", "--input", str(FIXTURE_PEOPLE), "--force")
            run_dir = base / "search-index/test-run"
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["counts"]["flatten_people"]["people"], 4)
            self.assertEqual(result["counts"]["build_roles"]["roles"], 3)
            self.assertTrue((run_dir / "ledger.json").exists())
            self.assertTrue((run_dir / "stats/validate_contracts.json").exists())
            self.assertTrue((run_dir / "records/people.records.jsonl").exists())
            self.assertTrue((run_dir / "records/summaries.records.jsonl").exists())
            self.assertEqual([row["id"] for row in json.loads((run_dir / "ledger.json").read_text())["steps"]], STEPS)
            validation = json.loads((run_dir / "stats/validate_contracts.json").read_text())
            self.assertTrue(validation["people"]["ok"])
            self.assertTrue(validation["companies"]["ok"])
            self.assertTrue(validation["summaries"]["ok"])
            for record_file in ["records/people.records.jsonl", "records/companies.records.jsonl", "records/summaries.records.jsonl"]:
                for row in read_jsonl(run_dir / record_file):
                    uuid.UUID(row["id"])
                    self.assertEqual(len(row["vector"]), 1536)

    def test_limit_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / ".powerpacks"
            result = run_cli("run", "--output-dir", str(base / "search-index"), "--run-id", "limited", "--input", str(FIXTURE_PEOPLE), "--force", "--limit", "1")
            self.assertEqual(result["counts"]["flatten_people"]["people"], 1)
            self.assertEqual(result["counts"]["build_people_records"]["people_records"], 1)
            self.assertEqual(len(read_jsonl(base / "search-index/limited/unified/flattened_people.jsonl")), 1)

    def test_continue_resumes_partial_run(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / ".powerpacks"
            plan = run_cli("plan", "--output-dir", str(base / "search-index"), "--run-id", "resume", "--input", str(FIXTURE_PEOPLE))
            run_dir = Path(plan["run_dir"])
            run_dir.joinpath("unified").mkdir(parents=True)
            run_dir.joinpath("ledger.json").write_text(json.dumps({"primitive":"build_processing_pipeline","version":1,"status":"running","run_id":"resume","run_dir":str(run_dir),"input":str(FIXTURE_PEOPLE),"steps":[{"id":s,"status":"completed" if s=="flatten_people" else "pending"} for s in STEPS],"artifacts":{}}, sort_keys=True), encoding="utf-8")
            # Create the skipped step output to mimic an interrupted run after flattening.
            from packs.indexing.lib.people import flatten_people
            from packs.indexing.lib.artifacts import jsonl_dumps

            run_dir.joinpath("unified/flattened_people.jsonl").write_text(jsonl_dumps(flatten_people(FIXTURE_PEOPLE)), encoding="utf-8")
            result = run_cli("continue", "--ledger", str(run_dir / "ledger.json"))
            self.assertEqual(result["status"], "completed")
            ledger_steps = [row["id"] for row in json.loads((run_dir / "ledger.json").read_text())["steps"] if row.get("status")=="completed"]
            self.assertEqual(ledger_steps.count("flatten_people"), 1)
            self.assertEqual(ledger_steps[-1], "validate_contracts")

    def test_build_pipeline_rejects_paid_role_provider_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / ".powerpacks"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(PIPELINE),
                    "run",
                    "--output-dir",
                    str(base / "search-index"),
                    "--run-id",
                    "paid-provider",
                    "--input",
                    str(FIXTURE_PEOPLE),
                    "--role-provider",
                    "tlm",
                    "--force",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("requires --allow-paid-role-provider", proc.stderr + proc.stdout)
            self.assertFalse((base / "search-index/paid-provider/roles/chunks").exists())

    def test_integrated_checkpointed_roles_partial_then_continue(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / ".powerpacks"
            result = run_cli(
                "run",
                "--output-dir",
                str(base / "search-index"),
                "--run-id",
                "role-partial",
                "--input",
                str(FIXTURE_PEOPLE),
                "--checkpoint-every",
                "1",
                "--stop-after-role-chunks",
                "1",
                "--force",
            )
            run_dir = base / "search-index/role-partial"
            self.assertEqual(result["status"], "partial")
            ledger = json.loads((run_dir / "ledger.json").read_text())
            by_step = {step["id"]: step for step in ledger["steps"]}
            self.assertEqual(by_step["flatten_people"]["status"], "completed")
            self.assertEqual(by_step["build_roles"]["status"], "partial")
            self.assertFalse((run_dir / "records/people.records.jsonl").exists())
            self.assertTrue((run_dir / "roles/checkpoint.json").exists())
            self.assertTrue((run_dir / "roles/chunks/roles.000001.jsonl").exists())

            resumed = run_cli("continue", "--ledger", str(run_dir / "ledger.json"))
            self.assertEqual(resumed["status"], "completed")
            ledger = json.loads((run_dir / "ledger.json").read_text())
            by_step = {step["id"]: step for step in ledger["steps"]}
            self.assertEqual(by_step["build_roles"]["status"], "completed")
            self.assertTrue(by_step["build_roles"]["stats"]["checkpointed"])
            self.assertEqual(by_step["build_roles"]["stats"]["provider"], "local")
            self.assertTrue((run_dir / "roles/roles_with_dense_text.jsonl").exists())
            self.assertTrue((run_dir / "roles/roles_with_dense_text_remapped.jsonl").exists())
            self.assertEqual(read_jsonl(run_dir / "roles/roles_with_dense_text.jsonl"), read_jsonl(run_dir / "roles/roles_with_dense_text_remapped.jsonl"))
            self.assertTrue((run_dir / "records/people.records.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
