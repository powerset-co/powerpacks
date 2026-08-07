"""Identity decisions stay parent-owned under projection, ordering, and merges."""

from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.identity_invariants import (
    IdentityInvariantAudit,
)
from packs.ingestion.primitives.deep_context.db.identity_policy import IdentityPolicy
from packs.ingestion.primitives.deep_context.db.models import (
    CandidatePeopleProjection,
    CandidatePersonRow,
    IdentityMachineProjection,
    LinkRow,
    ParentRow,
    PersonIdentifierRow,
    PersonIdentifiersProjection,
    PersonRow,
    ReviewSource,
)
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.realize.persist_review_identities import (
    PersistReviewIdentities,
)
from packs.shared.csv_io import CsvIO
from deep_context_sqlite_test_helpers import query


def _merge(db: Db, keep: str, remove: str) -> None:
    db.merge_parents(keep, remove)


def _seed_two_parent_db(path: Path) -> Db:
    db = Db(path)
    db.project_rows(
        (
            ParentRow("parent-a", "parent-a", "Parent A"),
            ParentRow("parent-b", "parent-b", "Parent B"),
            PersonRow("person-a", "parent-a"),
            PersonRow("person-b", "parent-b"),
            LinkRow(
                "candidate-a",
                "parent-a",
                "candidate-a",
                "pub",
                "https://www.linkedin.com/in/candidate-a",
            ),
            LinkRow(
                "candidate-b",
                "parent-b",
                "candidate-b",
                "pub",
                "https://www.linkedin.com/in/candidate-b",
            ),
        )
    )
    return db


class IdentityInvariantTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def assert_invariants(self, db: Db) -> None:
        report = IdentityInvariantAudit(db).run()
        self.assertTrue(report.ok, report.issues)

    def test_standing_sql_invariants_report_effective_winner_collisions(self) -> None:
        db = _seed_two_parent_db(self.base / "collision.sqlite")
        _merge(db, "parent-a", "parent-b")
        with db.transaction() as conn:
            conn.execute(
                "UPDATE links SET machine_action='verify', machine_approved='auto'",
            )

        report = IdentityInvariantAudit(db).run()

        self.assertFalse(report.ok)
        self.assertEqual(
            [issue.code for issue in report.issues],
            ["multiple_approved_candidates"],
        )
        self.assertEqual((report.parents_checked, report.links_checked), (1, 2))

    def test_standing_sql_invariants_report_unsettled_human_family(self) -> None:
        db = _seed_two_parent_db(self.base / "unsettled.sqlite")
        _merge(db, "parent-a", "parent-b")
        with db.transaction() as conn:
            conn.execute(
                "UPDATE links SET decision_action='verify', decision_approved='yes', "
                "decision_source='deep-context-review', decided_at='2026-08-06T01:00:00Z' "
                "WHERE row_key='candidate-a'"
            )

        report = IdentityInvariantAudit(db).run()

        self.assertEqual(
            [(issue.code, issue.owner, issue.detail) for issue in report.issues],
            [("undecided_human_siblings", "parent-a", "1")],
        )

    def test_decision_on_one_parent_changes_no_other_parent_rows(self) -> None:
        db = _seed_two_parent_db(self.base / "isolation.sqlite")
        before = [
            tuple(row)
            for row in query(
                db,
                "SELECT * FROM links WHERE parent_id='parent-b' ORDER BY row_key",
            )
        ]

        db.decide_identity("candidate-a", "verify")

        after = [
            tuple(row)
            for row in query(
                db,
                "SELECT * FROM links WHERE parent_id='parent-b' ORDER BY row_key",
            )
        ]
        self.assertEqual(after, before)
        self.assert_invariants(db)

    def test_projection_totality_verify_retarget_export_and_reset(self) -> None:
        db = Db(self.base / "projection.sqlite")
        rows: list[object] = [
            ParentRow("family", "family", "Jordan Family"),
            LinkRow(
                "original",
                "family",
                "original",
                "pub",
                "https://www.linkedin.com/in/original",
                machine_action="verify",
                machine_approved="auto",
            ),
        ]
        for index in range(5):
            person_id = f"child-{index}"
            rows.extend(
                (
                    PersonRow(person_id, "family"),
                    PersonIdentifiersProjection(
                        person_id,
                        (
                            PersonIdentifierRow(
                                person_id,
                                "email",
                                f"child-{index}@example.test",
                            ),
                        ),
                    ),
                )
            )
        db.project_rows(tuple(rows))
        directory = self.base / "directory.csv"

        def exported_identifiers() -> set[str]:
            PersistReviewIdentities(directory_csv=directory, db=db).run()
            return {
                row["public_identifier"]
                for row in CsvIO.read_dict_rows(directory)
                if row["source"] == "deep_context_review"
            }

        db.decide_identity("original", "verify")
        self.assertEqual(exported_identifiers(), {"original"})
        self.assertEqual(len(CsvIO.read_dict_rows(directory)), 5)

        db.decide_identity(
            "original",
            "retarget",
            replacement_url="https://www.linkedin.com/in/replacement",
            replacement_public_identifier="replacement",
        )
        self.assertEqual(exported_identifiers(), {"replacement"})
        self.assertEqual(len(CsvIO.read_dict_rows(directory)), 5)

        db.decide_identity("original", None)
        self.assertEqual(exported_identifiers(), {"original"})
        self.assertEqual(len(CsvIO.read_dict_rows(directory)), 5)
        self.assert_invariants(db)

    def test_decide_then_merge_matches_merge_then_decide(self) -> None:
        before = _seed_two_parent_db(self.base / "before.sqlite")
        after = _seed_two_parent_db(self.base / "after.sqlite")

        before.decide_identity(
            "candidate-a",
            "verify",
            decided_at="2026-08-06T01:00:00Z",
        )
        _merge(before, "parent-a", "parent-b")
        _merge(after, "parent-a", "parent-b")
        after.decide_identity(
            "candidate-a",
            "verify",
            decided_at="2026-08-06T01:00:00Z",
        )

        sql = (
            "SELECT row_key, parent_id, decision_action, decision_approved, "
            "decision_source, decided_at FROM links ORDER BY row_key"
        )
        self.assertEqual(
            [tuple(row) for row in query(before, sql)],
            [tuple(row) for row in query(after, sql)],
        )
        self.assert_invariants(before)
        self.assert_invariants(after)

    def test_merge_keeps_only_the_latest_direct_human_decision(self) -> None:
        db = _seed_two_parent_db(self.base / "latest.sqlite")
        db.decide_identity(
            "candidate-a",
            "verify",
            decided_at="2026-08-06T01:00:00Z",
        )
        db.decide_identity(
            "candidate-b",
            "retarget",
            replacement_url="https://www.linkedin.com/in/latest",
            decided_at="2026-08-06T02:00:00Z",
        )

        _merge(db, "parent-a", "parent-b")

        rows = {row["row_key"]: row for row in query(db, "SELECT * FROM links ORDER BY row_key")}
        self.assertEqual(
            (
                rows["candidate-b"]["decision_action"],
                rows["candidate-b"]["replacement_url"],
            ),
            ("retarget", "https://www.linkedin.com/in/latest"),
        )
        self.assertEqual(
            (
                rows["candidate-a"]["decision_action"],
                rows["candidate-a"]["decision_source"],
            ),
            ("detach", ReviewSource.SIBLING_SETTLE.value),
        )
        self.assert_invariants(db)

    def test_exclude_and_verify_same_second_stay_stable_on_reprojection(self) -> None:
        db = Db(self.base / "exclude-verify.sqlite")
        db.project_rows((
            ParentRow("family", "family"),
            LinkRow("excluded", "family", "excluded", "pub"),
            LinkRow("verified", "family", "verified", "pub"),
        ))
        at = "2026-08-06T01:00:00Z"
        db.decide_identity(
            "excluded",
            "exclude",
            note="not this person",
            decided_at=at,
        )
        db.decide_identity("verified", "verify", decided_at=at)
        expected = [
            tuple(row)
            for row in query(
                db,
                "SELECT row_key, decision_action, decision_source, decision_note "
                "FROM links ORDER BY row_key",
            )
        ]

        for _ in range(2):
            with db.transaction() as conn:
                IdentityPolicy.settle_human_families(conn, ("family",))
            db.project_rows((LinkRow("excluded", "family", "excluded", "pub"),))

        actual = [
            tuple(row)
            for row in query(
                db,
                "SELECT row_key, decision_action, decision_source, decision_note "
                "FROM links ORDER BY row_key",
            )
        ]
        self.assertEqual(actual, expected)
        self.assertEqual(
            actual,
            [
                ("excluded", "exclude", ReviewSource.REVIEW.value, "not this person"),
                ("verified", "verify", ReviewSource.REVIEW.value, None),
            ],
        )
        self.assert_invariants(db)

    def test_exclude_only_does_not_settle_a_later_sibling(self) -> None:
        db = Db(self.base / "exclude-only.sqlite")
        db.project_rows((
            ParentRow("family", "family"),
            LinkRow("excluded", "family", "excluded", "pub"),
        ))
        db.decide_identity(
            "excluded",
            "exclude",
            note="not this person",
            decided_at="2026-08-06T01:00:00Z",
        )

        db.project_rows((LinkRow("later", "family", "later", "pub"),))
        with db.transaction() as conn:
            IdentityPolicy.settle_human_families(conn, ("family",))

        rows = {
            row["row_key"]: row
            for row in query(db, "SELECT * FROM links ORDER BY row_key")
        }
        self.assertEqual(
            (
                rows["excluded"]["decision_action"],
                rows["excluded"]["decision_note"],
            ),
            ("exclude", "not this person"),
        )
        self.assertIsNone(rows["later"]["decision_action"])
        self.assert_invariants(db)

    def test_same_second_affirmative_winners_use_row_key_tiebreak(self) -> None:
        db = _seed_two_parent_db(self.base / "same-second.sqlite")
        at = "2026-08-06T01:00:00Z"
        db.decide_identity("candidate-a", "verify", decided_at=at)
        db.decide_identity(
            "candidate-b",
            "retarget",
            replacement_url="https://www.linkedin.com/in/candidate-b-new",
            decided_at=at,
        )

        db.merge_parents("parent-a", "parent-b")

        rows = {
            row["row_key"]: row
            for row in query(db, "SELECT * FROM links ORDER BY row_key")
        }
        self.assertEqual(
            (
                rows["candidate-a"]["decision_action"],
                rows["candidate-a"]["decision_source"],
            ),
            ("verify", ReviewSource.REVIEW.value),
        )
        self.assertEqual(
            (
                rows["candidate-b"]["decision_action"],
                rows["candidate-b"]["decision_source"],
                rows["candidate-b"]["replacement_url"],
            ),
            ("detach", ReviewSource.SIBLING_SETTLE.value, None),
        )
        self.assert_invariants(db)

    def test_ghost_row_settle_regression(self) -> None:
        db = Db(self.base / "ghost.sqlite")
        db.project_rows(
            (
                ParentRow("family", "family"),
                PersonRow("person", "family"),
                PersonRow("ghost-person", "family", is_ghost=1),
                LinkRow("clicked", "family", "clicked", "pub"),
                LinkRow("ghost", "family", "ghost", "ghost"),
            )
        )

        settled = db.decide_identity("clicked", "verify")

        self.assertEqual(set(settled), {"clicked", "ghost"})
        ghost = query(db, "SELECT * FROM links WHERE row_key='ghost'")[0]
        self.assertEqual(
            (ghost["decision_action"], ghost["decision_source"]),
            ("detach", ReviewSource.SIBLING_SETTLE.value),
        )
        self.assert_invariants(db)

    def test_completion_order_fanout_regression(self) -> None:
        db = Db(self.base / "completion.sqlite")
        db.project_rows(
            (
                ParentRow("family", "family"),
                PersonRow("person", "family"),
                LinkRow("clicked", "family", "clicked", "pub"),
            )
        )
        db.decide_identity(
            "clicked",
            "retarget",
            replacement_url="https://www.linkedin.com/in/chosen",
        )

        db.project_rows(
            (
                LinkRow(
                    "late-result",
                    "family",
                    "late-result",
                    "research",
                    "https://www.linkedin.com/in/late-result",
                    machine_action="verify",
                    machine_approved="auto",
                ),
            )
        )

        late = query(db, "SELECT * FROM links WHERE row_key='late-result'")[0]
        self.assertEqual(
            (
                late["decision_action"],
                late["decision_approved"],
                late["decision_source"],
                late["replacement_url"],
            ),
            ("detach", "yes", ReviewSource.SIBLING_SETTLE.value, None),
        )
        self.assert_invariants(db)

    def test_seeded_thousand_step_operation_walk(self) -> None:
        rng = random.Random(20260806)
        db = Db(self.base / "walk.sqlite")
        db.project_rows(
            tuple(
                row
                for index in range(4)
                for row in (
                    ParentRow(f"parent-{index}", f"parent-{index}"),
                    PersonRow(f"person-{index}", f"parent-{index}"),
                )
            )
        )
        next_person = 4
        next_candidate = 0

        for step in range(1_000):
            snapshot = canonical_snapshot(db)
            parent_ids = [row.parent_id for row in snapshot.parents]
            links = [str(row["row_key"]) for row in db.query("SELECT row_key FROM links")]
            operation = rng.choice(
                (
                    "child",
                    "candidate",
                    "decide",
                    "reset",
                    "merge",
                    "machine",
                )
            )
            if operation == "child":
                parent_id = rng.choice(parent_ids)
                person_id = f"person-{next_person}"
                next_person += 1
                db.project_rows((PersonRow(person_id, parent_id),))
            elif operation == "candidate":
                parent_id = rng.choice(parent_ids)
                person = rng.choice(
                    [row for row in snapshot.people if row.parent_id == parent_id]
                )
                row_key = f"candidate-{next_candidate}"
                next_candidate += 1
                db.project_rows(
                    (
                        LinkRow(
                            row_key,
                            parent_id,
                            row_key,
                            "pub",
                            f"https://www.linkedin.com/in/{row_key}",
                        ),
                        CandidatePeopleProjection(
                            row_key,
                            (CandidatePersonRow(row_key, person.person_id, parent_id),),
                        ),
                    )
                )
            elif operation == "decide" and links:
                row_key = rng.choice(links)
                action = rng.choice(("verify", "detach", "retarget"))
                replacement = f"https://www.linkedin.com/in/replacement-{step}" if action == "retarget" else None
                db.decide_identity(
                    row_key,
                    action,
                    replacement_url=replacement,
                    decided_at=f"2026-08-06T00:{step // 60:02d}:{step % 60:02d}Z",
                )
            elif operation == "reset" and links:
                db.decide_identity(rng.choice(links), None)
            elif operation == "merge" and len(parent_ids) > 1:
                keep, remove = rng.sample(parent_ids, 2)
                db.merge_parents(keep, remove)
            elif operation == "machine" and links:
                action = rng.choice(("verify", "retarget", "detach"))
                db.project_rows(
                    (
                        IdentityMachineProjection(
                            rng.choice(links),
                            machine_action=action,
                            machine_approved="auto",
                            machine_proposed_url=(
                                f"https://www.linkedin.com/in/machine-{step}"
                                if action == "retarget"
                                else None
                            ),
                        ),
                    )
                )
            report = IdentityInvariantAudit(db).run()
            self.assertTrue(report.ok, (step, operation, report.issues))

    def test_machine_projection_clears_two_automatic_winners(self) -> None:
        db = Db(self.base / "machine-projection.sqlite")
        db.project_rows(
            (
                ParentRow("family", "family"),
                PersonRow("person", "family"),
                LinkRow(
                    "candidate-a",
                    "family",
                    "candidate-a",
                    "pub",
                    machine_action="verify",
                    machine_approved="auto",
                ),
                LinkRow(
                    "candidate-b",
                    "family",
                    "candidate-b",
                    "pub",
                    machine_action="retarget",
                    machine_approved="auto",
                ),
            )
        )

        approvals = query(
            db,
            "SELECT machine_approved FROM links ORDER BY row_key",
        )

        self.assertEqual([row["machine_approved"] for row in approvals], [None, None])
        self.assert_invariants(db)

    def test_identity_policy_directly_clears_automatic_winner_conflicts(self) -> None:
        db = _seed_two_parent_db(self.base / "identity-policy.sqlite")
        _merge(db, "parent-a", "parent-b")
        with db.transaction() as conn:
            conn.execute(
                "UPDATE links SET machine_action='verify', machine_approved='auto'"
            )

            IdentityPolicy.clear_machine_winner_conflicts(conn, ("parent-a",))

            approvals = [
                row["machine_approved"]
                for row in conn.execute(
                    "SELECT machine_approved FROM links ORDER BY row_key"
                )
            ]
        self.assertEqual(approvals, [None, None])

    def test_parent_merge_clears_automatic_winner_conflict(self) -> None:
        db = _seed_two_parent_db(self.base / "machine-merge.sqlite")
        db.project_rows(
            (
                IdentityMachineProjection(
                    "candidate-a",
                    machine_action="verify",
                    machine_approved="auto",
                ),
                IdentityMachineProjection(
                    "candidate-b",
                    machine_action="verify",
                    machine_approved="auto",
                ),
            )
        )

        _merge(db, "parent-a", "parent-b")

        approvals = query(
            db,
            "SELECT machine_approved FROM links ORDER BY row_key",
        )
        self.assertEqual([row["machine_approved"] for row in approvals], [None, None])
        self.assert_invariants(db)


if __name__ == "__main__":
    unittest.main()
