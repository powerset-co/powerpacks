"""Incremental parent merges stay equivalent to the legacy migration rebuild."""

from __future__ import annotations

import ast
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from packs.ingestion.primitives.deep_context.build_parents import BuildParents
from packs.ingestion.primitives.deep_context.db.legacy import LegacyGraphMigration
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactRow,
    CandidatePeopleProjection,
    CandidatePersonRow,
    CanonicalGraphProjection,
    FactRow,
    GuidanceRow,
    JobKind,
    JobRow,
    JobStatus,
    LinkRow,
    MergeVerdictRow,
    OwnerContextRow,
    ParentRow,
    PersonIdentifierRow,
    PersonIdentifiersProjection,
    PersonRow,
    PersonSourceRow,
    PersonSourcesProjection,
    ProjectionStatus,
    ResearchRow,
    ResearchStatus,
    ReviewSource,
    RowKind,
    SyntheticProfileRow,
)
from packs.ingestion.primitives.deep_context.db.schema import TABLES
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.parents.assignment import load_assignment
from packs.ingestion.primitives.deep_context.parents.graph import clusters_from_pairs


DEEP_CONTEXT = Path("packs/ingestion/primitives/deep_context")
MIGRATION_GRAPH_CALLERS = {
    DEEP_CONTEXT / "db" / "legacy.py",
    DEEP_CONTEXT / "tools" / "parent_identity_proof.py",
}


def _seed(db: Db) -> None:
    rows: list[object] = [
        OwnerContextRow(
            "owner", '{"owner":true}', "/tmp/owner.json", "sha-owner",
            "2026-08-06T00:00:00Z",
        ),
        ParentRow(
            "parent-a", "parent-worth:a", "Jordan Alpha", "jordan-alpha",
            "yes", "Known collaborator", ReviewSource.PARENT_WORTH.value,
            "2026-08-06T00:00:00Z",
        ),
        ParentRow(
            "parent-b", "parent-worth:b", "Jordan Bravo", "jordan-bravo",
            "maybe", "Possible collaborator", ReviewSource.PARENT_WORTH.value,
            "2026-08-06T00:00:00Z",
        ),
        PersonRow(
            "person-a", "parent-a", "jordan-a", "jordan-alpha", "Jordan Alpha",
            facts_json='{"canonical_name":"Jordan Alpha"}',
            updated_at="2026-08-06T00:00:00Z",
        ),
        PersonRow(
            "person-b", "parent-b", "jordan-b", "jordan-bravo", "Jordan Bravo",
            facts_json='{"canonical_name":"Jordan Bravo"}',
            updated_at="2026-08-06T00:00:00Z",
        ),
        PersonIdentifiersProjection(
            "person-a",
            (PersonIdentifierRow("person-a", "email", "alpha@example.com"),),
        ),
        PersonIdentifiersProjection(
            "person-b",
            (PersonIdentifierRow("person-b", "phone", "+15550100"),),
        ),
        PersonSourcesProjection(
            "person-a", (PersonSourceRow("person-a", "gmail_msgvault"),),
        ),
        PersonSourcesProjection(
            "person-b", (PersonSourceRow("person-b", "imessage"),),
        ),
        LinkRow(
            "link-a", "parent-a", "jordan-a", RowKind.PUB.value,
            "https://www.linkedin.com/in/jordan-a",
        ),
        LinkRow(
            "link-b", "parent-b", "jordan-b", RowKind.PUB.value,
            "https://www.linkedin.com/in/jordan-b",
        ),
        LinkRow(
            "synthetic-b", "parent-b", "synthetic-b", RowKind.SYNTHETIC.value,
        ),
        CandidatePeopleProjection(
            "link-a", (CandidatePersonRow("link-a", "person-a", "parent-a"),),
        ),
        CandidatePeopleProjection(
            "link-b", (CandidatePersonRow("link-b", "person-b", "parent-b"),),
        ),
        CandidatePeopleProjection(
            "synthetic-b",
            (CandidatePersonRow("synthetic-b", "person-b", "parent-b"),),
        ),
        ArtifactRow(
            "research:link-b",
            ArtifactKind.RESEARCH.value,
            "parent-b",
            "/tmp/research-link-b.json",
            "sha-research-b",
            ProjectionStatus.PROJECTED.value,
            candidate_key="link-b",
            projected_at="2026-08-06T00:00:00Z",
        ),
        ArtifactRow(
            "synthetic:link-b",
            ArtifactKind.SYNTHETIC.value,
            "parent-b",
            "/tmp/synthetic-link-b.json",
            "sha-synthetic-b",
            ProjectionStatus.PROJECTED.value,
            candidate_key="synthetic-b",
            projected_at="2026-08-06T00:00:00Z",
        ),
        SyntheticProfileRow(
            "synthetic-b",
            "synthetic-b",
            '{"name":"Jordan Bravo"}',
            source_artifact_key="synthetic:link-b",
            name="Jordan Bravo",
            updated_at="2026-08-06T00:00:00Z",
        ),
        ResearchRow(
            "research-b",
            "parent-b",
            ResearchStatus.COMPLETE.value,
            candidate_key="link-b",
            artifact_key="research:link-b",
            selection_fingerprint="selection-b",
            result_json='{"status":"complete"}',
            updated_at="2026-08-06T00:00:00Z",
        ),
        GuidanceRow(
            "guidance-b",
            "parent-b",
            "Find the synthetic fixture",
            candidate_key="link-b",
            submitted_at="2026-08-06T00:00:00Z",
        ),
        JobRow(
            "job-b",
            JobKind.GUIDED_RETARGET.value,
            JobStatus.APPLIED.value,
            parent_id="parent-b",
            candidate_key="link-b",
            selection_fingerprint="selection-b",
            completed_count=1,
            total_count=1,
            result_json='{"status":"applied"}',
            started_at="2026-08-06T00:00:00Z",
            finished_at="2026-08-06T00:01:00Z",
        ),
    ]
    for suffix, parent_id, person_id in (
        ("a", "parent-a", "person-a"),
        ("b", "parent-b", "person-b"),
    ):
        artifact_key = f"facts:person-{suffix}"
        rows.extend((
            ArtifactRow(
                artifact_key,
                ArtifactKind.FACTS.value,
                parent_id,
                f"/tmp/person-{suffix}.jsonl",
                f"sha-{suffix}",
                ProjectionStatus.PROJECTED.value,
                person_id=person_id,
                projected_at="2026-08-06T00:00:00Z",
            ),
            FactRow(
                person_id,
                parent_id,
                artifact_key,
                person_id=person_id,
                machine_worth="yes" if suffix == "a" else "maybe",
                facts_json=f'{{"side":"{suffix}"}}',
                projected_at="2026-08-06T00:00:00Z",
            ),
        ))
    db.project_rows(tuple(rows))
    db.replace_merge_verdicts((MergeVerdictRow(
        "person-a", "person-b", "jordan-a", "jordan-b", "evidence-v1",
        "llm", 1, 0.99, 1, "same synthetic person", 1,
        "2026-08-06T02:30:00Z",
    ),))
    db.decide_worth(
        "parent-a", "yes", note="older decision",
        decided_at="2026-08-06T01:00:00Z",
    )
    db.decide_worth(
        "parent-b", "no", note="newer decision",
        decided_at="2026-08-06T02:00:00Z",
    )
    db.decide_identity(
        "link-a", "verify", decided_at="2026-08-06T01:00:00Z",
    )
    db.decide_identity(
        "link-b", "retarget",
        replacement_url="https://www.linkedin.com/in/jordan-final",
        decided_at="2026-08-06T03:00:00Z",
    )


def _state(db: Db) -> dict[str, list[tuple]]:
    """Every canonical application table, including indirect merge dependents."""
    state = {}
    for table in ("meta", *TABLES):
        rows = [tuple(row) for row in db.query(f"SELECT * FROM {table}")]
        state[table] = sorted(rows, key=repr)
    return state


def _legacy_projection_from_clustering(db: Db) -> CanonicalGraphProjection:
    """Derive the old whole-graph projection from the same accepted verdicts."""
    snapshot = canonical_snapshot(db)
    parent_by_person = {row.person_id: row.parent_id for row in snapshot.people}
    pairs = [
        {
            "slug_a": parent_by_person[row.person_a],
            "slug_b": parent_by_person[row.person_b],
        }
        for row in snapshot.merge_verdicts
        if row.accepted
        and parent_by_person.get(row.person_a)
        and parent_by_person.get(row.person_b)
        and parent_by_person[row.person_a] != parent_by_person[row.person_b]
    ]
    target_by_parent = {row.parent_id: row.parent_id for row in snapshot.parents}
    assignment = load_assignment(snapshot)
    for component in clusters_from_pairs(pairs):
        survivor = assignment.elect(component)
        target_by_parent.update((parent_id, survivor) for parent_id in component)

    parents_by_id = {row.parent_id: row for row in snapshot.parents}
    surviving = sorted(set(target_by_parent.values()))
    parents = tuple(
        ParentRow(
            row.parent_id,
            row.public_identifier,
            row.display_name,
            row.display_slug,
            row.machine_worth,
            row.machine_worth_reason,
            row.source,
            row.updated_at,
        )
        for parent_id in surviving
        for row in (parents_by_id[parent_id],)
    )
    display_slug = {row.parent_id: row.display_slug for row in parents}
    people = tuple(
        replace(
            row,
            parent_id=target_by_parent[row.parent_id],
            parent_slug=display_slug[target_by_parent[row.parent_id]],
        )
        for row in snapshot.people
    )
    return CanonicalGraphProjection(
        parents,
        people,
        snapshot.identifiers,
        snapshot.sources,
    )


class IncrementalParentMaintenanceTest(unittest.TestCase):
    def test_accepted_merge_matches_legacy_whole_graph_end_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = Db(root / "legacy.sqlite")
            incremental = Db(root / "incremental.sqlite")
            _seed(legacy)
            _seed(incremental)

            projection = _legacy_projection_from_clustering(legacy)
            LegacyGraphMigration.apply(legacy, projection)
            with patch(
                "packs.ingestion.primitives.deep_context.build_parents.now_iso",
                return_value="2026-08-06T04:00:00Z",
            ):
                BuildParents(db=legacy, parents_dir=root / "parents").execute()
                result = BuildParents(
                    db=incremental,
                    parents_dir=root / "parents",
                ).execute()

            self.assertEqual(result.parents_merged, 1)
            self.assertEqual(_state(incremental), _state(legacy))

    def test_whole_graph_calls_are_confined_to_migration_code(self) -> None:
        callers: set[Path] = set()
        for path in sorted(DEEP_CONTEXT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            aliases = {
                item.asname or item.name
                for node in tree.body
                if isinstance(node, ast.ImportFrom)
                and node.module == "packs.ingestion.primitives.deep_context.db.legacy"
                for item in node.names
                if item.name == "LegacyGraphMigration"
            }
            if any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and (
                    node.func.attr in {
                        "replace_canonical_graph", "_replace_canonical_graph",
                    }
                    or (
                        node.func.attr in {"apply", "_apply"}
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id in aliases
                    )
                )
                for node in ast.walk(tree)
            ):
                callers.add(path)

        self.assertEqual(callers, MIGRATION_GRAPH_CALLERS)


if __name__ == "__main__":
    unittest.main()
