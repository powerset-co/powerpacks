"""Load and render the install status words from status.json.

An event names one moment of a run. `InstallStatus.write(event, ...)` looks it
up in status.json, fills its {placeholders}, and writes the line, the note and the state
to the install manifest, which the page, the agent and the CLI read. Code never
words the page itself: it names an event, and a primitive's own text goes to the
manifest's details and the log, never to the page.

Read status.json top to bottom as the script; `bin/status-prose` prints it and
`bin/status-prose play` plays it on the status page.

Not worded in status.json:
  - what the page says while it cannot reach setup (`OFFLINE` in InstallPage.tsx);
  - the live enrichment line ("Looking up 3 people"), the review page's own copy
    (`web/src/pages/review/enrich/copy.ts`);
  - the live index line ("Building search records"), sent by the Modal run.

Changelog:
  2026-10-29: every status word moved to status.json, shared by Python and the web.
  2026-10-27: `tools.browser`: the tools step downloads Powerpacks' own browser.
  2026-10-13: under the desktop app, setup never asks which Gmail account (the Connect screen
      connects it) and WhatsApp has no skip: their words are gone.
  2026-10-13: `sources.connected` ends a Connect screen run (workflow.py `connect`); the page
      counts its tasks ("3 of 7 tasks completed") where the ring was.
  2026-10-09: under the desktop app (POWERPACKS_DESKTOP=1) no agent reads the page, so the lines
      that speak as one get the app's words (DESKTOP_LINES, DESKTOP_NOTES, DESKTOP_PAGE); the
      paid stages report their counts as they go; the Skip WhatsApp and elapsed-time page words.
  2026-10-07: the events of v1's processing, the spend approval, the index recovery and the
      review row are gone with them.
  2026-10-05: created; the copy moved here from status.py, workflow.py,
      pipeline.py, onboard.py, bin/bootstrap, the LinkedIn reader and the page.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from packs.powerset.primitives.install.steps import InstallState, InstallStep

# Set by the desktop app: setup's waits get buttons on the page, not an agent in chat.
DESKTOP = os.environ.get("POWERPACKS_DESKTOP") == "1"


@dataclass(frozen=True)
class Prose:
    """One event: the step it belongs to (None: the step the run is on), the state it
    puts that step in, the line under the page title, the note under the line, and
    what the page offers for it (`action`: qr, permission, gmail, review, ...)."""
    step: InstallStep | None
    state: InstallState
    line: str
    note: str = ""
    action: str = ""


@dataclass(frozen=True)
class Row:
    """One row of the page's step list and the steps it covers."""
    label: str
    steps: tuple[InstallStep, ...]
    needs: InstallStep | None = None  # shown only when this step is planned


_STATUS = json.loads(Path(__file__).with_name("status.json").read_text(encoding="utf-8"))

# The page and Python read the same rows, top to bottom.
ROWS = tuple(
    Row(row["label"], tuple(InstallStep(step) for step in row["steps"]),
        InstallStep(row["needs"]) if row["needs"] else None)
    for row in _STATUS["rows"]
)
PAGE = _STATUS["page"]
STAGE_EVENTS = {stage: tuple(events) for stage, events in _STATUS["stages"].items()}
PROSE: dict[str, Prose] = {
    event: Prose(InstallStep(item["step"]) if item["step"] else None,
                InstallState(item["state"]), item["line"], item.get("note", ""), item.get("action", ""))
    for event, item in _STATUS["events"].items()
}
DESKTOP_PAGE = _STATUS["desktop_page"]
DESKTOP_LINES = _STATUS["desktop_lines"]
DESKTOP_NOTES = _STATUS["desktop_notes"]
_DESKTOP_PHRASES = _STATUS["desktop_phrases"]
_SOURCE_COUNT = _STATUS["source_count"]
_NO_SOURCE_COUNTS = _STATUS["no_source_counts"]


def source_counts(counts: dict[str, int]) -> str:
    """The contacts each source added, for the line once every source is in."""
    return _STATUS["source_count_separator"].join(_SOURCE_COUNT.format(source=source.title(), count=count)
                      for source, count in counts.items()) or _NO_SOURCE_COUNTS


def _app_words(text: str) -> str:
    for phrase, replacement in _DESKTOP_PHRASES.items():
        text = text.replace(phrase, replacement)
    return text


def render(event: str, values: dict, *, desktop: bool = DESKTOP) -> tuple[Prose, str, str]:
    """The event's prose with its line and note filled from `values`; under the desktop app,
    the app's words for the lines that speak as an agent."""
    prose = PROSE[event]
    line, note = prose.line, prose.note
    if desktop:
        line, note = _app_words(DESKTOP_LINES.get(event, line)), _app_words(DESKTOP_NOTES.get(event, note))
    line = "{message}" if values.get("message") else line
    return prose, line.format(**values), note.format(**values)


def page_prose(*, desktop: bool = DESKTOP) -> dict:
    """The page's fixed words, sent with every status read."""
    return {"page": {**PAGE, **DESKTOP_PAGE} if desktop else PAGE}
