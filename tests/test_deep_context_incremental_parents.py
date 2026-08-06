"""Incremental parent merges stay equivalent to the legacy migration rebuild."""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.build_parents import BuildParents
from packs.ingestion.primitives.deep_context.db.legacy import LegacyGraphMigration
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactRow,
    CandidatePeopleProjection,
    CandidatePersonRow,
    CanonicalGraphProjection,
    FactRow,
    LinkRow,
    MergeVerdictRow,
    ParentRow,
    PersonRow,
    ProjectionStatus,
    ReviewSource,
    RowKind,
)
from packs.ingestion.primitives.deep_context.db.store import Db


DEEP_CONTEXT = Path("packs/ingestion/primitives/deep_context")
MIGRATION_GRAPH_CALLERS = {
    DEEP_CONTEXT / "db" / "legacy.py",
}


def _seed(db: Db) -> None:
    rows: list[object] = [
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
        LinkRow(
            "link-a", "parent-a", "jordan-a", RowKind.PUB.value,
            "https://www.linkedin.com/in/jordan-a",
        ),
        LinkRow(
            "link-b", "parent-b", "jordan-b", RowKind.PUB.value,
            "https://www.linkedin.com/in/jordan-b",
        ),
        CandidatePeopleProjection(
            "link-a", (CandidatePersonRow("link-a", "person-a", "parent-a"),),
        ),
        CandidatePeopleProjection(
            "link-b", (CandidatePersonRow("link-b", "person-b", "parent-b"),),
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
            ),
            FactRow(
                person_id,
                parent_id,
                artifact_key,
                person_id=person_id,
                machine_worth="yes" if suffix == "a" else "maybe",
                facts_json=f'{{"side":"{suffix}"}}',
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
    state = {
        table: [tuple(row) for row in db.query(f"SELECT * FROM {table} ORDER BY 1")]
        for table in ("parents", "people", "links", "facts")
    }
    state["decisions"] = [tuple(row) for row in db.query(
        "SELECT row_key, decision_action, decision_approved, decision_source, "
        "decision_note, decided_at, replacement_url, replacement_public_identifier "
        "FROM links ORDER BY row_key"
    )]
    return state


class IncrementalParentMaintenanceTest(unittest.TestCase):
    def test_accepted_merge_matches_legacy_whole_graph_end_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = Db(root / "legacy.sqlite")
            incremental = Db(root / "incremental.sqlite")
            _seed(legacy)
            _seed(incremental)

            LegacyGraphMigration.apply(legacy, CanonicalGraphProjection(
                parents=(ParentRow(
                    "parent-b", "parent-worth:b", "Jordan Bravo", "jordan-bravo",
                    "maybe", "Possible collaborator", ReviewSource.PARENT_WORTH.value,
                    "2026-08-06T00:00:00Z",
                ),),
                people=(
                    PersonRow(
                        "person-a", "parent-b", "jordan-a", "jordan-bravo",
                        "Jordan Alpha", facts_json='{"canonical_name":"Jordan Alpha"}',
                        updated_at="2026-08-06T00:00:00Z",
                    ),
                    PersonRow(
                        "person-b", "parent-b", "jordan-b", "jordan-bravo",
                        "Jordan Bravo", facts_json='{"canonical_name":"Jordan Bravo"}',
                        updated_at="2026-08-06T00:00:00Z",
                    ),
                ),
                identifiers=(),
                sources=(),
            ))
            result = BuildParents(
                db=incremental,
                parents_dir=root / "parents",
            ).execute()

            self.assertEqual(result.merged_parents, 1)
            self.assertEqual(_state(incremental), _state(legacy))

    def test_whole_graph_calls_are_confined_to_migration_code(self) -> None:
        callers: set[Path] = set()
        for path in sorted(DEEP_CONTEXT.rglob("*.py")):
            if "tools" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            if any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {
                    "replace_canonical_graph", "_replace_canonical_graph",
                }
                for node in ast.walk(tree)
            ):
                callers.add(path)

        self.assertEqual(callers, MIGRATION_GRAPH_CALLERS)


if __name__ == "__main__":
    unittest.main()
