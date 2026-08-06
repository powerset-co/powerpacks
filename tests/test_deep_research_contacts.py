"""Offline contract for the slim in-process Parallel research provider."""

import csv
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.ingestion.primitives.deep_context import deep_research_contacts as research
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    ParentRow,
    PersonRow,
    ProjectionStatus,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.parallel_research import (
    config,
    driver,
    queue,
    sdk_client,
)


FIELDS = [
    "handle",
    "source_parent_slug",
    "source_person_ids",
    "source_candidate_public_identifier",
    "display_name",
    "bio",
    "known_info",
    "primary_email",
    "phone_e164",
    "area_code",
    "source_channel",
    "retarget_hint",
]


def write_queue(
    path: Path, handles: list[str], *, guidance: str = ""
) -> list[dict[str, str]]:
    rows = []
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        for handle in handles:
            row = {
                "parent_id": "parent-1",
                "candidate_exists": "0",
                "handle": handle,
                "source_parent_slug": "jordan-bravo",
                "source_person_ids": json.dumps(["person-a"]),
                "source_candidate_public_identifier": f"candidate:{handle}",
                "display_name": "Jordan Bravo",
                "bio": "Founder; we discuss testing",
                "known_info": f"{guidance}\nOwner context: robotics".strip(),
                "primary_email": "casey@example.com",
                "phone_e164": "+15550100",
                "area_code": "555",
                "source_channel": "email",
                "retarget_hint": guidance,
            }
            rows.append(row)
            writer.writerow({field: row.get(field, "") for field in FIELDS})
    return rows


def seed_db(root: Path) -> Db:
    db = Db(root / "deep-context.sqlite")
    db.project_rows((
        ParentRow("parent-1", "parent-worth:parent-1", "Jordan Bravo"),
        PersonRow("person-a", "parent-1", display_name="Jordan Bravo"),
    ))
    return db


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

    def test_sdk_client_streams_group_results_without_per_run_fetches(self) -> None:
        class Events(list):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        completed = SimpleNamespace(
            run=SimpleNamespace(
                run_id="run-1", status="completed", error=None,
                metadata={"handle": "jordan-bravo"},
            ),
            output=SimpleNamespace(content={"real_name": "Jordan Bravo"}),
        )
        failed = SimpleNamespace(
            run=SimpleNamespace(
                run_id="run-2", status="failed", error="provider failed",
                metadata={"handle": "casey-delta"},
            ),
            output=None,
        )
        task_group = SimpleNamespace(
            create=mock.Mock(return_value=SimpleNamespace(task_group_id="group-1")),
            add_runs=mock.Mock(return_value=SimpleNamespace(run_ids=["run-1", "run-2"])),
            retrieve=mock.Mock(return_value=SimpleNamespace(
                status=SimpleNamespace(model_dump=lambda: {
                    "is_active": False,
                    "task_run_status_counts": {"completed": 1, "failed": 1},
                })
            )),
            get_runs=mock.Mock(return_value=Events([completed, failed])),
        )
        fake_sdk = SimpleNamespace(task_group=task_group)
        params = SimpleNamespace(
            batch_size=500, max_wait=60, poll_interval=0, api_timeout=30,
        )
        with mock.patch.object(sdk_client, "Parallel", return_value=fake_sdk):
            count, results, errors, final = sdk_client.ParallelClient(
                "test-key", "https://parallel.test", "beta"
            ).execute([{"metadata": {"handle": "jordan-bravo"}}] * 2, params, lambda _: None)

        self.assertEqual(count, 2)
        self.assertEqual(results, {"jordan-bravo": {"real_name": "Jordan Bravo"}})
        self.assertEqual(errors, ["run-2: failed: provider failed"])
        self.assertFalse(final["is_active"])
        task_group.get_runs.assert_called_once_with(
            "group-1", include_input=True, include_output=True, timeout=40,
        )
        self.assertFalse(hasattr(fake_sdk, "task_run"))

    def test_success_writes_only_raw_and_normalized_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue_csv = root / "research_queue.csv"
            output = root / "research"
            rows = write_queue(
                queue_csv,
                ["jordan-a", "jordan-b"],
                guidance="Find the right LinkedIn",
            )
            db = seed_db(root)
            queue_csv.unlink()
            with (
                mock.patch.object(sdk_client, "ParallelClient", StubParallelClient),
                mock.patch.object(driver, "_api_key", return_value="test-key"),
            ):
                payload = research.run_research(
                    research.ResearchRunParams(
                        output_dir=output,
                        rows=tuple(rows),
                        poll_interval=0,
                        db=db,
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
            queue_csv = root / "research_queue.csv"
            output = root / "research"
            rows = write_queue(queue_csv, ["jordan-bravo"], guidance="First clue")
            db = seed_db(root)
            params = research.ResearchRunParams(
                output_dir=output,
                rows=tuple(rows),
                poll_interval=0,
                db=db,
            )
            with (
                mock.patch.object(sdk_client, "ParallelClient", StubParallelClient),
                mock.patch.object(driver, "_api_key", return_value="test-key"),
            ):
                self.assertEqual(research.run_research(params)["status"], "completed")
                first = output / "jordan-bravo" / "01_research_parallel.json"
                first_fingerprint = json.loads(first.read_text())["metadata"]["input_fingerprint"]
                self.assertEqual(research.run_research(params)["status"], "no_work")
                changed = write_queue(
                    queue_csv, ["jordan-bravo"], guidance="Better clue"
                )
                self.assertEqual(
                    research.run_research(replace(params, rows=tuple(changed)))["status"],
                    "completed",
                )
                second_fingerprint = json.loads(first.read_text())["metadata"]["input_fingerprint"]

            self.assertNotEqual(first_fingerprint, second_fingerprint)
            self.assertEqual(len(StubParallelClient.submissions), 2)
            self.assertEqual(first.parent, output / "jordan-bravo")

    def test_pre_rewrite_paid_output_is_reused_without_rebilling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue_csv = root / "research_queue.csv"
            output = root / "research"
            rows = write_queue(queue_csv, ["jordan-bravo"])
            db = seed_db(root)
            person = output / "jordan-bravo"
            person.mkdir(parents=True)
            (person / "01_research_parallel.json").write_text(
                json.dumps({"metadata": {"research_notes": "legacy"}}), encoding="utf-8"
            )
            db.project_rows((ArtifactRow(
                "research:jordan-bravo",
                "research",
                "parent-1",
                str(person / "01_research_parallel.json"),
                "legacy-fingerprint",
                ProjectionStatus.PROJECTED.value,
            ),))
            payload = research.run_research(
                research.ResearchRunParams(
                    output_dir=output,
                    rows=tuple(rows),
                    db=db,
                )
            )
            self.assertEqual(payload["status"], "no_work")

    def test_coordinated_provider_reports_callback_without_writing_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue_csv = root / "research_queue.csv"
            output = root / "research"
            rows = write_queue(queue_csv, ["jordan-bravo"])
            db = seed_db(root)
            person = output / "jordan-bravo"
            person.mkdir(parents=True)
            (person / "01_research_parallel.json").write_text(
                json.dumps({"metadata": {"research_notes": "paid result"}}),
                encoding="utf-8",
            )
            db.project_rows((ArtifactRow(
                "research:jordan-bravo",
                "research",
                "parent-1",
                str(person / "01_research_parallel.json"),
                "paid-fingerprint",
                ProjectionStatus.PROJECTED.value,
            ),))
            events = []
            payload = research.run_research(research.ResearchRunParams(
                output_dir=output,
                rows=tuple(rows),
                on_progress=events.append,
                db=db,
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
            queue_csv = root / "research_queue.csv"
            rows = write_queue(queue_csv, ["jordan-a", "jordan-b", "jordan-c"])
            db = seed_db(root)
            with (
                mock.patch.object(sdk_client, "ParallelClient", StubParallelClient),
                mock.patch.object(driver, "_api_key", return_value="test-key"),
            ):
                payload = research.run_research(
                    research.ResearchRunParams(
                        output_dir=root / "research",
                        rows=tuple(rows),
                        limit=1,
                        poll_interval=0,
                        db=db,
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
            queue_csv = root / "research_queue.csv"
            manifest = root / "enrich" / "manifest.json"
            rows = write_queue(queue_csv, ["jordan-bravo"])
            db = seed_db(root)
            with (
                mock.patch.object(sdk_client, "ParallelClient", NoRunsClient),
                mock.patch.object(driver, "_api_key", return_value="test-key"),
            ):
                payload = research.run_research(
                    research.ResearchRunParams(
                        output_dir=root / "research",
                        rows=tuple(rows),
                        manifest=str(manifest),
                        db=db,
                    )
                )
            self.assertEqual(payload["status"], "failed")
            receipt = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["counts"]["failed"], 1)


if __name__ == "__main__":
    unittest.main()
