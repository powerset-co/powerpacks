"""Typed Parallel outputs projected explicitly before display-only receipts."""

from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.ingestion.primitives.deep_context.enrich import reconcile_deep_research as reconcile
from packs.ingestion.primitives.deep_context.enrich.parallel_research import driver, projection
from packs.ingestion.primitives.deep_context.enrich.parallel_research import models as research_models
from packs.ingestion.primitives.deep_context.enrich.parallel_research.queue import (
    ContactChannel,
    ResearchQueueRow,
)
from packs.ingestion.primitives.deep_context.enrich.research_reconcile import coordinator, selection
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.judging import (
    RetargetRunResult,
)
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.selection import QUEUE_FIELDS
from packs.ingestion.primitives.deep_context.db.models import (
    LinkRow,
    ParentRow,
    PersonRow,
    RowKind,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.view_models import EnrichmentQueueRow
from packs.ingestion.primitives.deep_context.db.workflow_views import ReviewSelection
from deep_context_sqlite_test_helpers import query


class EnrichmentProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.out = self.root / "deep-research"
        self.out.mkdir()
        self.manifest = self.out / "manifest.json"
        self.queue = self.out / "research_queue.csv"
        self.db = Db(self.root / "deep-context.sqlite")
        self.db.project_rows(
            (
                ParentRow("parent-1", "parent-worth:parent-1", "Jordan Bravo"),
                PersonRow("person-a", "parent-1", display_name="Jordan Bravo"),
                LinkRow(
                    "candidate:email:jordan@example.com",
                    "parent-1",
                    "candidate:email:jordan@example.com",
                    RowKind.CANDIDATE_EMAIL.value,
                    source=WriterSource.RECONCILE.value,
                ),
            )
        )
        self.queue_row = ResearchQueueRow(
            parent_id="parent-1",
            candidate_exists=True,
            row_key="candidate:email:jordan@example.com",
            handle="jordan-bravo",
            source_parent_slug="jordan-bravo",
            source_person_ids=("person-a",),
            source_candidate_public_identifier="candidate:email:jordan@example.com",
            display_name="Jordan Bravo",
            bio="Known collaborator",
            known_info="Synthetic fixture",
            primary_email="jordan@example.com",
            source_channel=ContactChannel.EMAIL,
            retarget_hint="Find the correct profile",
        )
        self.selection = ReviewSelection("selection-1", 1, 1, 0, 0, "")
        self._write_queue([self.queue_row])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_queue(self, rows: list[ResearchQueueRow]) -> None:
        with self.queue.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=QUEUE_FIELDS)
            writer.writeheader()
            writer.writerows(row.csv_dict(QUEUE_FIELDS) for row in rows)

    def _write_result(self, suffix: str = "one") -> tuple[Path, Path]:
        person = self.out / "jordan-bravo"
        person.mkdir(exist_ok=True)
        raw = person / "00_parallel_raw.json"
        result = person / "01_research_parallel.json"
        raw.write_text(json.dumps({"provider_result": suffix}), encoding="utf-8")
        result.write_text(
            json.dumps(
                {
                    "person": {"full_name": "Jordan Bravo", "confidence": 0.91},
                    "social": {"linkedin_url": f"https://www.linkedin.com/in/jordan-{suffix}"},
                    "metadata": {"research_notes": "fixture"},
                }
            ),
            encoding="utf-8",
        )
        return raw, result

    def _params(
        self,
        rows: tuple[ResearchQueueRow, ...] | None = None,
    ) -> research_models.ResearchRunParams:
        return research_models.ResearchRunParams(
            output_dir=self.out,
            rows=(self.queue_row,) if rows is None else rows,
            manifest=str(self.manifest),
            db=self.db,
        )

    def test_typed_projections_keep_exact_paths_and_hashes(self) -> None:
        raw, result = self._write_result()
        (projected,) = projection.research_artifact_projections(self._params())
        self.assertEqual(Path(projected.artifact.path), result.resolve())
        self.assertEqual(Path(projected.raw_artifact.path), raw.resolve())
        self.assertEqual(
            projected.artifact.content_fingerprint,
            hashlib.sha256(result.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            projected.raw_artifact.content_fingerprint,
            hashlib.sha256(raw.read_bytes()).hexdigest(),
        )
        self.assertEqual(projected.artifact.parent_id, "parent-1")

    def test_missing_candidate_uses_row_key_without_inventing_public_identifier(self) -> None:
        self._write_result()
        row = replace(
            self.queue_row,
            candidate_exists=False,
            row_key="person-a",
            source_candidate_public_identifier="",
        )

        (projected,) = projection.research_artifact_projections(
            self._params(rows=(row,))
        )
        self.db.project_rows((projected,))

        self.assertEqual(projected.candidate.row_key, "person-a")
        self.assertEqual(projected.candidate.public_identifier, "jordan-one")
        self.assertEqual(projected.artifact.candidate_key, "person-a")
        self.assertEqual(projected.research.candidate_key, "person-a")
        self.assertEqual(
            query(
                self.db,
                "SELECT candidate_key FROM artifacts "
                "WHERE artifact_key='research:jordan-bravo'",
            )[0]["candidate_key"],
            "person-a",
        )

    def test_running_terminal_and_changed_projection_preserve_human_decision(self) -> None:
        self._write_result()
        params = self._params()
        driver.report_progress(
            params,
            "running",
            research_models.ResearchProgressCounts(1, 0, 1, 0),
            selection=self.selection,
        )
        receipt = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "running")
        self.assertNotIn("artifacts", receipt)
        self.assertFalse(query(self.db, "SELECT * FROM jobs"))
        self.db.decide_identity("candidate:email:jordan@example.com", "verify")

        driver.report_progress(
            params,
            "research_complete",
            research_models.ResearchProgressCounts(1, 1, 0, 0),
            projections=projection.research_artifact_projections(params),
            selection=self.selection,
        )
        first_artifacts = query(self.db, "SELECT count(*) FROM artifacts")[0][0]
        self.assertEqual(first_artifacts, 2)
        self.assertEqual(
            json.loads(self.manifest.read_text(encoding="utf-8"))["status"],
            "research_complete",
        )

        self._write_result("two")
        driver.report_progress(
            params,
            "research_complete",
            research_models.ResearchProgressCounts(1, 1, 0, 0),
            projections=projection.research_artifact_projections(params),
            selection=self.selection,
        )
        driver.report_progress(
            params,
            "research_complete",
            research_models.ResearchProgressCounts(1, 1, 0, 0),
            projections=projection.research_artifact_projections(params),
            selection=self.selection,
        )
        link = query(
            self.db,
            "SELECT machine_proposed_public_identifier, decision_action, decision_approved "
            "FROM links WHERE row_key='candidate:email:jordan@example.com'",
        )[0]
        self.assertEqual(tuple(link), (None, "verify", "yes"))
        self.assertEqual(query(self.db, "SELECT count(*) FROM artifacts")[0][0], first_artifacts)

    def test_failure_receipt_keeps_error_without_erasing_artifacts(self) -> None:
        self._write_result()
        params = self._params()
        driver.report_progress(
            params,
            "research_complete",
            research_models.ResearchProgressCounts(1, 1, 0, 0),
            projections=projection.research_artifact_projections(params),
            selection=self.selection,
        )
        driver.report_progress(
            params,
            "failed",
            research_models.ResearchProgressCounts(1, 0, 0, 1),
            selection=self.selection,
            error="provider failed",
        )
        receipt = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual((receipt["status"], receipt["error"]), ("failed", "provider failed"))
        self.assertNotIn("artifacts", receipt)
        self.assertFalse(query(self.db, "SELECT * FROM jobs"))
        self.assertEqual(query(self.db, "SELECT count(*) FROM artifacts")[0][0], 2)

    def test_zero_work_terminal_projects_empty_inventory(self) -> None:
        self._write_queue([])
        driver.report_progress(
            self._params(rows=()),
            "research_complete",
            research_models.ResearchProgressCounts(0, 0, 0, 0),
            selection=replace(self.selection, fingerprint="selection-empty"),
        )
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertNotIn("artifacts", payload)
        self.assertFalse(query(self.db, "SELECT * FROM jobs"))

    def test_reconcile_needs_approval_writes_then_projects_without_spend(self) -> None:
        facts = self.root / "facts"
        raw = self.root / "raw"
        facts.mkdir()
        raw.mkdir()
        subset = [EnrichmentQueueRow(
            "parent-1", "jordan-bravo", "Jordan Bravo", ("person-a",),
            "candidate:email:jordan@example.com", True, "", "", "",
            (), (), False,
        )]
        with (
            mock.patch.object(
                selection,
                "workflow_state",
                return_value=SimpleNamespace(
                    selection=ReviewSelection(
                        "selection-1", 1, 1, 0, 0, "revision-1"
                    )
                ),
            ),
            mock.patch.object(selection, "enrichment_queue", return_value=subset),
            mock.patch.object(selection, "build_queue", return_value=[self.queue_row]),
            mock.patch.object(driver, "run_research") as paid,
        ):
            node = reconcile.ReconcileDeepResearch(
                manifest=self.manifest,
                out_dir=self.out,
                queue_csv=self.queue,
                budget=1.0,
                approve=False,
                db=self.db,
            )
            node.run()
        paid.assert_not_called()
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "needs_approval")
        self.assertNotIn("artifacts", payload)
        self.assertFalse(query(self.db, "SELECT * FROM jobs"))

    def test_reconcile_without_receipt_still_reports_provider_and_judge_progress(self) -> None:
        plan = selection.ResearchSelection(
            fingerprint=ReviewSelection("selection-1", 1, 1, 0, 0, ""),
            eligible=(EnrichmentQueueRow(
                "parent-1", "jordan-bravo", "Jordan Bravo", ("person-a",),
                "candidate:email:jordan@example.com", True, "", "", "",
                (), (), True,
            ),),
            queue=(self.queue_row,),
            pending=(self.queue_row,),
            reused_completed=0,
            duplicate_handles=0,
            eligible_candidates=1,
            processor="core2x",
            cost_per_person_usd=0.05,
            estimated_usd=0.05,
        )
        progress: list[coordinator.ResearchProgress | coordinator.JudgingProgress] = []
        options = coordinator.ReconcileOptions(
            out_dir=self.out,
            queue_csv=self.queue,
            manifest_path=self.manifest,
            processor="core2x",
            confirm_threshold=0.8,
            budget=0.05,
            approve=True,
            dry_run=False,
            include_plausibly_absent=False,
            include_candidates=True,
            no_llm=False,
            model="test-model",
            reasoning_effort="medium",
            on_progress=progress.append,
            db=self.db,
            receipt=None,
        )

        def run(params):
            params.on_progress(research_models.ResearchProgress(
                "running", research_models.ResearchProgressCounts(1, 0, 1, 0)
            ))
            return research_models.ResearchRunResult("completed")

        def propose(*_args, heartbeat, **_kwargs):
            heartbeat(1, 1)
            return RetargetRunResult("", 0, 0, 0, 1, 0, 0)

        with (
            mock.patch.object(coordinator, "select_research", return_value=plan),
            mock.patch.object(coordinator, "write_queue"),
            mock.patch.object(driver, "run_research", side_effect=run),
            mock.patch.object(coordinator, "propose_retargets", side_effect=propose),
        ):
            result, receipt = coordinator.execute_reconcile(options)

        self.assertEqual(result["status"], "ran")
        self.assertEqual(receipt["status"], "research_complete")
        self.assertEqual(
            [
                event.to_payload().get("phase")
                for event in progress
            ],
            [None, "judging_retargets"],
        )

    def test_completed_with_errors_still_judges_the_successful_handles(self) -> None:
        """One bad handle must not discard a whole paid run.

        Regression for the bug where run_research's own "completed_with_errors"
        (fired whenever ANY handle in the batch errored, even the benign
        no-metadata-handle case) fell outside RESEARCH_OK_STATUSES: propose()
        was skipped entirely and the receipt's failed count claimed every
        pending row failed, not just the one that actually did.
        """
        second_row = replace(
            self.queue_row,
            handle="casey-delta",
            row_key="candidate:email:casey@example.com",
            source_candidate_public_identifier="candidate:email:casey@example.com",
        )
        plan = selection.ResearchSelection(
            fingerprint=ReviewSelection("selection-1", 2, 2, 0, 0, ""),
            eligible=(
                EnrichmentQueueRow(
                    "parent-1", "jordan-bravo", "Jordan Bravo", ("person-a",),
                    "candidate:email:jordan@example.com", True, "", "", "",
                    (), (), True,
                ),
                EnrichmentQueueRow(
                    "parent-2", "casey-delta", "Casey Delta", ("person-b",),
                    "candidate:email:casey@example.com", True, "", "", "",
                    (), (), True,
                ),
            ),
            queue=(self.queue_row, second_row),
            pending=(self.queue_row, second_row),
            reused_completed=0,
            duplicate_handles=0,
            eligible_candidates=2,
            processor="core2x",
            cost_per_person_usd=0.05,
            estimated_usd=0.10,
        )
        options = coordinator.ReconcileOptions(
            out_dir=self.out,
            queue_csv=self.queue,
            manifest_path=self.manifest,
            processor="core2x",
            confirm_threshold=0.8,
            budget=0.10,
            approve=True,
            dry_run=False,
            include_plausibly_absent=False,
            include_candidates=True,
            no_llm=False,
            model="test-model",
            reasoning_effort="medium",
            on_progress=None,
            db=self.db,
            receipt=None,
        )

        def run(_params):
            # 199-of-200-style partial batch: one handle errored (e.g. the
            # benign missing-metadata.handle case in parallel_client.py), the
            # rest — jordan-bravo — completed and billed.
            return research_models.ResearchRunResult(
                "completed_with_errors",
                errors=("casey-delta: result did not match a submitted subject",),
            )

        proposed_subsets: list[int] = []

        def propose(*args, heartbeat, **_kwargs):
            proposed_subsets.append(len(args[0]))
            heartbeat(len(args[0]), len(args[0]))
            return RetargetRunResult("", 1, 0, 1, 1, 0, 0)

        with (
            mock.patch.object(coordinator, "select_research", return_value=plan),
            mock.patch.object(coordinator, "write_queue"),
            mock.patch.object(driver, "run_research", side_effect=run),
            mock.patch.object(coordinator, "propose_retargets", side_effect=propose),
        ):
            result, receipt = coordinator.execute_reconcile(options)

        # propose() ran over both eligible rows — a partial-error batch is not
        # discarded as a total failure.
        self.assertEqual(proposed_subsets, [2])
        self.assertEqual(result["status"], "ran")
        self.assertEqual(result["research_status"], "completed_with_errors")
        self.assertEqual(result["retargets_proposed"], 1)
        self.assertEqual(receipt["status"], "research_complete")
        # Only the one handle that actually errored counts as failed — not the
        # whole two-row pending batch (the pre-fix bug this regresses).
        self.assertEqual(receipt["counts"]["failed"], 1)
        self.assertEqual(receipt["counts"]["completed"], 1)
        self.assertEqual(receipt["counts"]["total"], 2)


if __name__ == "__main__":
    unittest.main()
