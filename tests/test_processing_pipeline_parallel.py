from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from packs.indexing.lib.io import read_json, write_json
from packs.indexing.primitives.build_processing_pipeline import build_processing_pipeline as pipeline


class ProcessingPipelineParallelTests(unittest.TestCase):
    def _run_with_fakes(
        self,
        steps: list[str],
        functions: dict[str, object],
        *,
        initial_steps: list[dict] | None = None,
        workers: int = 4,
    ) -> tuple[dict, dict[str, dict[str, float]]]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "ledger.json"
            write_json(
                ledger_path,
                {
                    "run_dir": str(root),
                    "status": "pending",
                    "steps": initial_steps or [{"id": step, "status": "pending"} for step in steps],
                },
            )
            timings: dict[str, dict[str, float]] = {}
            lock = threading.Lock()

            def wrap(step: str, function):
                def run(ledger, paths):
                    with lock:
                        timings.setdefault(step, {})["start"] = time.perf_counter()
                    try:
                        return function(ledger, paths)
                    finally:
                        with lock:
                            timings.setdefault(step, {})["end"] = time.perf_counter()

                return run

            wrapped = {step: wrap(step, function) for step, function in functions.items()}
            with mock.patch.object(pipeline, "STEPS", steps), \
                    mock.patch.object(pipeline, "STEP_FUNCTIONS", wrapped), \
                    mock.patch.object(pipeline, "commit_processed_person_hashes", return_value={"hashes_written": False}):
                result = pipeline.execute(
                    ledger_path,
                    executor_factory=concurrent.futures.ThreadPoolExecutor,
                    max_workers=workers,
                )
            return result, timings

    def test_independent_branches_overlap_and_fan_in_waits(self) -> None:
        steps = [
            "flatten_people",
            "build_roles",
            "embed_role_positions",
            "build_company_corpus",
            "embed_companies",
            "detect_ceo_founders",
            "infer_ages",
            "build_people_records",
        ]

        def complete(_ledger, _paths):
            time.sleep(0.04)
            return {}, {"rows": 1}

        result, timings = self._run_with_fakes(
            steps,
            {step: complete for step in steps},
            workers=4,
        )

        self.assertEqual(result["status"], "completed")
        self.assertLess(timings["build_roles"]["start"], timings["build_company_corpus"]["end"])
        self.assertLess(timings["build_company_corpus"]["start"], timings["build_roles"]["end"])
        self.assertGreaterEqual(timings["embed_role_positions"]["start"], timings["build_roles"]["end"])
        self.assertGreaterEqual(timings["embed_companies"]["start"], timings["build_company_corpus"]["end"])
        people_start = timings["build_people_records"]["start"]
        for prerequisite in ("embed_role_positions", "embed_companies", "detect_ceo_founders", "infer_ages"):
            self.assertGreaterEqual(people_start, timings[prerequisite]["end"])

    def test_completed_steps_are_not_resubmitted(self) -> None:
        steps = ["flatten_people", "build_roles", "embed_role_positions"]
        calls: list[str] = []

        def record(step: str):
            def run(_ledger, _paths):
                calls.append(step)
                return {}, {}

            return run

        initial = [
            {"id": "flatten_people", "status": "completed"},
            {"id": "build_roles", "status": "completed"},
            {"id": "embed_role_positions", "status": "pending"},
        ]
        result, _ = self._run_with_fakes(
            steps,
            {step: record(step) for step in steps},
            initial_steps=initial,
            workers=2,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(calls, ["embed_role_positions"])

    def test_partial_branch_prevents_dependent_work(self) -> None:
        steps = [
            "flatten_people",
            "build_roles",
            "embed_role_positions",
            "build_company_corpus",
            "embed_companies",
            "build_people_records",
        ]
        calls: list[str] = []

        def complete(step: str):
            def run(_ledger, _paths):
                calls.append(step)
                return {}, {}

            return run

        def partial_company(_ledger, _paths):
            calls.append("build_company_corpus")
            raise pipeline.PipelinePartial("build_company_corpus", {"checkpoint": "company"}, {"rows": 1})

        functions = {step: complete(step) for step in steps}
        functions["build_company_corpus"] = partial_company
        result, _ = self._run_with_fakes(steps, functions, workers=3)

        self.assertEqual(result["status"], "partial")
        self.assertNotIn("embed_companies", calls)
        self.assertNotIn("build_people_records", calls)
        company = next(step for step in result["steps"] if step["id"] == "build_company_corpus")
        self.assertEqual(company["status"], "partial")

    def test_provider_concurrency_is_shared_across_workers(self) -> None:
        ledger = {"_parallel_provider_divisor": 4}
        with mock.patch.dict(
            os.environ,
            {
                "POWERPACKS_OPENAI_CONCURRENCY": "240",
                "POWERPACKS_OPENAI_EMBEDDING_CONCURRENCY": "8",
            },
        ):
            self.assertEqual(pipeline.openai_concurrency(ledger), 60)
            self.assertEqual(pipeline.embedding_concurrency(ledger), 2)

    def test_worker_failure_does_not_schedule_fan_in(self) -> None:
        steps = [
            "flatten_people",
            "build_roles",
            "embed_role_positions",
            "build_company_corpus",
            "embed_companies",
            "build_people_records",
        ]
        calls: list[str] = []

        def complete(step: str):
            def run(_ledger, _paths):
                calls.append(step)
                time.sleep(0.01)
                return {}, {}

            return run

        def fail_company(_ledger, _paths):
            calls.append("build_company_corpus")
            raise RuntimeError("company failed")

        functions = {step: complete(step) for step in steps}
        functions["build_company_corpus"] = fail_company
        with self.assertRaisesRegex(RuntimeError, "company failed"):
            self._run_with_fakes(steps, functions, workers=3)
        self.assertNotIn("embed_companies", calls)
        self.assertNotIn("build_people_records", calls)

    def test_cli_runs_full_precomputed_pipeline_in_process_pool(self) -> None:
        from packs.indexing.lib.people import flatten_people
        import tests.test_indexing_pipeline as indexing_test
        from tests.test_real_openai_processing_pipeline import write_fixture_with_title_hashes

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_csv = write_fixture_with_title_hashes(indexing_test.FIXTURE_PEOPLE, root / "people.csv")
            original_fixture = indexing_test.FIXTURE_PEOPLE
            try:
                indexing_test.FIXTURE_PEOPLE = input_csv
                inputs = indexing_test.IndexingPipelineTests()._precomputed_inputs(root / "inputs")
            finally:
                indexing_test.FIXTURE_PEOPLE = original_fixture

            output = root / "output"
            founder_path = output / "unified/roles/founder_enrichment.jsonl"
            ages_path = output / "unified/inferred_ages.jsonl"
            founder_path.parent.mkdir(parents=True, exist_ok=True)
            founder_rows = []
            age_rows = []
            for person in flatten_people(input_csv):
                person_id = str(person["id"])
                age_rows.append({"person_id": person_id, "birth_year": 1990})
                for index, experience in enumerate(person.get("work_experiences") or []):
                    position_id = str(
                        experience.get("id")
                        or experience.get("position_id")
                        or f"{person_id}-{index}"
                    ).strip()
                    founder_rows.append(
                        {
                            "position_id": position_id,
                            "person_id": person_id,
                            "is_founder": False,
                            "confidence": 1.0,
                        }
                    )
            founder_path.write_text("".join(json.dumps(row) + "\n" for row in founder_rows))
            ages_path.write_text("".join(json.dumps(row) + "\n" for row in age_rows))

            command = [
                sys.executable,
                str(Path(__file__).resolve().parents[1] / "packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py"),
                "run",
                "--input",
                str(input_csv),
                "--output-dir",
                str(output),
                "--default-operator-id",
                "operator:test",
                "--role-input-classifications",
                str(inputs["role_classes"]),
                "--role-input-embeddings",
                str(inputs["role_embeddings"]),
                "--company-input-classifications",
                str(inputs["company_classes"]),
                "--company-input-embeddings",
                str(inputs["company_embeddings"]),
                "--summary-input-embeddings",
                str(inputs["summary_embeddings"]),
            ]
            result = subprocess.run(
                command,
                cwd=Path(__file__).resolve().parents[1],
                env=os.environ,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("parallel workers=4", result.stderr)
            ledger = read_json(output / "ledger.json")
            self.assertEqual(ledger["status"], "completed")
            self.assertTrue(all(step["status"] == "completed" for step in ledger["steps"]))


if __name__ == "__main__":
    unittest.main()
