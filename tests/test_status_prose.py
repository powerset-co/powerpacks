"""The status page script and the code that plays it stay in step.

Every event the installer writes is in status_prose.PROSE, every event there is
written somewhere, and every line and note renders and fits the lines the page holds.
"""
from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path

from packs.powerset.primitives.install.prose_cli import _RUN, _SAMPLE, _entry
from packs.powerset.primitives.install.status_prose import PAGE, PROSE, ROWS, page_prose, render
from packs.powerset.primitives.install.steps import InstallStep

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "packs/powerset/primitives/install"
# The Python files that write status, and the shell scripts that do.
WRITERS = [path for path in INSTALL.glob("*.py") if path.name not in {"status_prose.py", "prose_cli.py"}]
SCRIPTS = [ROOT / "bin/bootstrap", ROOT / "bin/setup-python"]
# The desktop app records the install and setup's readiness itself: its quoted event names count.
DESKTOP_WRITER = ROOT / "desktop/src-tauri/src/onboard.rs"
GMAIL_SCRIPT = ROOT / "packs/ingestion/primitives/setup/automations/google_oauth_browser.js"
LINKEDIN_READER = ROOT / "packs/ingestion/primitives/discover/linkedin/connections.py"
AREAS = {event.split(".")[0] for event in PROSE}
NOT_EVENTS = {"log", "json", "csv", "py", "sh", "tmp", "lock", "sqlite", "md", "png", "html"}
# The page holds 3 lines for the status line and 4 for the note on a wide screen
# (InstallPage.tsx MESSAGE_LINES / NOTE_LINES); these keep every one inside them.
LINE_BUDGET, NOTE_BUDGET = 150, 220


def _looks_like_event(text: str) -> bool:
    return (bool(re.fullmatch(r"[a-z_]+(\.[a-z_]+)+", text)) and text.split(".")[0] in AREAS
            and text.split(".")[-1] not in NOT_EVENTS)


def _python_events(path: Path) -> tuple[set[str], set[str]]:
    """(event literals, f-string event prefixes) in one Python file."""
    literals, prefixes = set(), set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _looks_like_event(node.value):
            literals.add(node.value)
        if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
            head = node.values[0].value
            if head.endswith(".") and _looks_like_event(head[:-1] + ".x"):
                prefixes.add(head)
    return literals, prefixes


def _written() -> tuple[set[str], set[str]]:
    literals, prefixes = set(), set()
    for path in WRITERS:
        found, heads = _python_events(path)
        literals |= found
        prefixes |= heads
    for path in SCRIPTS:
        literals |= {token for token in re.findall(r"[a-z_]+(?:\.[a-z_]+)+", path.read_text())
                     if _looks_like_event(token)}
    literals |= {token for token in re.findall(r'"([a-z_]+(?:\.[a-z_]+)+)"', DESKTOP_WRITER.read_text())
                 if _looks_like_event(token)}
    return literals, prefixes


class StatusProseTests(unittest.TestCase):
    def test_every_event_the_installer_writes_is_in_the_script(self) -> None:
        literals, prefixes = _written()
        self.assertEqual(sorted(literals - set(PROSE)), [])
        for prefix in prefixes:
            self.assertTrue(any(event.startswith(prefix) for event in PROSE), prefix)

    def test_every_event_in_the_script_is_written_somewhere(self) -> None:
        literals, prefixes = _written()
        unused = [event for event in PROSE
                  if event not in literals and not any(event.startswith(prefix) for prefix in prefixes)]
        self.assertEqual(unused, [])

    def test_the_gmail_automation_stages_and_linkedin_outcomes_are_in_the_script(self) -> None:
        for stage in re.findall(r'progress\("([a-z_]+)"\)', GMAIL_SCRIPT.read_text()):
            self.assertIn(f"gmail.app.{stage}", PROSE)
        reader = LINKEDIN_READER.read_text()
        for outcome in ("read", "partial", "limit", "stalled", "export_requested", "export_imported"):
            self.assertIn(f'"{outcome}"', reader)
            self.assertIn(f"linkedin.done.{outcome}", PROSE)

    def test_every_line_and_note_renders_and_fits_the_page(self) -> None:
        for event in PROSE:
            with self.subTest(event=event):
                _, line, note = render(event, _SAMPLE)
                self.assertNotIn("{", line + note)
                self.assertLessEqual(len(line), LINE_BUDGET)
                self.assertLessEqual(len(note), NOTE_BUDGET)

    def test_the_demo_run_plays_only_scripted_events(self) -> None:
        for item in _RUN:
            self.assertIn(_entry(item)[0], PROSE)

    def test_every_step_but_choosing_sources_has_one_row(self) -> None:
        rows = [step for row in ROWS for step in row.steps]
        self.assertEqual(sorted(rows), sorted(step for step in InstallStep if step is not InstallStep.SOURCES))

    def test_the_page_test_fixture_is_this_script(self) -> None:
        fixture = ROOT / "web/src/pages/install/prose.fixture.json"
        self.assertEqual(json.loads(fixture.read_text()), page_prose(),
                         "regenerate web/src/pages/install/prose.fixture.json from status_prose.page_prose()")

    def test_the_page_words_are_complete(self) -> None:
        for state in ("running", "waiting", "failed", "completed", "skipped", "paused", "next"):
            self.assertIn(f"state.{state}", PAGE)


if __name__ == "__main__":
    unittest.main()
