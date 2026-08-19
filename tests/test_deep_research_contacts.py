"""Offline contract for the thin Parallel research provider boundary."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from parallel.types import (
    TaskGroupStatus,
    TaskGroupStatusEvent,
    TaskRunEvent,
    TaskRunJsonOutput,
)

from packs.ingestion.primitives.deep_context.db.models import ArtifactRow, ParentRow, PersonRow
from packs.ingestion.primitives.common.legacy import (
    LEGACY_PARALLEL_HANDLE_RESULT,
    legacy_parallel_input_fingerprint,
)
from packs.ingestion.primitives.deep_context.db.queries import artifacts
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich.parallel_research import config, driver, parallel_client, queue
from packs.ingestion.primitives.deep_context.enrich.parallel_research.models import ResearchRunParams
from packs.ingestion.primitives.deep_context.enrich.parallel_research.queue import ResearchQueueRow
from packs.ingestion.primitives.deep_context.enrich.parallel_research.result import ResearchResult


def research_queue_row(handle: str = "jordan-bravo", *, guidance: str = "") -> ResearchQueueRow:
    return ResearchQueueRow(
        parent_id="parent-1",
        candidate_exists=False,
        row_key=f"candidate:{handle}",
        handle=handle,
        source_person_ids=("person-a",),
        display_name="Jordan Bravo",
        bio="Founder; we discuss testing",
        known_info="Owner context: robotics",
        primary_email="casey@example.com",
        phone_e164="+15550100",
        retarget_hint=guidance,
    )


def provider_output(*, linkedin: str | None = "https://www.linkedin.com/in/jordan-bravo-test") -> TaskRunJsonOutput:
    return TaskRunJsonOutput.model_validate({
        "type": "json",
        "content": {
            "real_name": "Jordan Bravo",
            "work_experience": [{
                "title": "Founder",
                "company_name": "Example",
                "is_current": True,
            }],
            "education": [],
            "location_city": "Oakland",
            "location_country": "US",
            "linkedin_url": linkedin,
            "github_url": None,
            "summary": "Founder",
        },
        "basis": [{
            "field": "linkedin_url",
            "reasoning": "Official company biography matches the dossier.",
            "confidence": "high",
            "citations": [{"url": "https://example.com/team", "excerpts": ["Jordan Bravo"]}],
        }],
    })


def status(**counts: int) -> TaskGroupStatus:
    return TaskGroupStatus(is_active=False, num_task_runs=sum(counts.values()), task_run_status_counts=counts)


def seed_db(root: Path) -> Db:
    db = Db(root / "deep-context.sqlite")
    db.project_rows((
        ParentRow("parent-1", "parent-worth:parent-1", "Jordan Bravo"),
        PersonRow("person-a", "parent-1", display_name="Jordan Bravo"),
    ))
    return db


class StubParallelClient:
    submissions: list[list[dict[str, object]]] = []

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def execute(self, inputs, _params, on_status, on_result):
        self.submissions.append(inputs)
        final = status(completed=len(inputs))
        on_status(final)
        for item in inputs:
            on_result(str(item["metadata"]["handle"]), provider_output())
        return ()


class ProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        StubParallelClient.submissions = []

    def test_schema_uses_native_arrays_and_provider_basis_for_confidence(self) -> None:
        properties = config.PERSON_RESEARCH_OUTPUT_SCHEMA["properties"]
        self.assertEqual(properties["work_experience"]["type"], "array")
        self.assertEqual(properties["education"]["type"], "array")
        self.assertNotIn("name_confidence", properties)
        self.assertIn("field-basis-2025-11-25", config.DEFAULT_BETA_HEADER)
        self.assertEqual(config.DEFAULT_STREAM_TIMEOUT, 3600)

    def test_stringified_nested_arrays_fail_at_the_provider_boundary(self) -> None:
        output = provider_output()
        output.content["work_experience"] = "[]"
        with self.assertRaisesRegex(ValueError, "must be arrays"):
            ResearchResult.from_output(output)

    def test_paid_fingerprint_covers_guidance_processor_and_contract(self) -> None:
        row = research_queue_row(guidance="Find the right LinkedIn")
        original = queue.input_fingerprint(row, row.handle)
        self.assertNotEqual(original, queue.input_fingerprint(replace(row, retarget_hint="Better clue"), row.handle))
        self.assertNotEqual(original, queue.input_fingerprint(row, row.handle, processor="pro"))
        self.assertEqual(original, queue.input_fingerprint(row, row.handle))

    def test_request_plan_fingerprint_covers_dossier_contract_and_dedupes(self) -> None:
        row = research_queue_row(guidance="Find the right LinkedIn")
        original = queue.request_plan_fingerprint((row,))
        duplicate = replace(
            row,
            row_key="candidate:email:duplicate@example.com",
            bio="Ignored duplicate dossier",
        )

        self.assertEqual(original, queue.request_plan_fingerprint((row, duplicate)))
        self.assertNotEqual(
            original,
            queue.request_plan_fingerprint((replace(row, bio="Changed dossier"),)),
        )
        self.assertNotEqual(
            original,
            queue.request_plan_fingerprint((row,), processor="pro"),
        )
        self.assertNotEqual(
            original,
            queue.request_plan_fingerprint((row,), beta_header="new-contract"),
        )

    def test_pre_contract_paid_fingerprint_is_grandfathered_without_spend(self) -> None:
        row = research_queue_row()
        legacy = legacy_parallel_input_fingerprint(queue.build_input(row, row.handle))
        pending, reused = queue.filter_already_done((row,), (
            ArtifactRow("research:jordan-bravo", "research", "parent-1", "/paid.json", "content", "projected", input_fingerprint=legacy),
        ))
        self.assertEqual((pending, reused), ([], 1))

    def test_missing_paid_fingerprint_is_not_an_exact_cache_hit(self) -> None:
        row = research_queue_row()
        pending, reused = queue.filter_already_done((row,), (
            ArtifactRow(
                "research:jordan-bravo",
                "research",
                "parent-1",
                "/unverifiable.json",
                "content",
                "projected",
                input_fingerprint=None,
            ),
        ))
        self.assertEqual((pending, reused), ([row], 0))

    def test_migrated_paid_result_is_grandfathered_by_stable_handle(self) -> None:
        row = research_queue_row()
        pending, reused = queue.filter_already_done(
            (row,),
            (
                ArtifactRow(
                    "research:jordan-bravo",
                    "research",
                    "parent-1",
                    "/paid-before-fingerprints.json",
                    "content",
                    "projected",
                    input_fingerprint=LEGACY_PARALLEL_HANDLE_RESULT,
                ),
            ),
        )
        self.assertEqual((pending, reused), ([], 1))

    def test_parallel_client_uses_sdk_models_and_streams_json_outputs(self) -> None:
        class Events(list):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        completed = TaskRunEvent.model_validate({
            "type": "task_run.state",
            "run": {
                "interaction_id": "interaction-1",
                "is_active": False,
                "processor": "core2x",
                "run_id": "run-1",
                "status": "completed",
                "metadata": {"handle": "jordan-bravo"},
            },
        })
        failed = TaskRunEvent.model_validate({
            "type": "task_run.state",
            "run": {
                "interaction_id": "interaction-2",
                "is_active": False,
                "processor": "core2x",
                "run_id": "run-2",
                "status": "failed",
                "metadata": {"handle": "casey-delta"},
            },
        })
        final = status(completed=1, failed=1)
        task_run = SimpleNamespace(
            result=mock.Mock(return_value=SimpleNamespace(output=provider_output())),
        )
        task_group = SimpleNamespace(
            create=mock.Mock(return_value=SimpleNamespace(task_group_id="group-1")),
            add_runs=mock.Mock(return_value=SimpleNamespace(run_ids=["run-1", "run-2"])),
            events=mock.Mock(return_value=Events([
                TaskGroupStatusEvent(
                    event_id="event-1",
                    type="task_group_status",
                    status=final,
                ),
            ])),
            get_runs=mock.Mock(return_value=Events([
                completed,
                failed,
            ])),
        )
        received: list[tuple[str, TaskRunJsonOutput]] = []
        with mock.patch.object(
            parallel_client,
            "Parallel",
            return_value=SimpleNamespace(task_group=task_group, task_run=task_run),
        ) as sdk:
            execution = parallel_client.ParallelClient("test-key", "https://parallel.test", "beta").execute(
                [
                    {"input": {}, "metadata": {"handle": "jordan-bravo"}, "processor": "core2x"},
                    {"input": {}, "metadata": {"handle": "casey-delta"}, "processor": "core2x"},
                ],
                SimpleNamespace(batch_size=500, stream_timeout=60),
                lambda _: None,
                lambda handle, output: received.append((handle, output)),
            )
        self.assertEqual(received[0][0], "jordan-bravo")
        self.assertEqual(received[0][1].basis[0].confidence, "high")
        self.assertEqual(execution, ("run-2: failed: no result",))
        task_group.get_runs.assert_called_once_with(
            "group-1",
            include_output=True,
            timeout=90,
        )
        task_run.result.assert_called_once_with("run-1", timeout=90)
        sdk.assert_called_once_with(
            api_key="test-key",
            base_url="https://parallel.test",
            default_headers={"parallel-beta": "beta"},
            max_retries=0,
        )

    def test_success_writes_one_provider_artifact_and_projects_basis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "research"
            db = seed_db(root)
            rows = (research_queue_row("jordan-a"), research_queue_row("jordan-b"))
            with (
                mock.patch.object(parallel_client, "ParallelClient", StubParallelClient),
                mock.patch.object(driver, "_api_key", return_value="test-key"),
            ):
                result = driver.run_research(ResearchRunParams(output_dir=output, rows=rows, db=db))

            self.assertTrue(result.complete)
            self.assertEqual(result.completed, 2)
            for handle in ("jordan-a", "jordan-b"):
                path = output / handle / "00_parallel_result.json"
                payload = json.loads(path.read_text())
                self.assertEqual(payload["basis"][0]["confidence"], "high")
                self.assertFalse((path.parent / "00_parallel_raw.json").exists())
                self.assertFalse((path.parent / "01_research_parallel.json").exists())
            projected = artifacts(db, kind="research")
            self.assertEqual(len(projected), 2)
            self.assertEqual(json.loads(projected[0].payload_json)["type"], "json")
            submitted = StubParallelClient.submissions[0][0]
            self.assertNotIn("task_spec", submitted)
            self.assertEqual(set(submitted["input"]), {"handle", "dossier"})

    def test_each_streamed_success_is_durable_before_later_error(self) -> None:
        class PartialClient(StubParallelClient):
            def execute(self, inputs, _params, on_status, on_result):
                on_result(str(inputs[0]["metadata"]["handle"]), provider_output())
                return ("result_stream: disconnected",)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = seed_db(root)
            rows = (research_queue_row("jordan-a"), research_queue_row("jordan-b"))
            with (
                mock.patch.object(parallel_client, "ParallelClient", PartialClient),
                mock.patch.object(driver, "_api_key", return_value="test-key"),
            ):
                result = driver.run_research(ResearchRunParams(output_dir=root / "research", rows=rows, db=db))
            self.assertFalse(result.complete)
            self.assertTrue(result.usable)
            self.assertEqual([row.artifact_key for row in artifacts(db, kind="research")], ["research:jordan-a"])
            pending, reused = queue.filter_already_done(rows, artifacts(db, kind="research"))
            self.assertEqual(reused, 1)
            self.assertEqual([row.handle for row in pending], ["jordan-b"])

    def test_sqlite_projection_precedes_the_derived_provider_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = seed_db(root)
            events: list[str] = []
            project_rows = db.project_rows
            write_json = driver.write_json

            def project(rows):
                events.append("project")
                return project_rows(rows)

            def write(path, payload):
                events.append("write")
                return write_json(path, payload)

            with (
                mock.patch.object(db, "project_rows", side_effect=project),
                mock.patch.object(driver, "write_json", side_effect=write),
                mock.patch.object(parallel_client, "ParallelClient", StubParallelClient),
                mock.patch.object(driver, "_api_key", return_value="test-key"),
            ):
                result = driver.run_research(
                    ResearchRunParams(
                        output_dir=root / "research",
                        rows=(research_queue_row(),),
                        db=db,
                    )
                )

            self.assertTrue(result.complete)
            self.assertEqual(events[:2], ["project", "write"])

    def test_provider_failure_is_failed_and_can_be_rerun(self) -> None:
        class FailedClient(StubParallelClient):
            def execute(self, _inputs, _params, _on_status, _on_result):
                raise TimeoutError("provider unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = seed_db(root)
            with (
                mock.patch.object(parallel_client, "ParallelClient", FailedClient),
                mock.patch.object(driver, "_api_key", return_value="test-key"),
            ):
                result = driver.run_research(
                    ResearchRunParams(output_dir=root / "research", rows=(research_queue_row(),), db=db)
                )
            self.assertFalse(result.complete)
            self.assertFalse(result.usable)
            self.assertEqual(result.errors, ("TimeoutError: provider unavailable",))


if __name__ == "__main__":
    unittest.main()
