"""A model name is written in packs/shared/models.py and nowhere else in the search code."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL_NAME = re.compile(r"""["'](gpt-\d[\w.-]*|text-embedding-[\w-]+)["']""")
# Reads of what a model supports ("gpt-5" prefixes), prices and evals are not defaults.
ALLOWED = ("usage_pricing.py", "/evals/")
SUPPORT_CHECK = re.compile(r"startswith\(|\bin model\.lower\(\)")


class ModelsOneHomeTests(unittest.TestCase):
    def test_search_code_names_no_model(self) -> None:
        found = []
        for folder in ("packs/search", "packs/sales-nav"):
            for path in (ROOT / folder).rglob("*.py"):
                relative = str(path.relative_to(ROOT))
                if any(part in relative for part in ALLOWED) or "/tests/" in relative:
                    continue
                for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    if MODEL_NAME.search(line) and not SUPPORT_CHECK.search(line):
                        found.append(f"{relative}:{number}")
        self.assertEqual(found, [], "write the default in packs/shared/models.py and import it")


if __name__ == "__main__":
    unittest.main()
