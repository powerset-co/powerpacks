from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packs.ingestion.primitives.deep_context.db.legacy import LegacyGraphMigration
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactRow,
    CandidatePersonRow,
    CanonicalGraphProjection,
    FactRow,
    GuidanceRow,
    GuidanceState,
    JobKind,
    JobRow,
    JobStatus,
    LinkRow,
    ParentRow,
    PersonIdentifierRow,
    PersonRow,
    PersonSourceRow,
    ProjectionStatus,
    ResearchRow,
    ResearchStatus,
    RowKind,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from deep_context_sqlite_test_helpers import (
    project_artifact,
    project_candidate,
    project_fact,
    project_parent,
    project_person,
    project_research,
    query,
    replace_candidate_people,
    replace_person_sources,
)


class CanonicalGraphTransactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Db(Path(self.temp.name) / "deep-context.sqlite")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_merge_rekeys_dependents_and_preserves_decisions(self) -> None:
        project_parent(self.db, ParentRow("parent-a", "parent-a"))
        project_parent(self.db, ParentRow("parent-b", "parent-b"))
        project_person(self.db, PersonRow("person-a", "parent-a", facts_json='{"old":1}'))
        project_person(self.db, PersonRow("person-a2", "parent-a"))
        project_person(self.db, PersonRow("person-b", "parent-b"))
        replace_person_sources(self.db, "person-a", (PersonSourceRow("person-a", "gmail"),))
        project_candidate(
            self.db,
            LinkRow(
                "link-a",
                "parent-a",
                "link-a",
                RowKind.PUB.value,
                machine_action="verify",
                machine_reason="machine stands",
            ),
        )
        project_candidate(
            self.db,
            LinkRow(
                "link-b",
                "parent-b",
                "link-b",
                RowKind.PUB.value,
            ),
        )
        replace_candidate_people(self.db, "link-a", (CandidatePersonRow("link-a", "person-a", "parent-a"),))
        replace_candidate_people(self.db, "link-b", (CandidatePersonRow("link-b", "person-b", "parent-b"),))
        self.db.decide_identity("link-a", "verify")
        self.db.decide_worth(
            "parent-a", "yes", note="older majority", decided_at="2026-01-01T00:00:00Z",
        )
        self.db.decide_worth(
            "parent-b", "no", note="newer minority", decided_at="2026-01-02T00:00:00Z",
        )
        project_artifact(
            self.db,
            ArtifactRow(
                "facts:person-a",
                ArtifactKind.FACTS.value,
                "parent-a",
                "/tmp/person-a.jsonl",
                "sha-a",
                ProjectionStatus.PROJECTED.value,
                person_id="person-a",
            ),
        )
        project_fact(
            self.db,
            FactRow(
                "person-a",
                "parent-a",
                "facts:person-a",
                person_id="person-a",
                machine_worth="yes",
            ),
        )
        project_artifact(
            self.db,
            ArtifactRow(
                "research:link-b",
                ArtifactKind.RESEARCH.value,
                "parent-b",
                "/tmp/link-b.json",
                "sha-b",
                ProjectionStatus.PROJECTED.value,
                candidate_key="link-b",
            ),
        )
        project_research(
            self.db,
            ResearchRow(
                "research-b",
                "parent-b",
                ResearchStatus.COMPLETE.value,
                candidate_key="link-b",
                artifact_key="research:link-b",
            ),
        )
        self.db.project_rows((
            GuidanceRow(
                "guidance-b",
                "parent-b",
                "find the right person",
                GuidanceState.PENDING.value,
                candidate_key="link-b",
            ),
        ))
        self.db.project_rows((
            JobRow(
                "job-b",
                JobKind.GUIDED_RETARGET.value,
                JobStatus.QUEUED.value,
                parent_id="parent-b",
                candidate_key="link-b",
            ),
        ))

        counts = LegacyGraphMigration.apply(
            self.db,
            CanonicalGraphProjection(
                parents=(ParentRow("parent-new", "parent-new"),),
                people=(
                    PersonRow("person-a", "parent-new"),
                    PersonRow("person-a2", "parent-new"),
                    PersonRow("person-b", "parent-new"),
                ),
                identifiers=(PersonIdentifierRow("person-a", "email", "a@example.com", "a@example.com"),),
                sources=(),
            )
        )

        self.assertEqual(counts.parents_removed, 2)
        self.assertEqual(query(self.db, "SELECT count(*) FROM parents")[0][0], 1)
        parent = query(self.db, "SELECT * FROM parents")[0]
        self.assertEqual(
            (
                parent["parent_id"], parent["human_worth"],
                parent["human_worth_note"], parent["human_worth_at"],
            ),
            ("parent-new", "no", "newer minority", "2026-01-02T00:00:00Z"),
        )
        self.assertEqual(
            {row[0] for row in query(self.db, "SELECT DISTINCT parent_id FROM people")},
            {"parent-new"},
        )
        for table in ("links", "candidate_people", "artifacts", "facts", "research", "guidance", "jobs"):
            self.assertEqual(
                {row[0] for row in query(self.db, f"SELECT DISTINCT parent_id FROM {table}")},
                {"parent-new"},
            )
        link = query(self.db, "SELECT * FROM links WHERE row_key='link-a'")[0]
        self.assertEqual((link["machine_reason"], link["decision_action"]), ("machine stands", "verify"))
        person = query(self.db, "SELECT * FROM people WHERE person_id='person-a'")[0]
        self.assertEqual(person["facts_json"], '{"old":1}')
        self.assertEqual(query(self.db, "SELECT source FROM person_sources")[0][0], "gmail")

    def test_plain_graph_replacement_does_not_resettle_every_link(self) -> None:
        project_parent(self.db, ParentRow("parent-a", "parent-a"))
        project_person(self.db, PersonRow("person-a", "parent-a"))
        project_candidate(
            self.db,
            LinkRow("winner", "parent-a", "winner", RowKind.PUB.value),
        )
        project_candidate(
            self.db,
            LinkRow("sibling", "parent-a", "sibling", RowKind.PUB.value),
        )
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE links SET decision_action='verify', decision_approved='yes', "
                "decision_source='deep-context-review', "
                "decided_at='2026-08-06T01:00:00Z' WHERE row_key='winner'"
            )

        LegacyGraphMigration.apply(
            self.db,
            CanonicalGraphProjection(
                (ParentRow("parent-a", "parent-a"),),
                (PersonRow("person-a", "parent-a"),),
                (),
                (),
            )
        )

        sibling = query(
            self.db,
            "SELECT decision_action FROM links WHERE row_key='sibling'",
        )[0]
        self.assertIsNone(sibling["decision_action"])

    def test_plain_graph_replacement_preserves_parent_machine_worth(self) -> None:
        project_parent(
            self.db,
            ParentRow(
                "parent-a",
                "parent-a",
                machine_worth="yes",
                machine_worth_reason="Known collaborator",
            ),
        )
        project_person(self.db, PersonRow("person-a", "parent-a"))

        LegacyGraphMigration.apply(
            self.db,
            CanonicalGraphProjection(
                (ParentRow("parent-a", "parent-a"),),
                (PersonRow("person-a", "parent-a"),),
                (),
                (),
            )
        )

        parent = query(
            self.db,
            "SELECT machine_worth, machine_worth_reason FROM parents "
            "WHERE parent_id='parent-a'",
        )[0]
        self.assertEqual(
            (parent["machine_worth"], parent["machine_worth_reason"]),
            ("yes", "Known collaborator"),
        )

    def test_cross_parent_candidate_rejected_before_mutation(self) -> None:
        project_parent(self.db, ParentRow("parent-old", "parent-old"))
        project_person(self.db, PersonRow("person-a", "parent-old"))
        project_person(self.db, PersonRow("person-b", "parent-old"))
        project_candidate(
            self.db,
            LinkRow(
                "shared",
                "parent-old",
                "shared",
                RowKind.PUB.value,
            ),
        )
        replace_candidate_people(
            self.db,
            "shared",
            (
                CandidatePersonRow("shared", "person-a", "parent-old"),
                CandidatePersonRow("shared", "person-b", "parent-old"),
            ),
        )
        projection = CanonicalGraphProjection(
            parents=(ParentRow("parent-a", "parent-a"), ParentRow("parent-b", "parent-b")),
            people=(PersonRow("person-a", "parent-a"), PersonRow("person-b", "parent-b")),
            identifiers=(),
            sources=(),
        )
        with self.assertRaisesRegex(StoreError, "crosses canonical parents"):
            LegacyGraphMigration.apply(self.db, projection)
        self.assertEqual(
            query(self.db, "SELECT parent_id FROM links WHERE row_key='shared'")[0][0],
            "parent-old",
        )
        self.assertEqual(query(self.db, "SELECT count(*) FROM parents")[0][0], 1)

    def test_parent_only_artifact_rejects_ambiguous_split(self) -> None:
        project_parent(self.db, ParentRow("parent-old", "parent-old"))
        project_person(self.db, PersonRow("person-a", "parent-old"))
        project_person(self.db, PersonRow("person-b", "parent-old"))
        project_artifact(
            self.db,
            ArtifactRow(
                "dossier:old",
                ArtifactKind.DOSSIER.value,
                "parent-old",
                "/tmp/dossier.md",
                "sha",
                ProjectionStatus.PROJECTED.value,
            ),
        )
        projection = CanonicalGraphProjection(
            parents=(ParentRow("parent-a", "parent-a"), ParentRow("parent-b", "parent-b")),
            people=(PersonRow("person-a", "parent-a"), PersonRow("person-b", "parent-b")),
            identifiers=(),
            sources=(),
        )
        with self.assertRaisesRegex(StoreError, "ambiguous canonical parent split"):
            LegacyGraphMigration.apply(self.db, projection)
        artifact = query(self.db, "SELECT parent_id FROM artifacts")[0]
        self.assertEqual(artifact["parent_id"], "parent-old")

    def test_planning_and_apply_share_one_connection(self) -> None:
        project_parent(self.db, ParentRow("parent-old", "parent-old"))
        project_person(self.db, PersonRow("person-a", "parent-old"))
        projection = CanonicalGraphProjection(
            parents=(ParentRow("parent-new", "parent-new"),),
            people=(PersonRow("person-a", "parent-new"),),
            identifiers=(),
            sources=(),
        )

        with patch.object(self.db, "transaction", wraps=self.db.transaction) as connect:
            LegacyGraphMigration.apply(self.db, projection)

        self.assertEqual(connect.call_count, 1)

    def test_apply_failure_rolls_back_complete_snapshot(self) -> None:
        project_parent(self.db, ParentRow("parent-old", "parent-old"))
        project_person(self.db, PersonRow("person-a", "parent-old"))
        projection = CanonicalGraphProjection(
            parents=(ParentRow("parent-new", "parent-new"),),
            people=(PersonRow("person-a", "parent-new"),),
            identifiers=(PersonIdentifierRow("person-a", "nickname", "casey"),),
            sources=(),
        )

        with self.assertRaises(sqlite3.IntegrityError):
            LegacyGraphMigration.apply(self.db, projection)

        self.assertEqual(
            [tuple(row) for row in query(self.db, "SELECT parent_id FROM parents")],
            [("parent-old",)],
        )
        self.assertEqual(
            [tuple(row) for row in query(self.db, "SELECT person_id, parent_id FROM people")],
            [("person-a", "parent-old")],
        )


if __name__ == "__main__":
    unittest.main()
