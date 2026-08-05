from __future__ import annotations

import sqlite3
import tempfile
import unittest
import csv
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.schema import (
    ArtifactKind,
    ArtifactRow,
    CandidatePersonRow,
    HumanWorth,
    IdentifierKind,
    JobKind,
    JobRow,
    JobStatus,
    LinkRow,
    MachineWorth,
    ParentRow,
    PersonIdentifierRow,
    PersonRow,
    ProjectionStatus,
    ReviewAction,
    SpendApprovalRow,
    StageStateRow,
    StageStatus,
    SyntheticProfileRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db, SchemaVersionError, StoreError


class DeepContextSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "deep-context.sqlite"
        self.db = Db(self.path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def parent(self, parent_id: str = "parent-1") -> None:
        self.db.project_parent(ParentRow(parent_id, f"pub-{parent_id}"))

    def person(self, person_id: str, parent_id: str = "parent-1") -> None:
        self.db.project_person(PersonRow(person_id, parent_id))

    def candidate(self, key: str, parent_id: str = "parent-1", *, kind: str = "pub") -> None:
        self.db.project_candidate(LinkRow(key, parent_id, key, kind))

    def test_existing_incompatible_layout_fails_before_mutation(self) -> None:
        self.parent()
        with sqlite3.connect(self.path) as conn:
            conn.execute("ALTER TABLE links ADD COLUMN rogue TEXT")
        with self.assertRaisesRegex(SchemaVersionError, "layout does not match"):
            Db(self.path)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM parents").fetchone()[0], 1)
            self.assertIn("rogue", {row[1] for row in conn.execute("PRAGMA table_info(links)")})

    def test_old_version_fails_without_running_v5_ddl(self) -> None:
        old = Path(self.temp.name) / "old.sqlite"
        with sqlite3.connect(old) as conn:
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT INTO meta VALUES ('schema_version', '4')")
            conn.execute("CREATE TABLE legacy_only (value TEXT)")
            conn.execute("INSERT INTO legacy_only VALUES ('kept')")
        with self.assertRaisesRegex(SchemaVersionError, "expected 5"):
            Db(old)
        with sqlite3.connect(old) as conn:
            self.assertEqual(conn.execute("SELECT value FROM legacy_only").fetchone()[0], "kept")
            self.assertNotIn("parents", {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            })

    def test_owner_relations_are_foreign_keyed_and_parent_consistent(self) -> None:
        self.parent("parent-1")
        self.parent("parent-2")
        self.person("person-1", "parent-1")
        self.candidate("candidate-1", "parent-1")
        self.db.replace_candidate_people(
            "candidate-1", (CandidatePersonRow("candidate-1", "person-1", "parent-1"),)
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.replace_candidate_people(
                "candidate-1", (CandidatePersonRow("candidate-1", "person-1", "parent-2"),)
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.project_candidate(LinkRow("orphan", "missing", "orphan", "pub"))

    def test_identifiers_are_normalized_rows_not_candidate_json(self) -> None:
        self.parent()
        self.person("person-1")
        rows = (
            PersonIdentifierRow("person-1", IdentifierKind.EMAIL.value,
                                "casey@example.com", "Casey@example.com"),
            PersonIdentifierRow("person-1", IdentifierKind.PHONE.value, "+15550100"),
        )
        self.db.replace_person_identifiers("person-1", rows)
        found = self.db.query(
            "SELECT kind, normalized_value FROM person_identifiers ORDER BY kind"
        )
        self.assertEqual([tuple(row) for row in found], [
            ("email", "casey@example.com"), ("phone", "+15550100")
        ])
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.replace_person_identifiers(
                "person-1", (PersonIdentifierRow("person-1", "nickname", "casey"),)
            )

    def test_machine_projection_cannot_overwrite_human_worth_or_identity(self) -> None:
        self.db.project_parent(ParentRow(
            "parent-1", "jordan", machine_worth=MachineWorth.MAYBE.value
        ))
        self.candidate("candidate-1")
        self.db.set_worth("parent-1", HumanWorth.YES.value, note="known collaborator")
        self.db.settle_identity("candidate-1", ReviewAction.VERIFY.value)

        self.db.project_parent(ParentRow(
            "parent-1", "jordan-new", machine_worth=MachineWorth.NO.value
        ))
        self.db.project_candidate(LinkRow(
            "candidate-1", "parent-1", "candidate-new", "pub",
            machine_action=ReviewAction.DETACH.value, machine_approved="auto",
        ))
        parent = self.db.query("SELECT * FROM parents")[0]
        candidate = self.db.query("SELECT * FROM links")[0]
        self.assertEqual((parent["machine_worth"], parent["human_worth"]), ("no", "yes"))
        self.assertEqual(
            (candidate["machine_action"], candidate["decision_action"]), ("detach", "verify")
        )
        with self.assertRaisesRegex(StoreError, "already has a human decision"):
            self.db.settle_identity("candidate-1", ReviewAction.DETACH.value)

    def test_machine_retarget_proposal_is_separate_from_human_replacement(self) -> None:
        self.parent()
        self.db.project_candidate(LinkRow(
            "candidate-1", "parent-1", "candidate-1", "pub",
            machine_action=ReviewAction.RETARGET.value,
            machine_proposed_url="https://www.linkedin.com/in/proposed",
            machine_proposed_public_identifier="proposed",
        ))
        self.db.settle_identity(
            "candidate-1", ReviewAction.RETARGET.value,
            replacement_url="https://www.linkedin.com/in/chosen",
            replacement_public_identifier="chosen",
        )
        self.db.project_candidate(LinkRow(
            "candidate-1", "parent-1", "candidate-1", "pub",
            machine_action=ReviewAction.RETARGET.value,
            machine_proposed_url="https://www.linkedin.com/in/new-proposal",
            machine_proposed_public_identifier="new-proposal",
        ))
        row = self.db.query("SELECT * FROM links WHERE row_key='candidate-1'")[0]
        self.assertEqual(row["machine_proposed_public_identifier"], "new-proposal")
        self.assertEqual(row["replacement_public_identifier"], "chosen")

    def test_synthetic_profile_has_one_candidate_owned_gate(self) -> None:
        self.parent()
        self.candidate("synthetic-1", kind="synthetic")
        self.db.project_synthetic_profile(SyntheticProfileRow(
            "synthetic-1", "synthetic-1", '{"full_name":"Jordan Bravo"}'
        ))
        self.db.settle_identity("synthetic-1", ReviewAction.DETACH.value)
        columns = {row["name"] for row in self.db.query("PRAGMA table_info(synthetic_profiles)")}
        self.assertFalse({"approved", "human_gate", "decision"} & columns)
        self.assertEqual(
            self.db.query("SELECT decision_action FROM links WHERE row_key='synthetic-1'")[0][0],
            "detach",
        )
        self.candidate("real-1")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic kind"):
            self.db.project_synthetic_profile(SyntheticProfileRow("bad", "real-1", "{}"))

        exported = Path(self.temp.name) / "synthetic.csv"
        self.db.export_batons(Path(self.temp.name) / "review.csv", exported)
        with exported.open(newline="", encoding="utf-8") as handle:
            self.assertEqual(next(csv.DictReader(handle))["approved"], "no")

    def test_artifact_projection_is_idempotent_and_owner_checked(self) -> None:
        self.parent()
        self.person("person-1")
        first = ArtifactRow(
            "facts:person-1", ArtifactKind.FACTS.value, "parent-1",
            "/artifacts/person-1.jsonl", "sha256:first", ProjectionStatus.PROJECTED.value,
            person_id="person-1",
        )
        self.assertTrue(self.db.project_artifact(first))
        self.assertFalse(self.db.project_artifact(first))
        second = ArtifactRow(
            "facts:person-1", ArtifactKind.FACTS.value, "parent-1",
            "/artifacts/person-1.jsonl", "sha256:second", ProjectionStatus.PROJECTED.value,
            person_id="person-1",
        )
        self.assertTrue(self.db.project_artifact(second))
        self.assertEqual(
            self.db.query("SELECT content_fingerprint FROM artifacts")[0][0], "sha256:second"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.project_artifact(ArtifactRow(
                "bad", ArtifactKind.FACTS.value, "parent-1", "/bad", "sha256:bad",
                ProjectionStatus.PROJECTED.value, person_id="missing",
            ))

        self.assertEqual(ArtifactKind.SYNTHETIC.value, "synthetic")

    def test_stage_spend_and_job_state_are_typed(self) -> None:
        self.db.save_stage(StageStateRow(
            "enrichment", StageStatus.NEEDS_APPROVAL.value, "selection:v1"
        ))
        self.db.approve_spend(SpendApprovalRow("enrichment", "selection:v1", 3, 1.5))
        self.db.save_job(JobRow(
            "guided-retarget", JobKind.GUIDED_RETARGET.value, JobStatus.QUEUED.value,
            completed_count=0, total_count=1,
        ))
        self.assertEqual(self.db.query("SELECT approved_count FROM spend_approvals")[0][0], 3)
        with self.assertRaisesRegex(StoreError, "current selection"):
            self.db.approve_spend(SpendApprovalRow("enrichment", "selection:v2", 3))
        self.db.save_stage(StageStateRow(
            "enrichment", StageStatus.NEEDS_APPROVAL.value, "selection:v2"
        ))
        self.assertEqual(self.db.query("SELECT COUNT(*) FROM spend_approvals")[0][0], 0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.save_stage(StageStateRow("bad", "whatever"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.save_job(JobRow(
                "bad", JobKind.ENRICHMENT.value, JobStatus.RUNNING.value,
                completed_count=2, total_count=1,
            ))

    def test_job_and_stage_can_join_one_projector_transaction(self) -> None:
        with self.db.connect() as conn:
            self.db.save_stage(StageStateRow(
                "enrichment", StageStatus.RUNNING.value, "selection:v1"
            ), conn=conn)
            self.db.save_job(JobRow(
                "enrichment", JobKind.ENRICHMENT.value, JobStatus.RUNNING.value,
                selection_fingerprint="selection:v1", total_count=2,
            ), conn=conn)
        self.assertEqual(self.db.query("SELECT status FROM stage_state")[0][0], "running")
        self.assertEqual(self.db.query("SELECT total_count FROM jobs")[0][0], 2)

    def test_settlement_derives_all_parent_siblings(self) -> None:
        self.parent()
        self.person("person-1")
        self.person("person-2")
        self.candidate("first")
        self.candidate("second")
        self.candidate("synthetic", kind="synthetic")
        self.db.replace_candidate_people(
            "first", (CandidatePersonRow("first", "person-1", "parent-1"),)
        )
        self.db.replace_candidate_people(
            "second", (CandidatePersonRow("second", "person-2", "parent-1"),)
        )
        settled = self.db.settle_identity("first", ReviewAction.VERIFY.value)
        self.assertEqual(set(settled), {"first", "second", "synthetic"})
        decisions = self.db.query(
            "SELECT row_key, decision_action FROM links ORDER BY row_key"
        )
        self.assertEqual([tuple(row) for row in decisions], [
            ("first", "verify"), ("second", "detach"), ("synthetic", "detach")
        ])

    def test_store_has_no_generic_decision_or_update_escape_hatch(self) -> None:
        tables = {row[0] for row in self.db.query(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertNotIn("decisions", tables)
        self.assertNotIn("verdicts", tables)
        self.assertFalse(hasattr(self.db, "upsert_decision"))
        self.assertFalse(hasattr(self.db, "update_link"))


if __name__ == "__main__":
    unittest.main()
