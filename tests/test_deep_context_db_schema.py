from __future__ import annotations

import sqlite3
import tempfile
import unittest
import csv
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.models import (
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
    PersonSourceRow,
    ProjectionStatus,
    ReviewAction,
    SyntheticProfileRow,
)
from packs.ingestion.primitives.deep_context.db import snapshots
from packs.ingestion.primitives.deep_context.db.schema import SCHEMA_VERSION
from packs.ingestion.primitives.deep_context.db.store import Db, SchemaVersionError, StoreError
from deep_context_sqlite_test_helpers import (
    project_artifact,
    project_candidate,
    project_parent,
    project_person,
    project_synthetic_profile,
    query,
    replace_candidate_people,
    replace_person_identifiers,
    replace_person_sources,
)


class DeepContextSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "deep-context.sqlite"
        self.db = Db(self.path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def parent(self, parent_id: str = "parent-1") -> None:
        project_parent(self.db, ParentRow(parent_id, f"pub-{parent_id}"))

    def person(self, person_id: str, parent_id: str = "parent-1") -> None:
        project_person(self.db, PersonRow(person_id, parent_id))

    def candidate(self, key: str, parent_id: str = "parent-1", *, kind: str = "pub") -> None:
        project_candidate(self.db, LinkRow(key, parent_id, key, kind))

    def test_existing_incompatible_layout_fails_before_mutation(self) -> None:
        self.parent()
        with sqlite3.connect(self.path) as conn:
            conn.execute("ALTER TABLE links ADD COLUMN rogue TEXT")
        with self.assertRaisesRegex(SchemaVersionError, rf"layout does not match schema version {SCHEMA_VERSION}"):
            Db(self.path)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM parents").fetchone()[0], 1)
            self.assertIn("rogue", {row[1] for row in conn.execute("PRAGMA table_info(links)")})

    def test_old_version_fails_without_running_current_ddl(self) -> None:
        old = Path(self.temp.name) / "old.sqlite"
        with sqlite3.connect(old) as conn:
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT INTO meta VALUES ('schema_version', '4')")
            conn.execute("CREATE TABLE legacy_only (value TEXT)")
            conn.execute("INSERT INTO legacy_only VALUES ('kept')")
        with self.assertRaisesRegex(SchemaVersionError, f"expected {SCHEMA_VERSION}"):
            Db(old)
        with sqlite3.connect(old) as conn:
            self.assertEqual(conn.execute("SELECT value FROM legacy_only").fetchone()[0], "kept")
            self.assertNotIn(
                "parents", {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            )

    def test_owner_relations_are_foreign_keyed_and_parent_consistent(self) -> None:
        self.parent("parent-1")
        self.parent("parent-2")
        self.person("person-1", "parent-1")
        self.candidate("candidate-1", "parent-1")
        replace_candidate_people(self.db, "candidate-1", (CandidatePersonRow("candidate-1", "person-1", "parent-1"),))
        with self.assertRaises(sqlite3.IntegrityError):
            replace_candidate_people(
                self.db, "candidate-1", (CandidatePersonRow("candidate-1", "person-1", "parent-2"),)
            )
        with self.assertRaises(sqlite3.IntegrityError):
            project_candidate(self.db, LinkRow("orphan", "missing", "orphan", "pub"))

    def test_identifiers_are_normalized_rows_not_candidate_json(self) -> None:
        self.parent()
        self.person("person-1")
        rows = (
            PersonIdentifierRow("person-1", IdentifierKind.EMAIL.value, "casey@example.com", "Casey@example.com"),
            PersonIdentifierRow("person-1", IdentifierKind.PHONE.value, "+15550100"),
        )
        replace_person_identifiers(self.db, "person-1", rows)
        found = query(self.db, "SELECT kind, normalized_value FROM person_identifiers ORDER BY kind")
        self.assertEqual([tuple(row) for row in found], [("email", "casey@example.com"), ("phone", "+15550100")])
        with self.assertRaises(sqlite3.IntegrityError):
            replace_person_identifiers(self.db, "person-1", (PersonIdentifierRow("person-1", "nickname", "casey"),))

    def test_sources_are_normalized_and_foreign_keyed(self) -> None:
        self.parent()
        self.person("person-1")
        replace_person_sources(
            self.db,
            "person-1",
            (
                PersonSourceRow("person-1", "gmail_msgvault"),
                PersonSourceRow("person-1", "linkedin_csv"),
            ),
        )
        self.assertEqual(
            [row["source"] for row in query(self.db, "SELECT source FROM person_sources ORDER BY source")],
            ["gmail_msgvault", "linkedin_csv"],
        )
        with self.assertRaises(sqlite3.IntegrityError):
            replace_person_sources(self.db, "missing", (PersonSourceRow("missing", "gmail_msgvault"),))

    def test_machine_projection_preserves_human_and_latest_click_wins(self) -> None:
        project_parent(self.db, ParentRow("parent-1", "jordan", machine_worth=MachineWorth.MAYBE.value))
        self.candidate("candidate-1")
        self.db.decide_worth("parent-1", HumanWorth.YES.value, note="known collaborator")
        self.db.decide_identity("candidate-1", ReviewAction.VERIFY.value)

        project_parent(self.db, ParentRow("parent-1", "jordan-new", machine_worth=MachineWorth.NO.value))
        project_candidate(
            self.db,
            LinkRow(
                "candidate-1",
                "parent-1",
                "candidate-new",
                "pub",
                machine_action=ReviewAction.DETACH.value,
                machine_approved="auto",
            ),
        )
        parent = query(self.db, "SELECT * FROM parents")[0]
        candidate = query(self.db, "SELECT * FROM links")[0]
        self.assertEqual((parent["machine_worth"], parent["human_worth"]), ("no", "yes"))
        self.assertEqual((candidate["machine_action"], candidate["decision_action"]), ("detach", "verify"))
        self.db.decide_identity("candidate-1", ReviewAction.DETACH.value)
        candidate = query(self.db, "SELECT * FROM links")[0]
        self.assertEqual(
            (candidate["decision_action"], candidate["decision_approved"]),
            ("detach", "yes"),
        )

    def test_worth_note_preserves_clears_and_resets_explicitly(self) -> None:
        self.parent()
        self.db.decide_worth("parent-1", "yes", note="known collaborator")

        self.db.decide_worth("parent-1", "no")
        row = query(self.db, "SELECT * FROM parents WHERE parent_id='parent-1'")[0]
        self.assertEqual((row["human_worth"], row["human_worth_note"]), ("no", "known collaborator"))

        self.db.decide_worth("parent-1", "yes", note="")
        row = query(self.db, "SELECT * FROM parents WHERE parent_id='parent-1'")[0]
        self.assertEqual((row["human_worth"], row["human_worth_note"]), ("yes", ""))

        self.db.decide_worth("parent-1", None)
        row = query(self.db, "SELECT * FROM parents WHERE parent_id='parent-1'")[0]
        self.assertEqual(
            (row["human_worth"], row["human_worth_note"], row["human_worth_source"]),
            (None, None, None),
        )

    def test_sibling_settlement_preserves_direct_human_exclude_and_note(self) -> None:
        self.parent()
        self.candidate("excluded")
        self.candidate("verified")
        self.db.decide_identity(
            "excluded",
            ReviewAction.EXCLUDE.value,
            note="not a usable identity",
        )

        settled = self.db.decide_identity("verified", ReviewAction.VERIFY.value)

        excluded = query(self.db, "SELECT * FROM links WHERE row_key='excluded'")[0]
        self.assertEqual(settled, ["verified"])
        self.assertEqual(
            (
                excluded["decision_action"],
                excluded["decision_source"],
                excluded["decision_note"],
            ),
            (
                ReviewAction.EXCLUDE.value,
                "deep-context-review",
                "not a usable identity",
            ),
        )

    def test_identity_click_order_settles_only_pending_siblings(self) -> None:
        cases = (
            (
                "exclude-only",
                (("aaa", "exclude", "exclude note"),),
                (("aaa", "exclude", "deep-context-review", "exclude note"),
                 ("mmm", None, None, None), ("zzz", None, None, None)),
            ),
            (
                "verify-then-exclude",
                (("zzz", "verify", None), ("aaa", "exclude", "exclude note")),
                (("aaa", "exclude", "deep-context-review", "exclude note"),
                 ("mmm", "detach", "legacy-sibling-settle", None),
                 ("zzz", "verify", "deep-context-review", None)),
            ),
            (
                "verify-then-detach",
                (("zzz", "verify", None), ("aaa", "detach", "skip note")),
                (("aaa", "detach", "deep-context-review", "skip note"),
                 ("mmm", "detach", "legacy-sibling-settle", None),
                 ("zzz", "verify", "deep-context-review", None)),
            ),
            (
                "detach-only",
                (("aaa", "detach", "skip note"),),
                (("aaa", "detach", "deep-context-review", "skip note"),
                 ("mmm", "detach", "legacy-sibling-settle", None),
                 ("zzz", "detach", "legacy-sibling-settle", None)),
            ),
        )
        for name, clicks, expected in cases:
            with self.subTest(name=name):
                path = Path(self.temp.name) / f"{name}.sqlite"
                db = Db(path)
                db.project_rows((
                    ParentRow("family", "family"),
                    *(LinkRow(key, "family", key, "pub") for key in ("aaa", "mmm", "zzz")),
                ))
                for key, action, note in clicks:
                    db.decide_identity(key, action, note=note)
                actual = [
                    tuple(row)
                    for row in query(
                        db,
                        "SELECT row_key, decision_action, decision_source, decision_note "
                        "FROM links ORDER BY row_key",
                    )
                ]
                self.assertEqual(actual, list(expected))

    def test_human_decision_door_rejects_machine_only_review_action(self) -> None:
        self.parent()
        self.candidate("candidate-1")

        with self.assertRaisesRegex(StoreError, "invalid identity action: review"):
            self.db.decide_identity("candidate-1", ReviewAction.REVIEW.value)

    def test_machine_retarget_proposal_is_separate_from_human_replacement(self) -> None:
        self.parent()
        project_candidate(
            self.db,
            LinkRow(
                "candidate-1",
                "parent-1",
                "candidate-1",
                "pub",
                machine_action=ReviewAction.RETARGET.value,
                machine_proposed_url="https://www.linkedin.com/in/proposed",
                machine_proposed_public_identifier="proposed",
            ),
        )
        self.db.decide_identity(
            "candidate-1",
            ReviewAction.RETARGET.value,
            replacement_url="https://www.linkedin.com/in/chosen",
            replacement_public_identifier="chosen",
        )
        project_candidate(
            self.db,
            LinkRow(
                "candidate-1",
                "parent-1",
                "candidate-1",
                "pub",
                machine_action=ReviewAction.RETARGET.value,
                machine_proposed_url="https://www.linkedin.com/in/new-proposal",
                machine_proposed_public_identifier="new-proposal",
            ),
        )
        row = query(self.db, "SELECT * FROM links WHERE row_key='candidate-1'")[0]
        self.assertEqual(row["machine_proposed_public_identifier"], "new-proposal")
        self.assertEqual(row["replacement_public_identifier"], "chosen")

    def test_machine_verdict_and_research_reject_are_distinct(self) -> None:
        self.parent()
        project_candidate(
            self.db,
            LinkRow(
                "candidate-1",
                "parent-1",
                "candidate-1",
                "candidate_email",
                machine_action=ReviewAction.RETARGET.value,
                machine_judgment="needs_review",
                machine_reject="no",
                machine_reject_confidence=0.74,
                machine_reject_reason="no contradiction",
                machine_proposed_url="https://www.linkedin.com/in/proposed",
            ),
        )
        row = query(self.db, "SELECT * FROM links WHERE row_key='candidate-1'")[0]
        self.assertEqual(row["machine_judgment"], "needs_review")
        self.assertEqual(row["machine_reject"], "no")

    def test_synthetic_profile_has_one_candidate_owned_gate(self) -> None:
        self.parent()
        self.candidate("synthetic-1", kind="synthetic")
        project_synthetic_profile(
            self.db, SyntheticProfileRow("synthetic-1", "synthetic-1", '{"full_name":"Jordan Bravo"}')
        )
        self.db.decide_identity("synthetic-1", ReviewAction.DETACH.value)
        columns = {row["name"] for row in query(self.db, "PRAGMA table_info(synthetic_profiles)")}
        self.assertFalse({"approved", "human_gate", "decision"} & columns)
        self.assertEqual(
            query(self.db, "SELECT decision_action FROM links WHERE row_key='synthetic-1'")[0][0],
            "detach",
        )
        self.candidate("real-1")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic kind"):
            project_synthetic_profile(self.db, SyntheticProfileRow("bad", "real-1", "{}"))

        exported = Path(self.temp.name) / "synthetic.csv"
        snapshots.export_batons(self.db, Path(self.temp.name) / "review.csv", exported)
        with exported.open(newline="", encoding="utf-8") as handle:
            self.assertEqual(next(csv.DictReader(handle))["approved"], "no")

    def test_artifact_projection_is_idempotent_and_owner_checked(self) -> None:
        self.parent()
        self.person("person-1")
        first = ArtifactRow(
            "facts:person-1",
            ArtifactKind.FACTS.value,
            "parent-1",
            "/artifacts/person-1.jsonl",
            "sha256:first",
            ProjectionStatus.PROJECTED.value,
            person_id="person-1",
        )
        self.assertTrue(project_artifact(self.db, first))
        self.assertFalse(project_artifact(self.db, first))
        second = ArtifactRow(
            "facts:person-1",
            ArtifactKind.FACTS.value,
            "parent-1",
            "/artifacts/person-1.jsonl",
            "sha256:second",
            ProjectionStatus.PROJECTED.value,
            person_id="person-1",
        )
        self.assertTrue(project_artifact(self.db, second))
        self.assertEqual(query(self.db, "SELECT content_fingerprint FROM artifacts")[0][0], "sha256:second")
        with self.assertRaises(sqlite3.IntegrityError):
            project_artifact(
                self.db,
                ArtifactRow(
                    "bad",
                    ArtifactKind.FACTS.value,
                    "parent-1",
                    "/bad",
                    "sha256:bad",
                    ProjectionStatus.PROJECTED.value,
                    person_id="missing",
                ),
            )

        self.assertEqual(ArtifactKind.SYNTHETIC.value, "synthetic")

    def test_job_state_is_typed_without_stage_or_approval_tables(self) -> None:
        self.db.project_rows((
            JobRow(
                "guided-retarget",
                JobKind.GUIDED_RETARGET.value,
                JobStatus.QUEUED.value,
                completed_count=0,
                total_count=1,
            ),
        ))
        tables = {row[0] for row in query(
            self.db, "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertFalse({"stage_state", "spend_approvals"} & tables)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.project_rows((
                JobRow(
                    "bad",
                    JobKind.ENRICHMENT.value,
                    JobStatus.RUNNING.value,
                    completed_count=2,
                    total_count=1,
                ),
            ))

    def test_job_uses_the_closed_projection_api(self) -> None:
        self.db.project_rows((
            JobRow(
                "enrichment",
                JobKind.ENRICHMENT.value,
                JobStatus.RUNNING.value,
                selection_fingerprint="selection:v1",
                total_count=2,
            ),
        ))
        self.assertEqual(query(self.db, "SELECT total_count FROM jobs")[0][0], 2)

    def test_settlement_derives_all_parent_siblings(self) -> None:
        self.parent()
        self.person("person-1")
        self.person("person-2")
        self.candidate("first")
        self.candidate("second")
        self.candidate("synthetic", kind="synthetic")
        replace_candidate_people(self.db, "first", (CandidatePersonRow("first", "person-1", "parent-1"),))
        replace_candidate_people(self.db, "second", (CandidatePersonRow("second", "person-2", "parent-1"),))
        settled = self.db.decide_identity("first", ReviewAction.VERIFY.value)
        self.assertEqual(set(settled), {"first", "second", "synthetic"})
        decisions = query(self.db, "SELECT row_key, decision_action FROM links ORDER BY row_key")
        self.assertEqual(
            [tuple(row) for row in decisions], [("first", "verify"), ("second", "detach"), ("synthetic", "detach")]
        )

    def test_store_has_no_generic_decision_or_update_escape_hatch(self) -> None:
        tables = {row[0] for row in query(self.db, "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("decisions", tables)
        self.assertNotIn("verdicts", tables)
        self.assertFalse(hasattr(self.db, "upsert_decision"))
        self.assertFalse(hasattr(self.db, "update_link"))


if __name__ == "__main__":
    unittest.main()
