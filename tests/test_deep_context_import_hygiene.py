"""Keep Deep Context dependencies visible and importable at module load."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


DEEP_CONTEXT = Path("packs/ingestion/primitives/deep_context")
DB_PACKAGE = DEEP_CONTEXT / "db"
EXPECTED_DB_OPERATIONS = {
    "legacy.import_legacy",
    "projectors.project_manifest",
    "snapshots.canonical_snapshot",
    "snapshots.identity_snapshot",
    "store.Db.decide_identity",
    "store.Db.decide_worth",
    "store.Db.export_batons",
    "store.Db.project_identity",
    "store.Db.project_rows",
    "store.Db.replace_canonical_graph",
    "store.Db.reset_review",
    "store.Db.save_state",
    "views.avatar_path",
    "views.directory",
    "views.dossier_path",
    "views.linkedin_review",
    "views.person_detail",
    "views.retarget_snapshot",
    "views.workflow_state",
    "views.worth_review",
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

    def test_db_public_surface_is_exact_and_at_most_twenty_operations(self) -> None:
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

        self.assertLessEqual(len(operations), 20)
        self.assertEqual(operations, EXPECTED_DB_OPERATIONS)


if __name__ == "__main__":
    unittest.main()
