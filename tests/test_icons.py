"""The glyphs the React app and the search page (Python) share stay one set: the source
pills and the plus, pin and flag actions."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ICONS = ROOT / "web" / "src" / "components" / "shared" / "icons"
CHANNEL_ICONS = ICONS / "channels.tsx"
ACTION_ICONS = ICONS / "actions.tsx"
RENDERING = ROOT / "packs" / "search" / "primitives" / "deep_search" / "results_web" / "rendering.py"

# React component -> rendering.py _SOURCE_ICONS key. WhatsApp has no Python glyph: the search
# page folds it into "messages".
SHARED = {"GmailIcon": "gmail", "IMessageIcon": "messages", "LinkedInIcon": "linkedin",
          "XIcon": "x", "ContactsExportIcon": "csv_import"}
# React component -> rendering.py module constant.
ACTIONS = {"PlusIcon": "PLUS_SVG", "PinIcon": "PIN_SVG", "FlagIcon": "FLAG_SVG"}


def _react_markup(component: str, path: Path = CHANNEL_ICONS) -> str:
    """The children of the component's <svg>, with JSX's `<x />` written as `<x/>`."""
    source = path.read_text(encoding="utf-8")
    body = re.search(rf"function {component}\(.*?<svg[^>]*>(.*?)</svg>", source, re.S)
    assert body is not None, component
    return "".join(line.strip() for line in body.group(1).splitlines()).replace(" />", "/>")


def _python_markup(key: str) -> str:
    icons = re.search(r"^_SOURCE_ICONS = \{\n(.*?)^\}", RENDERING.read_text(encoding="utf-8"), re.S | re.M)
    assert icons is not None
    entry = re.search(rf"^\s+'{key}': '(.*?)',$", icons.group(1), re.M)
    assert entry is not None, key
    return entry.group(1)


def _python_constant(name: str) -> str:
    """The children of a rendering.py <svg> constant, in the double quotes JSX writes."""
    source = RENDERING.read_text(encoding="utf-8")
    value = re.search(rf"^{name} = \((.*?)\)$", source, re.S | re.M)
    assert value is not None, name
    markup = "".join(re.findall(r'"(.*?)"', value.group(1)))
    body = re.search(r"<svg[^>]*>(.*)</svg>", markup)
    assert body is not None, name
    return body.group(1).replace("'", '"')


class SharedGlyphTests(unittest.TestCase):
    def test_react_and_python_draw_the_same_glyphs(self) -> None:
        for component, key in SHARED.items():
            with self.subTest(component=component):
                self.assertEqual(_react_markup(component), _python_markup(key))

    def test_the_action_glyphs_match_the_search_page(self) -> None:
        for component, name in ACTIONS.items():
            with self.subTest(component=component):
                self.assertEqual(_react_markup(component, ACTION_ICONS), _python_constant(name))


if __name__ == "__main__":
    unittest.main()
