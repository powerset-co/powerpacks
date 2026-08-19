"""Typed Parallel outputs projected explicitly before display-only receipts."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from parallel.types import TaskRunJsonOutput

from packs.ingestion.primitives.deep_context.enrich.research_reconcile import (
    coordinator,
)
from packs.ingestion.primitives.deep_context.manifests.receipt_counts import ReceiptCounts
from packs.ingestion.primitives.deep_context.enrich.parallel_research import driver, projection
from packs.ingestion.primitives.deep_context.enrich.parallel_research import models as research_models
from packs.ingestion.primitives.deep_context.enrich.parallel_research.queue import (
    ResearchQueueRow,
)
from packs.ingestion.primitives.deep_context.enrich.parallel_research.result import ResearchResult
from packs.ingestion.primitives.deep_context.enrich.research_reconcile import selection
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.models import (
    EnrichmentProgress,
    RetargetRunResult,
)
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
            source_person_ids=("person-a",),
            display_name="Jordan Bravo",
            bio="Known collaborator",
            known_info="Synthetic fixture",
            primary_email="jordan@example.com",
            retarget_hint="Find the correct profile",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_result(self, suffix: str = "one") -> tuple[Path, bytes, object]:
        person = self.out / "jordan-bravo"
        person.mkdir(exist_ok=True)
        path = person / "00_parallel_result.json"
        output = TaskRunJsonOutput.model_validate({
            "type": "json",
            "content": {
                "real_name": "Jordan Bravo",
                "work_experience": [{"title": "Founder", "company_name": "Example", "is_current": True}],
                "education": [],
                "location_city": "Oakland",
                "location_country": "US",
                "linkedin_url": f"https://www.linkedin.com/in/jordan-{suffix}",
                "summary": "Founder",
            },
            "basis": [{"field": "linkedin_url", "reasoning": "fixture", "citations": []}],
        })
        profile = ResearchResult.from_output(output)
        data = (json.dumps(output.model_dump(mode="json"), sort_keys=True) + "\n").encode()
        path.write_bytes(data)
        return path, data, profile

    def _projection(self, row: ResearchQueueRow | None = None, suffix: str = "one"):
        path, data, result = self._write_result(suffix)
        return projection.research_artifact_projection(
            self._params(rows=((row or self.queue_row),)), row or self.queue_row, result, path, data
        )

    def _params(
        self,
        rows: tuple[ResearchQueueRow, ...] | None = None,
    ) -> research_models.ResearchRunParams:
        return research_models.ResearchRunParams(
            output_dir=self.out,
            rows=(self.queue_row,) if rows is None else rows,
            db=self.db,
        )

    def test_typed_projections_keep_exact_paths_and_hashes(self) -> None:
        result, data, profile = self._write_result()
        projected = projection.research_artifact_projection(
            self._params(), self.queue_row, profile, result, data
        )
        self.assertEqual(Path(projected.artifact.path), result.resolve())
        self.assertIsNone(projected.raw_artifact)
        self.assertEqual(
            projected.artifact.content_fingerprint,
            hashlib.sha256(data).hexdigest(),
        )
        self.assertEqual(projected.artifact.parent_id, "parent-1")

    def test_missing_candidate_uses_row_key_without_inventing_public_identifier(self) -> None:
        row = replace(
            self.queue_row,
            candidate_exists=False,
            row_key="person-a",
        )

        projected = self._projection(row)
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

    def test_changed_projection_preserves_human_decision(self) -> None:
        self._write_result()
        self.db.decide_identity("candidate:email:jordan@example.com", "verify")

        self.db.project_rows((self._projection(),))
        first_artifacts = query(self.db, "SELECT count(*) FROM artifacts")[0][0]
        self.assertEqual(first_artifacts, 1)

        self.db.project_rows((self._projection(suffix="two"),))
        self.db.project_rows((self._projection(),))
        link = query(
            self.db,
            "SELECT machine_proposed_public_identifier, decision_action, decision_approved "
            "FROM links WHERE row_key='candidate:email:jordan@example.com'",
        )[0]
        self.assertEqual(tuple(link), (None, "verify", "yes"))
        self.assertEqual(query(self.db, "SELECT count(*) FROM artifacts")[0][0], first_artifacts)

    def test_reconcile_needs_approval_without_spend(self) -> None:
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
            node = coordinator.ReconcileDeepResearch(
                out_dir=self.out,
                budget=1.0,
                approve=False,
                db=self.db,
            )
            result = node.run().to_payload()
        paid.assert_not_called()
        self.assertEqual(result["status"], "needs_approval")
        self.assertFalse(self.manifest.exists())

    def test_reconcile_without_receipt_still_reports_provider_and_judge_progress(self) -> None:
        plan = selection.ResearchSelection(
            fingerprint=ReviewSelection("selection-1", 1, 1, 0, 0, ""),
            request_fingerprint="request-1",
            eligible=(EnrichmentQueueRow(
                "parent-1", "jordan-bravo", "Jordan Bravo", ("person-a",),
                "candidate:email:jordan@example.com", True, "", "", "",
                (), (), True,
            ),),
            pending=(self.queue_row,),
            reused_completed=0,
            duplicate_handles=0,
            eligible_candidates=1,
            processor="core2x",
            cost_per_person_usd=0.05,
            estimated_usd=0.05,
        )
        progress: list[EnrichmentProgress] = []
        node = coordinator.ReconcileDeepResearch(
            db=self.db,
            out_dir=self.out,
            processor="core2x",
            confirm_threshold=0.8,
            budget=0.05,
            approve=True,
            dry_run=False,
            include_plausibly_absent=False,
            model="test-model",
            reasoning_effort="medium",
            on_progress=progress.append,
        )

        def run(params):
            params.on_progress(ReceiptCounts(1, 1, 0, 0))
            return research_models.ResearchRunResult(1, completed=1)

        def propose(*_args, heartbeat, **_kwargs):
            heartbeat(1, 1)
            return RetargetRunResult(
                proposed=0,
                judge_calls=1,
                cached_verdicts=0,
                grandfathered=0,
            )

        with (
            mock.patch.object(coordinator, "select_research", return_value=plan),
            mock.patch.object(driver, "run_research", side_effect=run),
            mock.patch.object(coordinator, "propose_retargets", side_effect=propose),
        ):
            payload = node.run().to_payload()

        self.assertEqual(payload["status"], "ran")
        self.assertEqual(
            [
                event.phase
                for event in progress
            ],
            ["research", "research", "judging_retargets"],
        )

    def test_receipt_counts_never_include_duplicate_handles(self) -> None:
        """A duplicate handle is never queued or billed, so no receipt counts it.

        Locks the finish path to the same deduped reused + pending basis the
        driver's mid-run counts use. Pre-fix, the finish path counted
        the undeduplicated queue: the duplicate showed up as researched and the total
        jumped from 1 (mid-run) to 2 (final receipt).
        """
        eligible_row = EnrichmentQueueRow(
            "parent-1", "jordan-bravo", "Jordan Bravo", ("person-a",),
            "candidate:email:jordan@example.com", True, "", "", "",
            (), (), True,
        )
        plan = selection.ResearchSelection(
            fingerprint=ReviewSelection("selection-1", 2, 2, 0, 0, ""),
            request_fingerprint="request-1",
            eligible=(eligible_row, eligible_row),
            # Two eligible rows collapsed to one handle: one reused artifact,
            # one duplicate, nothing pending.
            pending=(),
            reused_completed=1,
            duplicate_handles=1,
            eligible_candidates=2,
            processor="core2x",
            cost_per_person_usd=0.05,
            estimated_usd=0.0,
        )
        node = coordinator.ReconcileDeepResearch(
            db=self.db,
            out_dir=self.out,
            processor="core2x",
            confirm_threshold=0.8,
            budget=0.0,
            approve=True,
            dry_run=False,
            include_plausibly_absent=False,
            model="test-model",
            reasoning_effort="medium",
        )
        with (
            mock.patch.object(coordinator, "select_research", return_value=plan),
            mock.patch.object(
                coordinator,
                "propose_retargets",
                return_value=RetargetRunResult(0, 0, 0, 0),
            ),
        ):
            payload = node.run().to_payload()

        self.assertEqual(payload["status"], "reused")
        self.assertEqual(payload["duplicate_handles"], 1)
        # Round-trip through JSON: statuses serialize as the plain strings and
        # the counts stay on the deduped basis (total 1, not len(queue) == 2).
        parsed = json.loads(json.dumps(payload))
        self.assertEqual(parsed["status"], "reused")
        self.assertEqual(
            parsed["counts"],
            {"total": 1, "completed": 1, "pending": 0, "failed": 0},
        )
        self.assertEqual(parsed["duplicate_handles"], 1)
        self.assertEqual(parsed["reused_completed"], 1)

    def test_reused_parallel_output_still_requires_approval_before_judging(self) -> None:
        plan = selection.ResearchSelection(
            fingerprint=ReviewSelection("selection-1", 1, 1, 0, 0, ""),
            request_fingerprint="request-1",
            eligible=(
                EnrichmentQueueRow(
                    "parent-1", "jordan-bravo", "Jordan Bravo", ("person-a",),
                    "candidate:email:jordan@example.com", True, "", "", "",
                    (), (), True,
                ),
            ),
            pending=(),
            reused_completed=1,
            duplicate_handles=0,
            eligible_candidates=1,
            processor="core2x",
            cost_per_person_usd=0.05,
            estimated_usd=0.0,
        )
        node = coordinator.ReconcileDeepResearch(
            db=self.db,
            out_dir=self.out,
            processor="core2x",
            confirm_threshold=0.8,
            budget=0.0,
            approve=False,
            dry_run=False,
            include_plausibly_absent=False,
            model="test-model",
            reasoning_effort="medium",
        )
        with (
            mock.patch.object(coordinator, "select_research", return_value=plan),
            mock.patch.object(
                coordinator,
                "propose_retargets",
                side_effect=AssertionError("approval gate must precede paid follow-up"),
            ),
        ):
            payload = node.run().to_payload()

        self.assertEqual(payload["status"], "needs_approval")
        self.assertIn("Parallel-only", payload["message"])

    def test_partial_results_still_judge_successful_handles(self) -> None:
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
        )
        plan = selection.ResearchSelection(
            fingerprint=ReviewSelection("selection-1", 2, 2, 0, 0, ""),
            request_fingerprint="request-1",
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
            pending=(self.queue_row, second_row),
            reused_completed=0,
            duplicate_handles=0,
            eligible_candidates=2,
            processor="core2x",
            cost_per_person_usd=0.05,
            estimated_usd=0.10,
        )
        node = coordinator.ReconcileDeepResearch(
            db=self.db,
            out_dir=self.out,
            processor="core2x",
            confirm_threshold=0.8,
            budget=0.10,
            approve=True,
            dry_run=False,
            include_plausibly_absent=False,
            model="test-model",
            reasoning_effort="medium",
        )

        def run(_params):
            # 199-of-200-style partial batch: one handle errored (e.g. the
            # benign missing-metadata.handle case in parallel_client.py), the
            # rest — jordan-bravo — completed and billed.
            return research_models.ResearchRunResult(
                2,
                completed=1,
                errors=("casey-delta: result did not match a submitted subject",),
            )

        proposed_subsets: list[int] = []

        def propose(*args, heartbeat, **_kwargs):
            proposed_subsets.append(len(args[0]))
            heartbeat(len(args[0]), len(args[0]))
            return RetargetRunResult(1, 1, 0, 0)

        with (
            mock.patch.object(coordinator, "select_research", return_value=plan),
            mock.patch.object(driver, "run_research", side_effect=run),
            mock.patch.object(coordinator, "propose_retargets", side_effect=propose),
        ):
            payload = node.run().to_payload()

        # propose() ran over both eligible rows — a partial-error batch is not
        # discarded as a total failure.
        self.assertEqual(proposed_subsets, [2])
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["retargets_proposed"], 1)
        self.assertEqual(
            payload["errors"],
            ["casey-delta: result did not match a submitted subject"],
        )
        self.assertIn("casey-delta", payload["error"])
        # Only the one handle that actually errored counts as failed — not the
        # whole two-row pending batch (the pre-fix bug this regresses).
        self.assertEqual(payload["counts"]["failed"], 1)
        self.assertEqual(payload["counts"]["completed"], 1)
        self.assertEqual(payload["counts"]["total"], 2)


if __name__ == "__main__":
    unittest.main()
