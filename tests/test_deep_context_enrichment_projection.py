"""Typed Parallel outputs projected explicitly before display-only receipts."""

from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context import deep_research_contacts as research
from packs.ingestion.primitives.deep_context import reconcile_deep_research as reconcile
from packs.ingestion.primitives.deep_context.parallel_research import driver
from packs.ingestion.primitives.deep_context.research_reconcile import coordinator, selection
from packs.ingestion.primitives.deep_context.research_reconcile.selection import QUEUE_FIELDS
from packs.ingestion.primitives.deep_context.db.models import (
    LinkRow,
    ParentRow,
    PersonRow,
    RowKind,
)
from packs.ingestion.primitives.deep_context.db.store import Db
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
                ),
            )
        )
        self.queue_row = {
            "parent_id": "parent-1",
            "candidate_exists": "1",
            "handle": "jordan-bravo",
            "source_parent_slug": "jordan-bravo",
            "source_person_ids": json.dumps(["person-a"]),
            "source_candidate_public_identifier": "candidate:email:jordan@example.com",
            "display_name": "Jordan Bravo",
            "bio": "Known collaborator",
            "known_info": "Synthetic fixture",
            "primary_email": "jordan@example.com",
            "phone_e164": "",
            "area_code": "",
            "source_channel": "email",
            "retarget_hint": "Find the correct profile",
        }
        self._write_queue([self.queue_row])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_queue(self, rows: list[dict[str, str]]) -> None:
        with self.queue.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=QUEUE_FIELDS)
            writer.writeheader()
            writer.writerows(
                {field: row.get(field, "") for field in QUEUE_FIELDS}
                for row in rows
            )

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
        rows: tuple[dict[str, str], ...] | None = None,
    ) -> research.ResearchRunParams:
        return research.ResearchRunParams(
            output_dir=self.out,
            rows=(self.queue_row,) if rows is None else rows,
            manifest=str(self.manifest),
            db=self.db,
        )

    def test_typed_projections_keep_exact_paths_and_hashes(self) -> None:
        raw, result = self._write_result()
        (projection,) = driver.research_artifact_projections(self._params())
        self.assertEqual(Path(projection.artifact.path), result.resolve())
        self.assertEqual(Path(projection.raw_artifact.path), raw.resolve())
        self.assertEqual(
            projection.artifact.content_fingerprint,
            hashlib.sha256(result.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            projection.raw_artifact.content_fingerprint,
            hashlib.sha256(raw.read_bytes()).hexdigest(),
        )
        self.assertEqual(projection.artifact.parent_id, "parent-1")

    def test_running_terminal_and_changed_projection_preserve_human_decision(self) -> None:
        self._write_result()
        params = self._params()
        driver.report_progress(
            params,
            "running",
            {"total": 1, "completed": 0, "pending": 1, "failed": 0},
            selection={"fingerprint": "selection-1"},
        )
        receipt = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "running")
        self.assertNotIn("artifacts", receipt)
        self.assertFalse(query(self.db, "SELECT * FROM jobs"))
        self.db.decide_identity("candidate:email:jordan@example.com", "verify")

        driver.report_progress(
            params,
            "research_complete",
            {"total": 1, "completed": 1, "pending": 0, "failed": 0},
            projections=driver.research_artifact_projections(params),
            selection={"fingerprint": "selection-1"},
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
            {"total": 1, "completed": 1, "pending": 0, "failed": 0},
            projections=driver.research_artifact_projections(params),
            selection={"fingerprint": "selection-1"},
        )
        driver.report_progress(
            params,
            "research_complete",
            {"total": 1, "completed": 1, "pending": 0, "failed": 0},
            projections=driver.research_artifact_projections(params),
            selection={"fingerprint": "selection-1"},
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
            {"total": 1, "completed": 1, "pending": 0, "failed": 0},
            projections=driver.research_artifact_projections(params),
            selection={"fingerprint": "selection-1"},
        )
        driver.report_progress(
            params,
            "failed",
            {"total": 1, "completed": 0, "pending": 0, "failed": 1},
            selection={"fingerprint": "selection-1"},
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
            {"total": 0, "completed": 0, "pending": 0, "failed": 0},
            selection={"fingerprint": "selection-empty"},
        )
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertNotIn("artifacts", payload)
        self.assertFalse(query(self.db, "SELECT * FROM jobs"))

    def test_reconcile_needs_approval_writes_then_projects_without_spend(self) -> None:
        facts = self.root / "facts"
        raw = self.root / "raw"
        facts.mkdir()
        raw.mkdir()
        subset = [
            {
                "parent_slug": "jordan-bravo",
                "person_ids": ["person-a"],
                "candidate_key": "candidate:email:jordan@example.com",
                "name": "Jordan Bravo",
                "linkedin": {},
                "verdict": {},
                "match_emails": [],
                "match_phones": [],
            }
        ]
        with (
            mock.patch.object(
                selection,
                "workflow_state",
                return_value={
                    "selection": {
                        "sha256": "selection-1",
                        "review_revision": "revision-1",
                    }
                },
            ),
            mock.patch.object(selection, "linkedin_review", return_value=subset),
            mock.patch.object(selection, "build_queue", return_value=[self.queue_row]),
            mock.patch.object(coordinator, "run_research") as paid,
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
            fingerprint={"fingerprint": "selection-1", "sha256": "selection-1"},
            eligible=({"parent_id": "parent-1"},),
            queue=(self.queue_row,),
            pending=(self.queue_row,),
            reused_completed=0,
            duplicate_handles=0,
            eligible_candidates=1,
            processor="core2x",
            cost_per_person_usd=0.05,
            estimated_usd=0.05,
        )
        progress: list[dict[str, object]] = []
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
            params.on_progress({
                "status": "running",
                "counts": {"total": 1, "completed": 0, "pending": 1, "failed": 0},
            })
            return {"status": "completed"}

        def propose(*_args, heartbeat, **_kwargs):
            heartbeat(1, 1)
            return {
                "proposed": 0,
                "judge_calls": 1,
                "cached_verdicts": 0,
                "grandfathered": 0,
            }

        with (
            mock.patch.object(coordinator, "select_research", return_value=plan),
            mock.patch.object(coordinator, "write_queue"),
            mock.patch.object(coordinator, "run_research", side_effect=run),
            mock.patch.object(coordinator, "propose_retargets", side_effect=propose),
        ):
            result, receipt = coordinator.execute_reconcile(options)

        self.assertEqual(result["status"], "ran")
        self.assertEqual(receipt["status"], "research_complete")
        self.assertEqual(
            [event.get("phase") for event in progress],
            [None, "judging_retargets"],
        )


if __name__ == "__main__":
    unittest.main()
