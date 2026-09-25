"""Owned machine projection and atomic review reset transactions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    IdentityMachineProjection,
    LinkRow,
    MergeVerdictRow,
    ParentRow,
    PersonRow,
    WriterSource,
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
                    source=WriterSource.RECONCILE.value,
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
        self.db.project_rows(
            (
                IdentityMachineProjection(
                    "candidate-1",
                    machine_action="detach",
                    machine_approved="auto",
                    machine_confidence=0.93,
                    machine_reason="fresh judgment",
                    machine_judgment="wrong_person",
                    authoritative_detach=1,
                    source=WriterSource.HEAL.value,
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
            self.db.project_rows(
                (
                    IdentityMachineProjection(
                        "candidate-1", machine_action="detach", source=WriterSource.RECONCILE.value
                    ),
                    IdentityMachineProjection(
                        "missing", machine_action="verify", source=WriterSource.RECONCILE.value
                    ),
                )
            )
        row = query(self.db, "SELECT machine_action, machine_reason FROM links WHERE row_key='candidate-1'")[0]
        self.assertEqual(tuple(row), ("verify", "old machine reason"))

    def test_review_reset_is_atomic_and_preserves_machine_state(self) -> None:
        self.db.decide_worth("parent-1", "yes", note="keep in network")
        self.db.decide_identity("candidate-1", "detach")

        counts = self.db.reset_review()

        self.assertEqual((counts.human_worth_cleared, counts.human_identity_cleared), (1, 1))
        parent = query(self.db, "SELECT * FROM parents WHERE parent_id='parent-1'")[0]
        link = query(self.db, "SELECT * FROM links WHERE row_key='candidate-1'")[0]
        self.assertIsNone(parent["human_worth"])
        self.assertEqual((link["decision_action"], link["replacement_url"]), (None, None))
        self.assertEqual((link["machine_action"], link["machine_reason"]), ("verify", "old machine reason"))

    def test_merge_verdict_cache_upserts_atomically(self) -> None:
        self.db.project_rows((
            PersonRow("person-a", "parent-1"),
            PersonRow("person-b", "parent-1"),
        ))
        first = MergeVerdictRow(
            "person-a", "person-b", "jordan-a", "jordan-b", "sig-1",
            "llm", 1, 0.91, 1, "same person", 1,
        )
        self.db.replace_merge_verdicts((first,))
        self.assertEqual(
            tuple(query(self.db, "SELECT signature, accepted FROM merge_verdicts")[0]),
            ("sig-1", 1),
        )
        self.db.replace_merge_verdicts(())
        self.assertEqual(query(self.db, "SELECT count(*) FROM merge_verdicts")[0][0], 1)
        with self.assertRaisesRegex(StoreError, "ordered and distinct"):
            self.db.replace_merge_verdicts((
                MergeVerdictRow(
                    "person-b", "person-a", "b", "a", "sig", "llm",
                    1, 0.9, 1,
                ),
            ))

    def test_merge_verdict_update_preserves_unrelated_paid_cache(self) -> None:
        self.db.project_rows((
            PersonRow("person-a", "parent-1"),
            PersonRow("person-b", "parent-1"),
            PersonRow("person-c", "parent-1"),
        ))
        first = MergeVerdictRow(
            "person-a", "person-b", "a", "b", "sig-ab", "llm", 0, 0.8, 1,
        )
        second = MergeVerdictRow(
            "person-b", "person-c", "b", "c", "sig-bc", "llm", 1, 0.9, 1,
        )
        self.db.replace_merge_verdicts((first,))
        self.db.replace_merge_verdicts((second,))

        rows = query(
            self.db,
            "SELECT person_a, person_b, signature FROM merge_verdicts "
            "ORDER BY person_a, person_b",
        )
        self.assertEqual([tuple(row) for row in rows], [
            ("person-a", "person-b", "sig-ab"),
            ("person-b", "person-c", "sig-bc"),
        ])

    def test_open_normalizes_sqlite_errors_to_store_error(self) -> None:
        broken = Path(self.temp.name) / "broken.sqlite"
        broken.write_text("not a sqlite database", encoding="utf-8")

        with self.assertRaisesRegex(StoreError, "cannot open Deep Context database"):
            Db(broken)


if __name__ == "__main__":
    unittest.main()


class Foreign_key_delta(unittest.TestCase):
    """project_rows validates the FK delta, not the whole world."""

    def _planted_orphan(self, root: Path) -> Db:
        db = Db(root / "t.sqlite")
        db.project_rows((
            ParentRow("parent-1", "p1", display_name="P One"),
            PersonRow("person-1", "parent-1"),
            ArtifactRow("research:x", "research", "parent-1", "/dev/null",
                        "fp", "projected"),
        ))
        # Bypass the store (foreign_keys OFF) exactly like a raw sqlite3 CLI
        # delete: artifact gone, dependent research row orphaned.
        import sqlite3 as _s
        conn = _s.connect(root / "t.sqlite")
        conn.execute("DELETE FROM artifacts WHERE artifact_key='research:x'")
        conn.commit()
        conn.close()
        return db

    def test_pre_existing_orphan_does_not_block_unrelated_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = self._planted_orphan(Path(directory))
            db.project_rows((
                ParentRow("parent-2", "p2", display_name="P Two"),
                PersonRow("person-2", "parent-2"),
            ))  # must not raise

    def test_healing_upsert_lands(self) -> None:
        from packs.ingestion.primitives.deep_context.db.models import ResearchRow, ResearchStatus
        with tempfile.TemporaryDirectory() as directory:
            db = self._planted_orphan(Path(directory))
            # Re-project the artifact + its research row: heals the orphan.
            db.project_rows((
                ArtifactRow("research:x", "research", "parent-1", "/dev/null",
                            "fp2", "projected"),
                ResearchRow("x", "parent-1", ResearchStatus.COMPLETE.value,
                            None, "research:x", "{}", "2026-01-01T00:00:00Z"),
            ))
            row = db.query("SELECT artifact_key FROM research WHERE handle='x'")[0]
            self.assertEqual(row["artifact_key"], "research:x")
