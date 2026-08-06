"""Keep Deep Context dependencies visible and importable at module load."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


DEEP_CONTEXT = Path("packs/ingestion/primitives/deep_context")


class DeepContextImportHygieneTests(unittest.TestCase):
    def test_deep_context_has_no_nested_imports(self) -> None:
        nested: list[str] = []
        for path in sorted(DEEP_CONTEXT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)) and node.col_offset:
                    nested.append(f"{path}:{node.lineno}")

        self.assertEqual(nested, [])


if __name__ == "__main__":
    unittest.main()
