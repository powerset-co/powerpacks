"""SQLite-only review restart CLI coverage."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from packs.ingestion.primitives.deep_context.review import restart_review
from packs.ingestion.primitives.deep_context.db.models import (
    JobKind,
    JobRow,
    JobStatus,
    LinkRow,
    ParentRow,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from deep_context_sqlite_test_helpers import query


class RestartReviewSqliteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "deep-context.sqlite"
        self.db = Db(self.db_path)
        self.db.project_rows(
            (
                ParentRow(
                    "parent-1",
                    "jordan-bravo",
                    machine_worth="maybe",
                    machine_worth_reason="machine reason",
                ),
                LinkRow(
                    "candidate-1",
                    "parent-1",
                    "jordan-bravo",
                    "pub",
                    machine_action="verify",
                    machine_approved="auto",
                    machine_reason="machine identity reason",
                    source=WriterSource.RECONCILE.value,
                ),
            )
        )
        self.db.decide_worth("parent-1", "yes", note="human note")
        self.db.decide_identity("candidate-1", "detach")
        self.db.project_rows((
            JobRow(
                "enrichment-job",
                JobKind.ENRICHMENT.value,
                JobStatus.APPLIED.value,
                completed_count=1,
                total_count=1,
            ),
        ))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_preview_is_read_only(self) -> None:
        counts = self.db.reset_review(apply=False)

        self.assertEqual(
            (counts.human_worth_cleared, counts.human_identity_cleared),
            (1, 1),
        )
        self.assertEqual(query(self.db, "SELECT human_worth FROM parents")[0][0], "yes")
        self.assertEqual(query(self.db, "SELECT decision_action FROM links")[0][0], "detach")

    def test_apply_uses_atomic_db_reset_and_preserves_machine_work(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = restart_review.main(["--db", str(self.db_path), "--apply"])

        self.assertEqual(result, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "applied")
        self.assertEqual(payload["human_worth_cleared"], 1)
        self.assertEqual(payload["human_identity_cleared"], 1)
        parent = query(self.db, "SELECT * FROM parents")[0]
        link = query(self.db, "SELECT * FROM links")[0]
        self.assertIsNone(parent["human_worth"])
        self.assertEqual(parent["machine_worth"], "maybe")
        self.assertIsNone(link["decision_action"])
        self.assertEqual(link["machine_action"], "verify")
        self.assertEqual(query(self.db, "SELECT status FROM jobs")[0][0], "applied")


if __name__ == "__main__":
    unittest.main()
