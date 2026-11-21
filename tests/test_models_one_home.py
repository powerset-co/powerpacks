"""A model name is written in packs/shared/models.py and nowhere else in the product code."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL_NAME = re.compile(r"""["'](gpt-\d[\w.-]*|text-embedding-[\w-]+|claude-(opus|sonnet|haiku|fable)[\w.-]*)["']""")
# Reads of what a model supports ("gpt-5" prefixes), price tables and evals are not defaults.
ALLOWED = ("usage_pricing.py", "/evals/", "packs/shared/models.py")
PRICE_ROW = re.compile(r"""^\s*["'][\w.-]+["']\s*:\s*(\{\s*["']input["']|[\d.]+,)""")
SUPPORT_CHECK = re.compile(r"startswith\(|\bin model\.lower\(\)")


class ModelsOneHomeTests(unittest.TestCase):
    def test_product_code_names_no_model(self) -> None:
        found = []
        for folder in ("packs",):
            for path in (ROOT / folder).rglob("*.py"):
                relative = str(path.relative_to(ROOT))
                if any(part in relative for part in ALLOWED) or "/tests/" in relative:
                    continue
                for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    if MODEL_NAME.search(line) and not SUPPORT_CHECK.search(line) and not PRICE_ROW.search(line):
                        found.append(f"{relative}:{number}")
        self.assertEqual(found, [], "write the default in packs/shared/models.py and import it")


if __name__ == "__main__":
    unittest.main()
