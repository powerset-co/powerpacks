from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


class CanonicalGraphTransactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Db(Path(self.temp.name) / "deep-context.sqlite")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_merge_rekeys_dependents_and_preserves_decisions(self) -> None:
        self.db.project_parent(ParentRow("parent-a", "parent-a"))
        self.db.project_parent(ParentRow("parent-b", "parent-b"))
        self.db.project_person(PersonRow("person-a", "parent-a", facts_json='{"old":1}'))
        self.db.project_person(PersonRow("person-b", "parent-b"))
        self.db.replace_person_sources(
            "person-a", (PersonSourceRow("person-a", "gmail"),)
        )
        self.db.project_candidate(LinkRow(
            "link-a", "parent-a", "link-a", RowKind.PUB.value,
            machine_action="verify", machine_reason="machine stands",
        ))
        self.db.project_candidate(LinkRow(
            "link-b", "parent-b", "link-b", RowKind.PUB.value,
        ))
        self.db.replace_candidate_people(
            "link-a", (CandidatePersonRow("link-a", "person-a", "parent-a"),)
        )
        self.db.replace_candidate_people(
            "link-b", (CandidatePersonRow("link-b", "person-b", "parent-b"),)
        )
        self.db.settle_identity("link-a", "verify")
        self.db.set_worth("parent-a", "yes", note="known useful")
        self.db.set_worth("parent-b", "no", note="less overlap tie loses by id")
        self.db.project_artifact(ArtifactRow(
            "facts:person-a", ArtifactKind.FACTS.value, "parent-a",
            "/tmp/person-a.jsonl", "sha-a", ProjectionStatus.PROJECTED.value,
            person_id="person-a",
        ))
        self.db.project_fact(FactRow(
            "person-a", "parent-a", "facts:person-a", person_id="person-a",
            machine_worth="yes",
        ))
        self.db.project_artifact(ArtifactRow(
            "research:link-b", ArtifactKind.RESEARCH.value, "parent-b",
            "/tmp/link-b.json", "sha-b", ProjectionStatus.PROJECTED.value,
            candidate_key="link-b",
        ))
        self.db.project_research(ResearchRow(
            "research-b", "parent-b", ResearchStatus.COMPLETE.value,
            candidate_key="link-b", artifact_key="research:link-b",
        ))
        self.db.save_guidance(GuidanceRow(
            "guidance-b", "parent-b", "find the right person",
            GuidanceState.PENDING.value, candidate_key="link-b",
        ))
        self.db.save_job(JobRow(
            "job-b", JobKind.GUIDED_RETARGET.value, JobStatus.QUEUED.value,
            parent_id="parent-b", candidate_key="link-b",
        ))

        counts = self.db.replace_canonical_graph(CanonicalGraphProjection(
            parents=(ParentRow("parent-new", "parent-new"),),
            people=(
                PersonRow("person-a", "parent-new"),
                PersonRow("person-b", "parent-new"),
            ),
            identifiers=(PersonIdentifierRow(
                "person-a", "email", "a@example.com", "a@example.com"
            ),),
            sources=(),
        ))

        self.assertEqual(counts.parents_removed, 2)
        self.assertEqual(self.db.query("SELECT count(*) FROM parents")[0][0], 1)
        parent = self.db.query("SELECT * FROM parents")[0]
        self.assertEqual((parent["parent_id"], parent["human_worth"]),
                         ("parent-new", "yes"))
        self.assertEqual(
            {row[0] for row in self.db.query("SELECT DISTINCT parent_id FROM people")},
            {"parent-new"},
        )
        for table in ("links", "candidate_people", "artifacts", "facts",
                      "research", "guidance", "jobs"):
            self.assertEqual(
                {row[0] for row in self.db.query(f"SELECT DISTINCT parent_id FROM {table}")},
                {"parent-new"},
            )
        link = self.db.query("SELECT * FROM links WHERE row_key='link-a'")[0]
        self.assertEqual((link["machine_reason"], link["decision_action"]),
                         ("machine stands", "verify"))
        person = self.db.query("SELECT * FROM people WHERE person_id='person-a'")[0]
        self.assertEqual(person["facts_json"], '{"old":1}')
        self.assertEqual(self.db.query("SELECT source FROM person_sources")[0][0], "gmail")

    def test_cross_parent_candidate_rejected_before_mutation(self) -> None:
        self.db.project_parent(ParentRow("parent-old", "parent-old"))
        self.db.project_person(PersonRow("person-a", "parent-old"))
        self.db.project_person(PersonRow("person-b", "parent-old"))
        self.db.project_candidate(LinkRow(
            "shared", "parent-old", "shared", RowKind.PUB.value,
        ))
        self.db.replace_candidate_people("shared", (
            CandidatePersonRow("shared", "person-a", "parent-old"),
            CandidatePersonRow("shared", "person-b", "parent-old"),
        ))
        projection = CanonicalGraphProjection(
            parents=(ParentRow("parent-a", "parent-a"), ParentRow("parent-b", "parent-b")),
            people=(PersonRow("person-a", "parent-a"), PersonRow("person-b", "parent-b")),
            identifiers=(), sources=(),
        )
        with self.assertRaisesRegex(StoreError, "crosses canonical parents"):
            self.db.replace_canonical_graph(projection)
        self.assertEqual(
            self.db.query("SELECT parent_id FROM links WHERE row_key='shared'")[0][0],
            "parent-old",
        )
        self.assertEqual(self.db.query("SELECT count(*) FROM parents")[0][0], 1)

    def test_parent_only_artifact_rejects_ambiguous_split(self) -> None:
        self.db.project_parent(ParentRow("parent-old", "parent-old"))
        self.db.project_person(PersonRow("person-a", "parent-old"))
        self.db.project_person(PersonRow("person-b", "parent-old"))
        self.db.project_artifact(ArtifactRow(
            "dossier:old", ArtifactKind.DOSSIER.value, "parent-old",
            "/tmp/dossier.md", "sha", ProjectionStatus.PROJECTED.value,
        ))
        projection = CanonicalGraphProjection(
            parents=(ParentRow("parent-a", "parent-a"), ParentRow("parent-b", "parent-b")),
            people=(PersonRow("person-a", "parent-a"), PersonRow("person-b", "parent-b")),
            identifiers=(), sources=(),
        )
        with self.assertRaisesRegex(StoreError, "ambiguous canonical parent split"):
            self.db.replace_canonical_graph(projection)
        artifact = self.db.query("SELECT parent_id FROM artifacts")[0]
        self.assertEqual(artifact["parent_id"], "parent-old")

    def test_planning_and_apply_share_one_connection(self) -> None:
        self.db.project_parent(ParentRow("parent-old", "parent-old"))
        self.db.project_person(PersonRow("person-a", "parent-old"))
        projection = CanonicalGraphProjection(
            parents=(ParentRow("parent-new", "parent-new"),),
            people=(PersonRow("person-a", "parent-new"),),
            identifiers=(),
            sources=(),
        )

        with patch.object(self.db, "connect", wraps=self.db.connect) as connect:
            self.db.replace_canonical_graph(projection)

        self.assertEqual(connect.call_count, 1)

    def test_apply_failure_rolls_back_complete_snapshot(self) -> None:
        self.db.project_parent(ParentRow("parent-old", "parent-old"))
        self.db.project_person(PersonRow("person-a", "parent-old"))
        projection = CanonicalGraphProjection(
            parents=(ParentRow("parent-new", "parent-new"),),
            people=(PersonRow("person-a", "parent-new"),),
            identifiers=(PersonIdentifierRow("person-a", "nickname", "casey"),),
            sources=(),
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.db.replace_canonical_graph(projection)

        self.assertEqual(
            [tuple(row) for row in self.db.query("SELECT parent_id FROM parents")],
            [("parent-old",)],
        )
        self.assertEqual(
            [tuple(row) for row in self.db.query("SELECT person_id, parent_id FROM people")],
            [("person-a", "parent-old")],
        )


if __name__ == "__main__":
    unittest.main()
