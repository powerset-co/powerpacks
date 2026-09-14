from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from packs.ingestion.primitives.deep_context.db.identity_policy import IdentityPolicy
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactRow,
    CandidatePeopleProjection,
    CandidatePersonRow,
    FactRow,
    GuidanceRow,
    LinkRow,
    ParentRow,
    PersonRow,
    ProjectionStatus,
    ResearchRow,
    ResearchStatus,
    ReviewAction,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from deep_context_sqlite_test_helpers import query


class IncrementalParentMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Db(Path(self.temp.name) / "deep-context.sqlite")
        self.db.project_rows((
            ParentRow(
                "survivor", "survivor", display_slug="survivor-slug", machine_worth="yes",
            ),
            ParentRow(
                "absorbed", "absorbed", display_slug="absorbed-slug", machine_worth="no",
            ),
            PersonRow("person-survivor", "survivor", parent_slug="survivor-slug"),
            PersonRow("person-absorbed", "absorbed", parent_slug="absorbed-slug"),
            LinkRow(
                "link-survivor", "survivor", "link-survivor", "pub",
                source=WriterSource.RECONCILE.value,
            ),
            LinkRow(
                "link-absorbed", "absorbed", "link-absorbed", "pub",
                source=WriterSource.RECONCILE.value,
            ),
            LinkRow(
                "machine-survivor", "survivor", "machine-survivor", "pub",
                machine_action="verify", machine_approved="auto",
                source=WriterSource.RECONCILE.value,
            ),
            LinkRow(
                "machine-absorbed", "absorbed", "machine-absorbed", "pub",
                machine_action="verify", machine_approved="auto",
                source=WriterSource.RECONCILE.value,
            ),
            CandidatePeopleProjection(
                "link-absorbed",
                (CandidatePersonRow("link-absorbed", "person-absorbed", "absorbed"),),
            ),
            ArtifactRow(
                "facts:absorbed", ArtifactKind.FACTS.value, "absorbed",
                "/tmp/absorbed.jsonl", "sha256:facts", ProjectionStatus.PROJECTED.value,
                person_id="person-absorbed",
            ),
            ArtifactRow(
                "research:absorbed", ArtifactKind.RESEARCH.value, "absorbed",
                "/tmp/research.json", "sha256:research", ProjectionStatus.PROJECTED.value,
                candidate_key="link-absorbed",
            ),
            FactRow(
                "person-absorbed", "absorbed", "facts:absorbed",
                person_id="person-absorbed", facts_json="{}",
            ),
            ResearchRow(
                "absorbed-research", "absorbed", ResearchStatus.COMPLETE.value,
                candidate_key="link-absorbed", artifact_key="research:absorbed",
            ),
            GuidanceRow(
                "absorbed-guidance", "absorbed", "Jordan Bravo",
                candidate_key="link-absorbed",
            ),
        ))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_merge_repoints_every_parent_owner_and_runs_identity_policy(self) -> None:
        self.db.decide_worth(
            "survivor", "yes", note="older", decided_at="2026-08-05T01:00:00Z",
        )
        self.db.decide_worth(
            "absorbed", "no", note="newer", decided_at="2026-08-05T02:00:00Z",
        )
        self.db.decide_identity(
            "link-survivor", ReviewAction.VERIFY.value,
            decided_at="2026-08-05T03:00:00Z",
        )
        self.db.decide_identity(
            "link-absorbed", ReviewAction.VERIFY.value,
            decided_at="2026-08-05T04:00:00Z",
        )

        self.db.merge_parents("survivor", "absorbed")

        parent = query(self.db, "SELECT * FROM parents")[0]
        self.assertEqual(parent["parent_id"], "survivor")
        self.assertEqual(parent["machine_worth"], "yes")
        self.assertEqual(
            (parent["human_worth"], parent["human_worth_note"], parent["human_worth_at"]),
            ("no", "newer", "2026-08-05T02:00:00Z"),
        )
        tables = [
            row["name"]
            for row in query(
                self.db,
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
            )
            if any(
                column["name"] == "parent_id"
                for column in query(self.db, f"PRAGMA table_info({row['name']})")
            )
        ]
        self.assertEqual(
            {
                "parents", "people", "links", "candidate_people", "artifacts", "facts",
                "research", "guidance",
            },
            set(tables),
        )
        for table in tables:
            self.assertEqual(
                query(
                    self.db, f"SELECT count(*) FROM {table} WHERE parent_id='absorbed'",
                )[0][0],
                0,
                table,
            )
        self.assertEqual(
            {
                row["parent_slug"]
                for row in query(self.db, "SELECT parent_slug FROM people")
            },
            {"survivor-slug"},
        )
        decisions = {
            row["row_key"]: (row["decision_action"], row["decision_source"])
            for row in query(self.db, "SELECT * FROM links")
        }
        self.assertEqual(decisions["link-absorbed"][0], "verify")
        self.assertEqual(decisions["link-survivor"], ("detach", "sibling-settle"))

    def test_merge_is_atomic_and_rejects_invalid_parents(self) -> None:
        with patch.object(
            IdentityPolicy,
            "settle_human_families",
            side_effect=RuntimeError("stop after repoint"),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop after repoint"):
                self.db.merge_parents("survivor", "absorbed")
        people = query(
            self.db,
            "SELECT person_id, parent_id FROM people ORDER BY person_id",
        )
        self.assertEqual(
            [tuple(row) for row in people],
            [("person-absorbed", "absorbed"), ("person-survivor", "survivor")],
        )
        self.assertEqual(query(self.db, "SELECT count(*) FROM parents")[0][0], 2)
        with self.assertRaisesRegex(StoreError, "into itself"):
            self.db.merge_parents("survivor", "survivor")
        with self.assertRaisesRegex(StoreError, "unknown parent: missing"):
            self.db.merge_parents("survivor", "missing")
        self.db.merge_parents("survivor", "absorbed")
        machine = query(
            self.db,
            "SELECT row_key, machine_approved FROM links WHERE row_key LIKE 'machine-%'",
        )
        self.assertEqual({row["machine_approved"] for row in machine}, {None})

    def test_merge_accepts_an_already_owned_transaction_boundary(self) -> None:
        transaction = self.db.transaction

        @contextmanager
        def active_transaction():
            with transaction() as conn:
                conn.execute("BEGIN DEFERRED")
                yield conn

        with patch.object(self.db, "transaction", active_transaction):
            self.db.merge_parents("survivor", "absorbed")
        self.assertEqual(query(self.db, "SELECT count(*) FROM parents")[0][0], 1)

    def test_merge_checks_foreign_keys_before_commit(self) -> None:
        def leave_orphan(conn, _parent_ids) -> None:
            conn.execute(
                "INSERT INTO artifacts "
                "(artifact_key, kind, parent_id, path, content_fingerprint, status) "
                "VALUES ('orphan', 'facts', 'missing-parent', '/tmp/orphan', 'sha', 'projected')"
            )

        with patch.object(
            IdentityPolicy,
            "clear_machine_winner_conflicts",
            side_effect=leave_orphan,
        ):
            with self.assertRaisesRegex(StoreError, "parent merge violates foreign keys"):
                self.db.merge_parents("survivor", "absorbed")
        self.assertEqual(query(self.db, "SELECT count(*) FROM parents")[0][0], 2)
        self.assertEqual(query(self.db, "SELECT count(*) FROM artifacts WHERE artifact_key='orphan'")[0][0], 0)


if __name__ == "__main__":
    unittest.main()
