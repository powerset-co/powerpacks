from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SearchAdapterSurfaceTests(unittest.TestCase):
    def test_host_adapters_retire_search_company(self) -> None:
        for relative in (
            "adapters/claude-code/install.sh",
            "adapters/codex/install.sh",
            "adapters/pi/install.sh",
        ):
            with self.subTest(adapter=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                retired = re.search(r"RETIRED_SKILLS=\(\n(?P<body>.*?)\n\)", text, re.DOTALL)
                self.assertIsNotNone(retired)
                self.assertIn("search-company", retired.group("body").split())
                self.assertNotIn("install_skill search-company ", text)
                self.assertNotRegex(text, r"installed Powerpacks skills[^\n]*search-company")

                managed = re.search(r"MANAGED_SKILLS=\(\n(?P<body>.*?)\n\)", text, re.DOTALL)
                if managed is not None:
                    self.assertNotIn("search-company", managed.group("body").split())

    def test_nanoclaw_scrubs_retired_and_legacy_skill_directories(self) -> None:
        text = (ROOT / "adapters/nanoclaw/install.sh").read_text(encoding="utf-8")
        for root in (".claude/skills", "container/skills"):
            self.assertIn(f'$TARGET/{root}/search-company', text)
            self.assertIn(f'$TARGET/{root}/search-network', text)


if __name__ == "__main__":
    unittest.main()
