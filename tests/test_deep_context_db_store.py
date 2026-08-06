"""Owned machine projection and atomic review reset transactions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.models import (
    IdentityMachineProjection,
    JobKind,
    JobRow,
    JobStatus,
    LinkRow,
    ParentRow,
    ReviewSource,
    SpendApprovalRow,
    StageStateRow,
    StageStatus,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from deep_context_sqlite_test_helpers import query


class DeepContextStoreTransactionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Db(Path(self.temp.name) / "deep-context.sqlite")
        self.db.project_rows(
            (
                ParentRow("parent-1", "parent-worth:parent-1"),
                LinkRow(
                    "candidate-1",
                    "parent-1",
                    "candidate-1",
                    "pub",
                    machine_action="verify",
                    machine_reason="old machine reason",
                    source=ReviewSource.RECONCILE.value,
                ),
            )
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_machine_projection_preserves_human_and_base_columns(self) -> None:
        self.db.decide_identity(
            "candidate-1",
            "retarget",
            replacement_url="https://www.linkedin.com/in/jordan-bravo",
        )
        self.db.project_identity(
            (
                IdentityMachineProjection(
                    "candidate-1",
                    machine_action="detach",
                    machine_approved="auto",
                    machine_confidence=0.93,
                    machine_reason="fresh judgment",
                    machine_judgment="wrong_person",
                    authoritative_detach=1,
                    source=ReviewSource.HEAL.value,
                ),
            )
        )
        row = query(self.db, "SELECT * FROM links WHERE row_key='candidate-1'")[0]
        self.assertEqual((row["parent_id"], row["kind"]), ("parent-1", "pub"))
        self.assertEqual((row["machine_action"], row["machine_reason"]), ("detach", "fresh judgment"))
        self.assertEqual((row["decision_action"], row["decision_approved"]), ("retarget", "yes"))
        self.assertEqual(row["replacement_url"], "https://www.linkedin.com/in/jordan-bravo")

    def test_machine_projection_batch_rolls_back_on_unknown_candidate(self) -> None:
        with self.assertRaisesRegex(StoreError, "unknown candidate"):
            self.db.project_identity(
                (
                    IdentityMachineProjection("candidate-1", machine_action="detach"),
                    IdentityMachineProjection("missing", machine_action="verify"),
                )
            )
        row = query(self.db, "SELECT machine_action, machine_reason FROM links WHERE row_key='candidate-1'")[0]
        self.assertEqual(tuple(row), ("verify", "old machine reason"))

    def test_review_reset_is_atomic_and_preserves_machine_jobs(self) -> None:
        self.db.decide_worth("parent-1", "yes", note="keep in network")
        self.db.decide_identity("candidate-1", "detach")
        self.db.save_state(StageStateRow("worth", StageStatus.COMPLETE.value, "selection-1", "artifact-1"))
        self.db.save_state(SpendApprovalRow("worth", "selection-1", 1, 0.05))
        self.db.save_state(
            JobRow(
                "enrichment-job",
                JobKind.ENRICHMENT.value,
                JobStatus.APPLIED.value,
                completed_count=1,
                total_count=1,
            )
        )

        counts = self.db.reset_review()

        self.assertEqual((counts.human_worth_cleared, counts.human_identity_cleared), (1, 1))
        parent = query(self.db, "SELECT * FROM parents WHERE parent_id='parent-1'")[0]
        link = query(self.db, "SELECT * FROM links WHERE row_key='candidate-1'")[0]
        self.assertIsNone(parent["human_worth"])
        self.assertEqual((link["decision_action"], link["replacement_url"]), (None, None))
        self.assertEqual((link["machine_action"], link["machine_reason"]), ("verify", "old machine reason"))
        self.assertEqual(query(self.db, "SELECT status FROM stage_state")[0][0], "pending")
        self.assertEqual(query(self.db, "SELECT count(*) FROM spend_approvals")[0][0], 0)
        self.assertEqual(query(self.db, "SELECT status FROM jobs")[0][0], "applied")


if __name__ == "__main__":
    unittest.main()
