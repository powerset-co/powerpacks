"""SQLite-only review restart CLI coverage."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from packs.ingestion.primitives.deep_context import restart_review
from packs.ingestion.primitives.deep_context.db.models import (
    JobKind,
    JobRow,
    JobStatus,
    LinkRow,
    ParentRow,
    SpendApprovalRow,
    StageStateRow,
    StageStatus,
)
from packs.ingestion.primitives.deep_context.db.store import Db


class RestartReviewSqliteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "deep-context.sqlite"
        self.db = Db(self.db_path)
        self.db.project_parent(ParentRow(
            "parent-1", "jordan-bravo", machine_worth="maybe",
            machine_worth_reason="machine reason",
        ))
        self.db.project_candidate(LinkRow(
            "candidate-1", "parent-1", "jordan-bravo", "pub",
            machine_action="verify", machine_approved="auto",
            machine_reason="machine identity reason",
        ))
        self.db.set_worth("parent-1", "yes", note="human note")
        self.db.settle_identity("candidate-1", "detach")
        self.db.save_stage(StageStateRow(
            "worth", StageStatus.COMPLETE.value, "selection-1", "artifact-1",
        ))
        self.db.approve_spend(SpendApprovalRow("worth", "selection-1", 1, 0.05))
        self.db.save_job(JobRow(
            "enrichment-job", JobKind.ENRICHMENT.value, JobStatus.APPLIED.value,
            completed_count=1, total_count=1,
        ))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_preview_is_read_only(self) -> None:
        counts = restart_review.preview_reset(self.db)

        self.assertEqual(
            (counts.human_worth_cleared, counts.human_identity_cleared,
             counts.stage_states_reset, counts.spend_approvals_cleared),
            (1, 1, 1, 1),
        )
        self.assertEqual(
            self.db.query("SELECT human_worth FROM parents")[0][0], "yes"
        )
        self.assertEqual(
            self.db.query("SELECT decision_action FROM links")[0][0], "detach"
        )

    def test_apply_uses_atomic_db_reset_and_preserves_machine_work(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = restart_review.main(["--db", str(self.db_path), "--apply"])

        self.assertEqual(result, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "applied")
        self.assertEqual(payload["human_worth_cleared"], 1)
        self.assertEqual(payload["human_identity_cleared"], 1)
        parent = self.db.query("SELECT * FROM parents")[0]
        link = self.db.query("SELECT * FROM links")[0]
        self.assertIsNone(parent["human_worth"])
        self.assertEqual(parent["machine_worth"], "maybe")
        self.assertIsNone(link["decision_action"])
        self.assertEqual(link["machine_action"], "verify")
        self.assertEqual(self.db.query("SELECT status FROM stage_state")[0][0], "pending")
        self.assertEqual(self.db.query("SELECT count(*) FROM spend_approvals")[0][0], 0)
        self.assertEqual(self.db.query("SELECT status FROM jobs")[0][0], "applied")


if __name__ == "__main__":
    unittest.main()
