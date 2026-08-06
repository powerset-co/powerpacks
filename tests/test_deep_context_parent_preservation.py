"""Canonical rebuild keeps SQLite-owned people without dossier projections."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.models import (
    CandidatePeopleProjection,
    CandidatePersonRow,
    LinkRow,
    ParentRow,
    PersonRow,
    RowKind,
)
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.parents.projection import CanonicalGraphBuilder
from deep_context_sqlite_test_helpers import query


class ParentPreservationTest(unittest.TestCase):
    def test_projection_preserves_candidate_people_without_dossiers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Db(Path(directory) / "deep-context.sqlite")
            db.project_rows((
                ParentRow("old-person", "parent-worth:old-person", "Jordan Bravo", "jordan-a"),
                ParentRow(
                    "candidate-parent", "parent-worth:candidate-parent",
                    "Candidate", "candidate",
                ),
                PersonRow("person-a", "old-person", "jordan-a", "jordan-a", "Jordan Bravo"),
                PersonRow(
                    "candidate:email:candidate@example.com", "candidate-parent",
                    display_name="Candidate",
                ),
                LinkRow(
                    "candidate@example.com", "candidate-parent", "candidate@example.com",
                    RowKind.CANDIDATE_EMAIL.value, candidate_origin=1,
                ),
                CandidatePeopleProjection("candidate@example.com", (
                    CandidatePersonRow(
                        "candidate@example.com", "candidate:email:candidate@example.com",
                        "candidate-parent",
                    ),
                )),
            ))
            snapshot = canonical_snapshot(db)
            builder = CanonicalGraphBuilder(db, snapshot, {
                "jordan-a": {
                    "person_id": "person-a", "name": "Jordan Bravo",
                    "emails": ["jordan@example.com"], "phones": [],
                    "source_channels": [],
                },
            })
            builder.add_parent("rebuilt-parent", "Jordan Bravo", "jordan-bravo")
            builder.add_member("jordan-a", "rebuilt-parent", "jordan-bravo")

            builder.apply()

            candidate = query(
                db,
                "SELECT parent_id, is_ghost FROM people WHERE person_id=?",
                ("candidate:email:candidate@example.com",),
            )[0]
            self.assertEqual(
                (candidate["parent_id"], candidate["is_ghost"]),
                ("candidate-parent", 0),
            )
            membership = query(
                db,
                "SELECT parent_id FROM candidate_people WHERE row_key='candidate@example.com'",
            )[0]
            self.assertEqual(membership["parent_id"], "candidate-parent")
            self.assertEqual(
                {row["parent_id"] for row in query(db, "SELECT parent_id FROM parents")},
                {"rebuilt-parent", "candidate-parent"},
            )


if __name__ == "__main__":
    unittest.main()
