from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import replace
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactRow,
    CandidatePersonRow,
    HumanWorth,
    IdentifierKind,
    LinkRow,
    MachineWorth,
    MergeVerdictRow,
    ParentRow,
    PersonIdentifierRow,
    PersonRow,
    PersonSourceRow,
    ProjectionStatus,
    ReviewAction,
    SyntheticProfileRow,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.db.schema import (
    DDL,
    MERGE_VERDICTS_DDL,
    BINARY_MERGE_VERDICTS_DDL,
    IMPORTED_PEOPLE_DDL,
    RESEARCH_INDEX_DDL,
    SCHEMA_VERSION,
)
from packs.ingestion.primitives.deep_context.db.store import Db, SchemaVersionError, StoreError
from packs.ingestion.primitives.deep_context.db.queries import merge_verdicts
from packs.ingestion.primitives.deep_context.db.audit_identity import IdentityAudit, AuditCategory
from packs.ingestion.primitives.pipeline.contract import PeopleRow
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


def _journal_mode(path: Path) -> str:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("PRAGMA journal_mode").fetchone()[0]


def _index_names(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}


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
        project_candidate(self.db, LinkRow(key, parent_id, key, kind, source=WriterSource.RECONCILE.value))

    def test_existing_incompatible_layout_fails_before_mutation(self) -> None:
        self.parent()
        with sqlite3.connect(self.path) as conn:
            conn.execute("ALTER TABLE links ADD COLUMN rogue TEXT")
        with self.assertRaisesRegex(SchemaVersionError, rf"layout does not match schema version {SCHEMA_VERSION}"):
            Db(self.path)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM parents").fetchone()[0], 1)
            self.assertIn("rogue", {row[1] for row in conn.execute("PRAGMA table_info(links)")})

    def test_schema_version_includes_uncertain_merge_verdicts(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 3)

    def test_v1_upgrade_preserves_decisions_and_reopens(self) -> None:
        self.path.unlink()
        with sqlite3.connect(self.path) as conn:
            conn.executescript(DDL.replace(MERGE_VERDICTS_DDL, BINARY_MERGE_VERDICTS_DDL)
                               .replace(IMPORTED_PEOPLE_DDL, "").replace(RESEARCH_INDEX_DDL, ""))
            conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
            conn.execute("INSERT INTO parents(parent_id, public_identifier, human_worth) "
                         "VALUES ('parent-1', 'parent-worth:parent-1', 'yes')")
            conn.execute("INSERT INTO people(person_id, parent_id) VALUES ('person-1', 'parent-1')")
            conn.execute("INSERT INTO links(row_key, parent_id, public_identifier, kind, "
                         "decision_action, decision_approved, source) "
                         "VALUES ('jordan-bravo', 'parent-1', 'jordan-bravo', 'pub', "
                         "'verify', 'yes', 'deep-context-reconcile')")
        upgraded = Db(self.path)
        self.assertEqual(upgraded.query("SELECT human_worth FROM parents")[0][0], "yes")
        self.assertEqual(upgraded.query("SELECT decision_approved FROM links")[0][0], "yes")
        self.assertEqual(upgraded.query("PRAGMA foreign_key_check"), [])
        self.assertEqual(upgraded.query("SELECT * FROM imported_people"), [])
        self.assertEqual(Db(self.path).query("SELECT value FROM meta WHERE key='schema_version'")[0][0], "3")

    def test_store_opens_in_wal_with_research_index(self) -> None:
        # A fresh store and a pre-3.8.2 store (rollback journal, no research index)
        # both end up in WAL — a long read no longer blocks a decision's commit —
        # with the index that keeps identity queries off full scans of research.
        self.assertEqual(_journal_mode(self.path), "wal")
        self.assertIn("research_by_candidate", _index_names(self.path))
        self.path.unlink()
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executescript(DDL.replace(MERGE_VERDICTS_DDL, BINARY_MERGE_VERDICTS_DDL).replace(RESEARCH_INDEX_DDL, ""))
            conn.execute("INSERT INTO meta VALUES ('schema_version', '2')")
            conn.commit()
        self.assertEqual(_journal_mode(self.path), "delete")
        Db(self.path)
        self.assertEqual(_journal_mode(self.path), "wal")
        self.assertIn("research_by_candidate", _index_names(self.path))
        Db(self.path)  # a second open is a no-op on an up-to-date store

    def test_merge_verdict_requires_cache_provenance(self) -> None:
        self.parent()
        self.person("person-a")
        self.person("person-b")
        for signature, judge in (("", "llm"), ("evidence-v1", ""), ("evidence-v1", "other")):
            with self.subTest(signature=signature, judge=judge), self.assertRaises(sqlite3.IntegrityError):
                self.db.replace_merge_verdicts((
                    MergeVerdictRow(
                        "person-a",
                        "person-b",
                        "a",
                        "b",
                        signature,
                        judge,
                        False,
                        0.0,
                        False,
                    ),
                ))

    def test_uncertain_merge_verdict_roundtrips_without_becoming_different(self) -> None:
        self.parent()
        self.person("person-a")
        self.person("person-b")
        row = MergeVerdictRow("person-a", "person-b", "a", "b", "current-input", "llm", None, 0.8, False,
                              reason="not enough identity evidence")
        self.db.replace_merge_verdicts((row,))
        self.assertIsNone(merge_verdicts(Db(self.path))[0].same_person)
        self.assertFalse(merge_verdicts(self.db)[0].accepted)

    def test_only_explicit_same_person_can_be_accepted(self) -> None:
        self.parent()
        self.person("person-a")
        self.person("person-b")
        row = MergeVerdictRow("person-a", "person-b", "a", "b", "current-input", "llm", True, 0.9, True,
                              accepted=True)
        for value in (None, False):
            with self.subTest(value=value), self.assertRaises(sqlite3.IntegrityError):
                self.db.replace_merge_verdicts((replace(row, same_person=value),))
        self.db.replace_merge_verdicts((row,))
        self.assertTrue(merge_verdicts(self.db)[0].accepted)

    def test_v2_upgrade_preserves_paid_rows_without_binary_authority(self) -> None:
        path = Path(self.temp.name) / "binary.sqlite"
        binary_ddl = DDL.replace(MERGE_VERDICTS_DDL, BINARY_MERGE_VERDICTS_DDL)
        with sqlite3.connect(path) as conn:
            conn.executescript(binary_ddl)
            conn.execute("INSERT INTO meta VALUES ('schema_version', '2')")
            conn.execute("INSERT INTO parents(parent_id,public_identifier,human_worth) VALUES ('p','p','yes')")
            conn.executemany("INSERT INTO people(person_id,parent_id) VALUES (?,'p')", (("a",), ("b",), ("c",)))
            conn.execute("INSERT INTO links(row_key,parent_id,public_identifier,kind,decision_action,decision_approved,source) "
                         "VALUES ('jordan-bravo','p','jordan-bravo','pub','verify','yes','deep-context-reconcile')")
            conn.executemany("INSERT INTO merge_verdicts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                ("a", "b", "a", "b", "paid-positive", "llm", 1, .9, 1, "paid positive reason", 1, "2026-10-03"),
                ("a", "c", "a", "c", "paid-negative", "llm", 0, .2, 0, "insufficient evidence", 0, "2026-10-03"),
                ("b", "c", "b", "c", "old-free", "slam_dunk", 1, .99, 1, "old free reason", 1, "2026-10-03"),
            ))
        upgraded = Db(path)
        rows = merge_verdicts(upgraded)
        self.assertEqual(len(rows), 3)
        self.assertEqual([(r.signature, r.reason, r.confidence) for r in rows], [
            ("paid-positive", "paid positive reason", .9),
            ("paid-negative", "insufficient evidence", .2),
            ("old-free", "old free reason", .99),
        ])
        self.assertEqual([r.same_person for r in rows], [True, None, True])
        self.assertEqual([r.accepted for r in rows], [False, False, False])
        self.assertEqual(upgraded.query("SELECT human_worth FROM parents")[0][0], "yes")
        self.assertEqual(upgraded.query("SELECT decision_approved FROM links")[0][0], "yes")
        self.assertEqual(upgraded.query("PRAGMA foreign_key_check"), [])
        self.assertEqual(merge_verdicts(Db(path)), rows)

    def test_paid_verdict_checkpoint_does_not_clear_other_accepted_edges(self) -> None:
        self.parent("parent-a")
        self.parent("parent-b")
        self.parent("parent-c")
        for name in ("a", "b", "c"):
            self.person(name, f"parent-{name}")
        accepted = MergeVerdictRow("a", "b", "a", "b", "accepted-input", "sol", True, .95, True,
                                   accepted=True)
        pending = MergeVerdictRow("a", "c", "a", "c", "paid-input", "sol", None, .9, False)
        self.db.replace_merge_verdicts((accepted,))
        self.db.project_rows((pending,))
        self.assertEqual([(r.signature, r.same_person, r.accepted) for r in merge_verdicts(Db(self.path))],
                         [("accepted-input", True, True), ("paid-input", None, False)])

    def test_readonly_audit_reports_proved_different_but_not_uncertain(self) -> None:
        self.parent()
        for name in ("a", "b", "c"):
            self.person(name)
        self.db.replace_imported_people(tuple(
            PeopleRow(id=name, full_name="Jordan Bravo") for name in ("a", "b", "c")
        ))
        rows = (
            MergeVerdictRow("a", "b", "a", "b", "ab-input", "sol", True, .95, True, accepted=True),
            MergeVerdictRow("b", "c", "b", "c", "bc-input", "sol", True, .95, True, accepted=True),
            MergeVerdictRow("a", "c", "a", "c", "ac-input", "sol", None, .9, False),
        )
        self.db.replace_merge_verdicts(rows)
        report = IdentityAudit(db_path=self.path).run()
        self.assertFalse(report.parents)
        self.db.project_rows((replace(rows[-1], same_person=False),))
        findings = [f.category for p in IdentityAudit(db_path=self.path).run().parents for f in p.findings]
        self.assertEqual(findings, [AuditCategory.ACCEPTED_MERGE_CONFLICTS_WITH_NEGATIVE])

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
            project_candidate(
                self.db, LinkRow("orphan", "missing", "orphan", "pub", source=WriterSource.RECONCILE.value)
            )

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
                source=WriterSource.RECONCILE.value,
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
                 ("mmm", "detach", "sibling-settle", None),
                 ("zzz", "verify", "deep-context-review", None)),
            ),
            (
                "verify-then-detach",
                (("zzz", "verify", None), ("aaa", "detach", "skip note")),
                (("aaa", "detach", "deep-context-review", "skip note"),
                 ("mmm", "detach", "sibling-settle", None),
                 ("zzz", "verify", "deep-context-review", None)),
            ),
            (
                "detach-only",
                (("aaa", "detach", "skip note"),),
                (("aaa", "detach", "deep-context-review", "skip note"),
                 ("mmm", "detach", "sibling-settle", None),
                 ("zzz", "detach", "sibling-settle", None)),
            ),
        )
        for name, clicks, expected in cases:
            with self.subTest(name=name):
                path = Path(self.temp.name) / f"{name}.sqlite"
                db = Db(path)
                db.project_rows((
                    ParentRow("family", "family"),
                    *(
                        LinkRow(key, "family", key, "pub", source=WriterSource.RECONCILE.value)
                        for key in ("aaa", "mmm", "zzz")
                    ),
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

    def test_human_linkedin_url_settles_the_whole_family(self) -> None:
        url = "https://www.linkedin.com/in/jordan-bravo-2"
        family = ("aaa", "mmm", "zzz")
        columns = (
            "SELECT row_key, decision_action, decision_source, decision_note, "
            "replacement_url, replacement_public_identifier FROM links ORDER BY row_key"
        )
        for order in ("verify-then-retarget", "retarget-then-verify"):
            with self.subTest(order=order):
                self.db = Db(Path(self.temp.name) / f"{order}.sqlite")
                self.parent()
                for key in family:
                    self.candidate(key)
                self.db.decide_identity("mmm", ReviewAction.EXCLUDE.value, note="not Jordan")

                if order == "verify-then-retarget":
                    self.db.decide_identity("aaa", ReviewAction.VERIFY.value)
                    settled = self.db.decide_identity(
                        "zzz",
                        ReviewAction.RETARGET.value,
                        replacement_url=url,
                        replacement_public_identifier="jordan-bravo-2",
                    )
                    # The human-excluded row is kept, so only the rewritten rows come back.
                    self.assertEqual(settled, ["zzz", "aaa"])
                    expected = [
                        ("aaa", "detach", "sibling-settle", None, None, None),
                        ("mmm", "exclude", "deep-context-review", "not Jordan", None, None),
                        ("zzz", "retarget", "deep-context-review", None, url, "jordan-bravo-2"),
                    ]
                else:
                    settled = self.db.decide_identity(
                        "zzz",
                        ReviewAction.RETARGET.value,
                        replacement_url=url,
                        replacement_public_identifier="jordan-bravo-2",
                        note="casey@example.com says this is Jordan",
                    )
                    # The pasted URL alone settles the pending sibling.
                    self.assertEqual(settled, ["zzz", "aaa"])
                    self.assertEqual(
                        [tuple(row) for row in query(
                            self.db, "SELECT decision_action, decision_source FROM links WHERE row_key='aaa'"
                        )],
                        [("detach", "sibling-settle")],
                    )
                    settled = self.db.decide_identity("aaa", ReviewAction.VERIFY.value)
                    self.assertEqual(settled, ["aaa", "zzz"])
                    expected = [
                        ("aaa", "verify", "deep-context-review", None, None, None),
                        ("mmm", "exclude", "deep-context-review", "not Jordan", None, None),
                        ("zzz", "detach", "sibling-settle", "casey@example.com says this is Jordan", None, None),
                    ]

                self.assertEqual([tuple(row) for row in query(self.db, columns)], expected)
                self.assertFalse(query(self.db, "SELECT row_key FROM links WHERE decision_action IS NULL"))

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
                source=WriterSource.RECONCILE.value,
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
                source=WriterSource.RECONCILE.value,
            ),
        )
        row = query(self.db, "SELECT * FROM links WHERE row_key='candidate-1'")[0]
        self.assertEqual(row["machine_proposed_public_identifier"], "new-proposal")
        self.assertEqual(row["replacement_public_identifier"], "chosen")

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

    def test_schema_has_no_stage_approval_or_job_ledgers(self) -> None:
        tables = {row[0] for row in query(
            self.db, "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertFalse({"stage_state", "spend_approvals", "jobs"} & tables)

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
