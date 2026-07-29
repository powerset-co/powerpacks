"""In-process contract of the Parallel research client.

Locks the payload-returning `run_research`/`submit_research`/`poll_research`
shapes that reconcile_deep_research branches on (the subprocess boundary is
gone), with a stubbed ParallelClient — no network, no spend.
"""

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context import deep_research_contacts as drc


def write_queue(path: Path, handles: list[str]) -> None:
    fields = ["handle", "display_name", "bio", "known_info", "primary_email",
              "phone_e164", "area_code", "source_channel", "retarget_hint"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for handle in handles:
            writer.writerow({
                "handle": handle,
                "display_name": "Jordan Bravo",
                "bio": "We discuss: testing",
                "known_info": "synthetic",
                "primary_email": "casey@example.com",
                "phone_e164": "+15550100",
                "area_code": "555",
                "source_channel": "email",
                "retarget_hint": "",
            })


class StubParallelClient:
    """A ParallelClient that answers like a completed task group."""

    def __init__(self, *_args, **_kwargs):
        pass

    def create_group(self, metadata=None):
        return {"taskgroup_id": "tg-test-1"}

    def add_runs(self, group_id, batch):
        return {"run_ids": [f"run-{item['metadata']['handle']}" for item in batch]}

    def get_group(self, group_id):
        return {"status": {"is_active": False,
                           "task_run_status_counts": {"completed": 2}}}

    def get_run_result(self, run_id, api_timeout=60):
        handle = run_id.removeprefix("run-")
        return {
            "output": {"content": {"real_name": "Jordan Bravo",
                                   "linkedin_url": "https://www.linkedin.com/in/jordan-bravo-test"}},
            "run": {"metadata": {"handle": handle}},
        }


class RunResearchTests(unittest.TestCase):
    def test_no_work_when_every_row_already_done(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            queue = base / "research_queue.csv"
            out = base / "deep-research"
            write_queue(queue, ["jordan-a", "jordan-b"])
            for handle in ("jordan-a", "jordan-b"):
                person = out / handle
                person.mkdir(parents=True)
                (person / "01_research_parallel.json").write_text("{}", encoding="utf-8")
            manifest = base / "enrich" / "manifest.json"
            manifest.parent.mkdir(parents=True)

            payload = drc.run_research(drc.ResearchRunParams(
                input_csv=queue, output_dir=out, manifest=str(manifest)))

            self.assertEqual(payload["status"], "no_work")
            self.assertEqual(payload["skipped_already_done"], 2)
            written = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(written["status"], "research_complete")
            self.assertEqual(written["counts"]["completed"], 2)

    def test_failed_submit_returns_without_polling(self) -> None:
        class NoGroupClient(StubParallelClient):
            def create_group(self, metadata=None):
                return {}

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            queue = base / "research_queue.csv"
            out = base / "deep-research"
            write_queue(queue, ["jordan-a"])
            with mock.patch.object(drc, "ParallelClient", NoGroupClient), \
                 mock.patch.object(drc, "_resolve_api_key", return_value="test-key"):
                payload = drc.run_research(drc.ResearchRunParams(
                    input_csv=queue, output_dir=out))

            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["command"], "submit")
            self.assertFalse((out / "_taskgroup.json").exists())

    def test_run_success_writes_artifacts_and_returns_summary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            queue = base / "research_queue.csv"
            out = base / "deep-research"
            write_queue(queue, ["jordan-a", "jordan-b"])
            with mock.patch.object(drc, "ParallelClient", StubParallelClient), \
                 mock.patch.object(drc, "_resolve_api_key", return_value="test-key"):
                payload = drc.run_research(drc.ResearchRunParams(
                    input_csv=queue, output_dir=out))

            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["counts"]["results_fetched"], 2)
            self.assertEqual(payload["counts"]["errors"], 0)
            self.assertEqual(payload["counts"]["linkedin_found"], 2)
            for handle in ("jordan-a", "jordan-b"):
                self.assertTrue((out / handle / "00_parallel_raw.json").is_file())
                research = json.loads(
                    (out / handle / "01_research_parallel.json").read_text(encoding="utf-8"))
                self.assertEqual(research["metadata"]["source_identifier"],
                                 "casey@example.com")
            state = json.loads((out / "_taskgroup.json").read_text(encoding="utf-8"))
            self.assertEqual(state["taskgroup_id"], "tg-test-1")
            summary = json.loads((out / "_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["status"], "completed")

    def test_limit_caps_submission(self) -> None:
        submitted: list[list] = []

        class CountingClient(StubParallelClient):
            def add_runs(self, group_id, batch):
                submitted.append(batch)
                return super().add_runs(group_id, batch)

            def get_group(self, group_id):
                return {"status": {"is_active": False,
                                   "task_run_status_counts": {"completed": 1}}}

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            queue = base / "research_queue.csv"
            out = base / "deep-research"
            write_queue(queue, ["jordan-a", "jordan-b", "jordan-c"])
            with mock.patch.object(drc, "ParallelClient", CountingClient), \
                 mock.patch.object(drc, "_resolve_api_key", return_value="test-key"):
                payload = drc.run_research(drc.ResearchRunParams(
                    input_csv=queue, output_dir=out, limit=1))

            self.assertEqual(sum(len(batch) for batch in submitted), 1)
            self.assertEqual(payload["counts"]["results_fetched"], 1)

    def test_cli_exit_codes_map_from_status(self) -> None:
        cases = {"no_work": 0, "completed": 0, "completed_with_errors": 2, "failed": 1}
        for status, expected in cases.items():
            with mock.patch.object(drc, "run_research",
                                   return_value={"status": status}), \
                 mock.patch.object(drc, "emit"):
                args = mock.Mock(input="q.csv", output_dir="out", manifest="")
                self.assertEqual(drc.cmd_run(args), expected, status)


if __name__ == "__main__":
    unittest.main()
