"""Keep Deep Context dependencies visible and importable at module load."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


DEEP_CONTEXT = Path("packs/ingestion/primitives/deep_context")
DB_PACKAGE = DEEP_CONTEXT / "db"
MIGRATION_PACKAGE = DEEP_CONTEXT / "migration"
EXPECTED_DB_OPERATIONS = {
    "identity_views.approved_identities",
    "identity_views.attached_identity_queue",
    "identity_views.decision_parents",
    "identity_views.enrichment_queue",
    "identity_views.heal_identity_queue",
    "identity_views.human_settled_identities",
    "identity_views.linkedin_parents",
    "identity_views.linkedin_progress",
    "identity_views.linkedin_queue",
    "identity_views.resolve_identity_key",
    "identity_views.synthetic_fallback",
    "merge_queries.merge_people",
    "people_views.avatar_payload",
    "people_views.person_detail",
    "people_views.person_lookup",
    "projectors.project_parent_fact",
    "projectors.project_parent_source_bundle",
    "queries.artifacts",
    "context_queries.collection_bundle_group_message_count",
    "context_queries.collection_bundle_parent_ids",
    "context_queries.collection_sources",
    "context_queries.dossier_evidence_rows",
    "context_queries.existing_parent_ids",
    "queries.facts",
    "identity_queries.guidance_rows",
    "queries.identifiers",
    "identity_queries.links",
    "identity_queries.memberships",
    "identity_queries.parent_has_contact_identifier",
    "queries.merge_verdicts",
    "queries.owner_path",
    "queries.owner_profile",
    "queries.parent_fact_counts",
    "queries.parents",
    "queries.people",
    "identity_queries.research_rows",
    "identity_queries.review_rows",
    "queries.sources",
    "identity_queries.synthetic_profiles",
    "queries.typed_rows",
    "snapshots.canonical_snapshot",
    "store.Db.decide_identity",
    "store.Db.decide_worth",
    "store.Db.merge_parents",
    "store.Db.project_rows",
    "store.Db.prune_synthetic_candidates",
    "store.Db.query",
    "store.Db.replace_merge_verdicts",
    "store.Db.reset_review",
    "store.Db.transaction",
    "store.open_existing_db",
    "workflow_views.workflow_state",
    "worth_views.worth_counts",
    "worth_views.worth_queue",
    "worth_views.worth_rows",
}


class DeepContextImportHygieneTests(unittest.TestCase):
    def test_deep_context_has_no_nested_imports(self) -> None:
        nested: list[str] = []
        for path in sorted(DEEP_CONTEXT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)) and node.col_offset:
                    nested.append(f"{path}:{node.lineno}")

        self.assertEqual(nested, [])

    def test_db_public_surface_is_exact(self) -> None:
        operations: set[str] = set()
        for path in sorted(DB_PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            module = path.stem
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not node.name.startswith("_"):
                        operations.add(f"{module}.{node.name}")
                elif isinstance(node, ast.ClassDef) and node.name == "Db":
                    for member in node.body:
                        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            if not member.name.startswith("_"):
                                operations.add(f"{module}.Db.{member.name}")

        self.assertEqual(operations, EXPECTED_DB_OPERATIONS)

    def test_canonical_sqlite_access_stays_inside_db_package(self) -> None:
        direct_db_calls: list[str] = []
        sqlite_imports: list[str] = []
        for path in sorted(DEEP_CONTEXT.rglob("*.py")):
            if DB_PACKAGE in path.parents or MIGRATION_PACKAGE in path.parents:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"query", "_query", "transaction"}
                ):
                    direct_db_calls.append(f"{path}:{node.lineno}:{node.func.attr}")
                if (
                    isinstance(node, ast.Import)
                    and any(alias.name == "sqlite3" for alias in node.names)
                    or isinstance(node, ast.ImportFrom)
                    and node.module == "sqlite3"
                ):
                    sqlite_imports.append(f"{path}:{node.lineno}")

        self.assertEqual(direct_db_calls, [])
        self.assertEqual(sqlite_imports, [])


if __name__ == "__main__":
    unittest.main()
