"""Parent identity is minted once and then absorbed, never re-derived."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from packs.ingestion.primitives.deep_context.merge_candidates.build_parents import BuildParents
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    CanonicalSnapshot,
    MergeVerdictRow,
    ParentRow,
    ParentSnapshotRow,
    PersonIdentifierRow,
    PersonIdentifiersProjection,
    PersonRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.ensure_parents.assignment import (
    load_assignment,
    mint_parent_id,
    ParentAssignment,
    ParentFacts,
)

PEOPLE = (
    ("person-a", "jordan-a", "Jordan Bravo"),
    ("person-b", "jordan-b", "Jordan Bravo"),
    ("person-c", "casey-c", "Casey Delta"),
    ("person-d", "casey-d", "Casey Delta"),
)
GMAIL_PEOPLE = (("person-b", "jordan-b"), ("person-d", "casey-d"))
MERGE_ROWS = (
    ("jordan-a", "jordan-b", "Jordan Bravo"),
    ("casey-c", "casey-d", "Casey Delta"),
)


def _parent_snapshot(rows: dict[str, str | None]) -> CanonicalSnapshot:
    """A snapshot whose parents carry only the human worth decision timestamps."""
    return CanonicalSnapshot(
        parents=tuple(
            ParentSnapshotRow(
                parent_id,
                f"parent-worth:{parent_id}",
                human_worth=None if decided_at is None else "yes",
                human_worth_at=decided_at,
            )
            for parent_id, decided_at in rows.items()
        ),
        people=(),
        identifiers=(),
        sources=(),
        artifacts=(),
        facts=(),
        owner=None,
        owner_path=None,
        dossiers=(),
        merge_verdicts=(),
    )


def _load_snapshot_assignment(snapshot: CanonicalSnapshot) -> ParentAssignment:
    """Seed the typed database door used by production assignment loading."""
    temp_dir = tempfile.TemporaryDirectory()
    db = Db(Path(temp_dir.name) / "deep-context.sqlite")
    snapshot_parents = {row.parent_id: row for row in snapshot.parents}
    parent_ids = set(snapshot_parents) | {row.parent_id for row in snapshot.people}
    db.project_rows(
        (
            *(
                ParentRow(
                    parent_id,
                    snapshot_parents[parent_id].public_identifier
                    if parent_id in snapshot_parents
                    else f"parent-worth:{parent_id}",
                    display_name=(snapshot_parents[parent_id].display_name if parent_id in snapshot_parents else None),
                )
                for parent_id in sorted(parent_ids)
            ),
            *snapshot.people,
        )
    )
    with db.transaction() as conn:
        for row in snapshot.parents:
            conn.execute(
                "UPDATE parents SET human_worth=?, human_worth_at=? WHERE parent_id=?",
                (row.human_worth, row.human_worth_at, row.parent_id),
            )
    assignment = load_assignment(db)
    temp_dir.cleanup()
    return assignment


class ParentAssignmentRuleTest(unittest.TestCase):
    def test_empty_existing_set_mints_the_founding_child_set_id(self) -> None:
        assignment = ParentAssignment({}, {})
        self.assertEqual(
            assignment.resolve(["jordan-a", "jordan-b"], ["person-a", "person-b"]),
            mint_parent_id(["person-a", "person-b"]),
        )

    def test_one_existing_parent_absorbs_the_grown_cluster_unchanged(self) -> None:
        assignment = ParentAssignment({"jordan-a": "parent-keepme0001"}, {})
        self.assertEqual(
            assignment.resolve(["jordan-a", "jordan-b"], ["person-a", "person-b"]),
            "parent-keepme0001",
        )

    def test_merge_survivor_prefers_the_newest_human_worth_decision(self) -> None:
        assignment = ParentAssignment(
            {"jordan-a": "parent-aaaa", "jordan-b": "parent-bbbb", "casey-c": "parent-cccc"},
            {
                "parent-aaaa": ParentFacts(1, "2026-01-01T00:00:00Z", 9),
                "parent-bbbb": ParentFacts(1, "2026-02-01T00:00:00Z", 1),
                "parent-cccc": ParentFacts(0, "", 40),
            },
        )
        self.assertEqual(
            assignment.resolve(
                ["jordan-a", "jordan-b", "casey-c"],
                ["person-a", "person-b", "person-c"],
            ),
            "parent-bbbb",
        )

    def test_merge_survivor_falls_back_to_the_most_current_members(self) -> None:
        assignment = ParentAssignment(
            {"jordan-a": "parent-aaaa", "jordan-b": "parent-bbbb"},
            {
                "parent-aaaa": ParentFacts(0, "", 2),
                "parent-bbbb": ParentFacts(0, "", 5),
            },
        )
        self.assertEqual(
            assignment.resolve(["jordan-a", "jordan-b"], ["person-a", "person-b"]),
            "parent-bbbb",
        )

    def test_merge_survivor_falls_back_to_the_smallest_parent_id(self) -> None:
        assignment = ParentAssignment(
            {"jordan-a": "parent-zzzz", "jordan-b": "parent-aaaa"},
            {
                "parent-zzzz": ParentFacts(1, "2026-03-01T00:00:00Z", 3),
                "parent-aaaa": ParentFacts(1, "2026-03-01T00:00:00Z", 3),
            },
        )
        self.assertEqual(
            assignment.resolve(["jordan-a", "jordan-b"], ["person-a", "person-b"]),
            "parent-aaaa",
        )

    def test_an_id_is_claimed_by_at_most_one_cluster_per_build(self) -> None:
        assignment = ParentAssignment({"jordan-a": "parent-shared", "jordan-b": "parent-shared"}, {})
        first = assignment.resolve(["jordan-a"], ["person-a"])
        second = assignment.resolve(["jordan-b"], ["person-b"])
        self.assertEqual(first, "parent-shared")
        self.assertEqual(second, mint_parent_id(["person-b"]))

    def test_reserved_owner_ids_stay_out_of_cluster_resolution(self) -> None:
        assignment = ParentAssignment({"jordan-a": "parent-owned"}, {})
        assignment.reserve("parent-owned")
        self.assertEqual(
            assignment.resolve(["jordan-a"], ["person-a"]),
            mint_parent_id(["person-a"]),
        )

    def test_sqlite_membership_is_the_only_assignment_source(self) -> None:
        snapshot = CanonicalSnapshot(
            parents=(),
            people=(PersonRow("person-b", "parent-fromdb", "jordan-b"),),
            identifiers=(),
            sources=(),
            artifacts=(),
            facts=(),
            owner=None,
            owner_path=None,
            dossiers=(),
            merge_verdicts=(),
        )
        assignment = _load_snapshot_assignment(snapshot)
        self.assertEqual(
            assignment.parent_by_child,
            {"jordan-b": "parent-fromdb"},
        )
        self.assertEqual(assignment.facts["parent-fromdb"], ParentFacts(0, "", 1))

    def test_loaded_facts_carry_the_human_decision_and_member_count(self) -> None:
        snapshot = replace(
            _parent_snapshot({"parent-decided": "2026-04-05T06:07:08Z"}),
            people=(
                PersonRow("person-a", "parent-decided", "jordan-a"),
                PersonRow("person-b", "parent-decided", "jordan-b"),
            ),
        )
        assignment = _load_snapshot_assignment(snapshot)
        self.assertEqual(
            assignment.facts["parent-decided"],
            ParentFacts(1, "2026-04-05T06:07:08Z", 2),
        )


class ParentIncrementalBuildTest(unittest.TestCase):
    """Adding Gmail-derived children must not re-key the parents already minted."""

    def _seed(self, db: Db, root: Path, person_ids: tuple[str, ...]) -> None:
        """Project what upstream stages would have: people with provisional
        singleton parents, one dossier artifact each, and the accepted pairs."""
        existing = {row["person_id"] for row in db.query("SELECT person_id FROM people")}
        rows: list = []
        slug_by_person = {}
        for person_id, slug, name in PEOPLE:
            if person_id not in person_ids:
                continue
            slug_by_person[person_id] = slug
            if person_id in existing:
                continue
            parent_id = f"seed-{person_id}"
            body = f"# {name}\n\nBody\n"
            rows.extend(
                (
                    ParentRow(parent_id, f"parent-worth:{parent_id}", name, slug),
                    PersonRow(person_id, parent_id, slug, slug, name),
                    PersonIdentifiersProjection(
                        person_id,
                        (
                            PersonIdentifierRow(
                                person_id,
                                "email",
                                f"{slug}@example.com",
                                f"{slug}@example.com",
                            ),
                        ),
                    ),
                    ArtifactRow(
                        f"dossier-person:{person_id}",
                        "dossier",
                        parent_id,
                        str(root / "dossiers" / f"{slug}.md"),
                        hashlib.sha256(body.encode()).hexdigest(),
                        "projected",
                        person_id=person_id,
                        payload_json=json.dumps(
                            {
                                "person_id": person_id,
                                "name": name,
                                "path": f"dossiers/{slug}.md",
                                "headline": "Synthetic fixture headline.",
                                "full_name": name,
                                "emails": [f"{slug}@example.com"],
                                "phones": ["+15550100"],
                                "source_channels": ["gmail_msgvault"],
                                "body": body,
                            }
                        ),
                    ),
                )
            )
        db.project_rows(tuple(rows))
        person_by_slug = {slug: pid for pid, slug in slug_by_person.items()}
        db.replace_merge_verdicts(
            tuple(
                MergeVerdictRow(
                    person_by_slug[slug_a],
                    person_by_slug[slug_b],
                    slug_a,
                    slug_b,
                    "sig",
                    "llm",
                    1,
                    0.99,
                    1,
                    "synthetic fixture",
                    1,
                )
                for slug_a, slug_b, _ in MERGE_ROWS
                if slug_a in person_by_slug and slug_b in person_by_slug
            )
        )

    def _build(self, root: Path, person_ids: tuple[str, ...]) -> dict[str, str]:
        db = Db(root / "deep-context.sqlite")
        self._seed(db, root, person_ids)
        BuildParents(db=db, parents_dir=root / "parents").execute()
        return {row["child_slug"]: row["parent_id"] for row in db.query("SELECT child_slug, parent_id FROM people")}

    def test_gmail_children_join_without_re_keying_existing_parents(self) -> None:
        everything = tuple(person_id for person_id, _, _ in PEOPLE)
        seeded = tuple(person_id for person_id in everything if person_id not in {pid for pid, _ in GMAIL_PEOPLE})
        with tempfile.TemporaryDirectory() as directory:
            incremental_root = Path(directory) / "incremental"
            incremental_root.mkdir()
            before = self._build(incremental_root, seeded)
            after = self._build(incremental_root, everything)

            cold_root = Path(directory) / "cold"
            cold_root.mkdir()
            cold = self._build(cold_root, everything)

        self.assertEqual(len(before), len(seeded))
        self.assertEqual(len(after), len(everything))
        self.assertEqual({slug: after[slug] for slug in before}, before)
        self.assertEqual(_partition(after), _partition(cold))
        self.assertEqual(after["jordan-a"], after["jordan-b"])
        self.assertEqual(after["casey-c"], after["casey-d"])


def _partition(parent_by_child: dict[str, str]) -> set[frozenset[str]]:
    groups: dict[str, set[str]] = {}
    for child, parent_id in parent_by_child.items():
        groups.setdefault(parent_id, set()).add(child)
    return {frozenset(group) for group in groups.values()}


if __name__ == "__main__":
    unittest.main()
