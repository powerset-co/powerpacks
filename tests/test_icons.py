"""The source glyphs the People page (React) and the search page (Python) share stay one set."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANNEL_ICONS = ROOT / "web" / "src" / "components" / "shared" / "icons" / "channels.tsx"
RENDERING = ROOT / "packs" / "search" / "primitives" / "deep_search" / "results_web" / "rendering.py"

# React component -> rendering.py _SOURCE_ICONS key. WhatsApp has no Python glyph: the search
# page folds it into "messages".
SHARED = {"GmailIcon": "gmail", "IMessageIcon": "messages", "LinkedInIcon": "linkedin"}


def _react_markup(component: str) -> str:
    """The children of the component's <svg>, with JSX's `<x />` written as `<x/>`."""
    source = CHANNEL_ICONS.read_text(encoding="utf-8")
    body = re.search(rf"function {component}\(.*?<svg[^>]*>(.*?)</svg>", source, re.S)
    assert body is not None, component
    return "".join(line.strip() for line in body.group(1).splitlines()).replace(" />", "/>")


def _python_markup(key: str) -> str:
    icons = re.search(r"^_SOURCE_ICONS = \{\n(.*?)^\}", RENDERING.read_text(encoding="utf-8"), re.S | re.M)
    assert icons is not None
    entry = re.search(rf"^\s+'{key}': '(.*?)',$", icons.group(1), re.M)
    assert entry is not None, key
    return entry.group(1)


class SharedGlyphTests(unittest.TestCase):
    def test_react_and_python_draw_the_same_glyphs(self) -> None:
        for component, key in SHARED.items():
            with self.subTest(component=component):
                self.assertEqual(_react_markup(component), _python_markup(key))


if __name__ == "__main__":
    unittest.main()
