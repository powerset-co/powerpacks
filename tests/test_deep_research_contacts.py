"""Offline contract for the slim in-process Parallel research provider."""

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context import deep_research_contacts as research
from packs.ingestion.primitives.deep_context.parallel_research import (
    config,
    driver,
    queue,
    sdk_client,
)


FIELDS = [
    "handle",
    "display_name",
    "bio",
    "known_info",
    "primary_email",
    "phone_e164",
    "area_code",
    "source_channel",
    "retarget_hint",
]


def write_queue(path: Path, handles: list[str], *, guidance: str = "") -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        for handle in handles:
            writer.writerow({
                "handle": handle,
                "display_name": "Jordan Bravo",
                "bio": "Founder; we discuss testing",
                "known_info": f"{guidance}\nOwner context: robotics".strip(),
                "primary_email": "casey@example.com",
                "phone_e164": "+15550100",
                "area_code": "555",
                "source_channel": "email",
                "retarget_hint": guidance,
            })


class StubParallelClient:
    submissions: list[list[dict]] = []

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def execute(self, inputs: list[dict], _params, on_status):
        self.submissions.append(inputs)
        on_status({"completed": len(inputs)})
        result = {
            item["metadata"]["handle"]: {
                "real_name": "Jordan Bravo",
                "name_confidence": 0.9,
                "name_evidence": "official profile",
                "work_experience": "[]",
                "education": "[]",
                "linkedin_url": "https://www.linkedin.com/in/jordan-bravo-test",
                "summary": "Founder",
                "research_notes": "matched",
            }
            for item in inputs
        }
        return len(inputs), result, [], {
            "is_active": False,
            "task_run_status_counts": {"completed": len(inputs)},
        }


class ProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        StubParallelClient.submissions = []

    def test_build_input_is_one_dossier_plus_optional_guidance(self) -> None:
        row = {
            "display_name": "Jordan Bravo",
            "bio": "Known collaborator",
            "known_info": "Find the corrected profile.\nOwner context: robotics",
            "retarget_hint": "Find the corrected profile.",
            "primary_email": "casey@example.com",
        }
        payload = queue.build_input(row, "jordan-bravo")
        self.assertEqual(set(payload), {"handle", "dossier", "guidance"})
        self.assertEqual(payload["guidance"], "Find the corrected profile.")
        self.assertIn("Owner context: robotics", payload["dossier"])
        self.assertNotIn("Find the corrected profile.", payload["dossier"])
        self.assertEqual(
            set(config.PERSON_RESEARCH_INPUT_SCHEMA["properties"]),
            {"handle", "dossier", "guidance"},
        )

    def test_success_writes_only_raw_and_normalized_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "research_queue.csv"
            output = root / "research"
            write_queue(queue, ["jordan-a", "jordan-b"], guidance="Find the right LinkedIn")
            with (
                mock.patch.object(sdk_client, "ParallelClient", StubParallelClient),
                mock.patch.object(driver, "_api_key", return_value="test-key"),
            ):
                payload = research.run_research(
                    research.ResearchRunParams(
                        input_csv=queue,
                        output_dir=output,
                        poll_interval=0,
                    )
                )

            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["counts"]["results_fetched"], 2)
            self.assertTrue((output / "manifest.json").is_file())
            self.assertFalse((output / "_taskgroup.json").exists())
            self.assertFalse((output / "_manifest.json").exists())
            for handle in ("jordan-a", "jordan-b"):
                self.assertTrue((output / handle / "00_parallel_raw.json").is_file())
                normalized = json.loads(
                    (output / handle / "01_research_parallel.json").read_text(encoding="utf-8")
                )
                self.assertEqual(normalized["metadata"]["source_identifier"], "casey@example.com")
                self.assertEqual(len(normalized["metadata"]["input_fingerprint"]), 64)
            submitted_input = StubParallelClient.submissions[0][0]["input"]
            self.assertEqual(set(submitted_input), {"handle", "dossier", "guidance"})

    def test_same_input_reuses_but_changed_guidance_overwrites_fixed_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "research_queue.csv"
            output = root / "research"
            write_queue(queue, ["jordan-bravo"], guidance="First clue")
            params = research.ResearchRunParams(
                input_csv=queue,
                output_dir=output,
                poll_interval=0,
            )
            with (
                mock.patch.object(sdk_client, "ParallelClient", StubParallelClient),
                mock.patch.object(driver, "_api_key", return_value="test-key"),
            ):
                self.assertEqual(research.run_research(params)["status"], "completed")
                first = output / "jordan-bravo" / "01_research_parallel.json"
                first_fingerprint = json.loads(first.read_text())["metadata"]["input_fingerprint"]
                self.assertEqual(research.run_research(params)["status"], "no_work")
                write_queue(queue, ["jordan-bravo"], guidance="Better clue")
                self.assertEqual(research.run_research(params)["status"], "completed")
                second_fingerprint = json.loads(first.read_text())["metadata"]["input_fingerprint"]

            self.assertNotEqual(first_fingerprint, second_fingerprint)
            self.assertEqual(len(StubParallelClient.submissions), 2)
            self.assertEqual(first.parent, output / "jordan-bravo")

    def test_pre_rewrite_paid_output_is_reused_without_rebilling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "research_queue.csv"
            output = root / "research"
            write_queue(queue, ["jordan-bravo"])
            person = output / "jordan-bravo"
            person.mkdir(parents=True)
            (person / "01_research_parallel.json").write_text(
                json.dumps({"metadata": {"research_notes": "legacy"}}), encoding="utf-8"
            )
            payload = research.run_research(
                research.ResearchRunParams(input_csv=queue, output_dir=output)
            )
            self.assertEqual(payload["status"], "no_work")

    def test_coordinated_provider_reports_callback_without_writing_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "research_queue.csv"
            output = root / "research"
            write_queue(queue, ["jordan-bravo"])
            person = output / "jordan-bravo"
            person.mkdir(parents=True)
            (person / "01_research_parallel.json").write_text(
                json.dumps({"metadata": {"research_notes": "paid result"}}),
                encoding="utf-8",
            )
            events = []
            payload = research.run_research(research.ResearchRunParams(
                input_csv=queue,
                output_dir=output,
                on_progress=events.append,
                owns_receipt=False,
            ))

            self.assertEqual(payload["status"], "no_work")
            self.assertEqual(events, [{
                "status": "research_complete",
                "counts": {"total": 1, "completed": 1, "pending": 0, "failed": 0},
            }])
            self.assertFalse((output / "manifest.json").exists())

    def test_limit_caps_submitted_dossiers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "research_queue.csv"
            write_queue(queue, ["jordan-a", "jordan-b", "jordan-c"])
            with (
                mock.patch.object(sdk_client, "ParallelClient", StubParallelClient),
                mock.patch.object(driver, "_api_key", return_value="test-key"),
            ):
                payload = research.run_research(
                    research.ResearchRunParams(
                        input_csv=queue,
                        output_dir=root / "research",
                        limit=1,
                        poll_interval=0,
                    )
                )
            self.assertEqual(payload["counts"]["run_ids"], 1)
            self.assertEqual(sum(map(len, StubParallelClient.submissions)), 1)

    def test_no_provider_run_ids_is_a_failed_canonical_manifest(self) -> None:
        class NoRunsClient(StubParallelClient):
            def execute(self, _inputs, _params, _on_status):
                return 0, {}, [], {}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "research_queue.csv"
            manifest = root / "enrich" / "manifest.json"
            write_queue(queue, ["jordan-bravo"])
            with (
                mock.patch.object(sdk_client, "ParallelClient", NoRunsClient),
                mock.patch.object(driver, "_api_key", return_value="test-key"),
            ):
                payload = research.run_research(
                    research.ResearchRunParams(
                        input_csv=queue,
                        output_dir=root / "research",
                        manifest=str(manifest),
                    )
                )
            self.assertEqual(payload["status"], "failed")
            receipt = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["counts"]["failed"], 1)


if __name__ == "__main__":
    unittest.main()
