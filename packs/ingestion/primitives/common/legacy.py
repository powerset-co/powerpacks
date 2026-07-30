#!/usr/bin/env python3
"""Cope-with-old-installs scrubs — the ONE module allowed to know legacy shapes.

Each stage calls its scrub as the first line of `execute()`; everything after
that call may assume current shapes. No other module may read or write a legacy
artifact — that prohibition is what entitles the stage's boundary parsers to be
strict.

Every entry is dated and carries a removal condition: a legacy scrub is a
countdown, not a fixture. When the condition is met, delete the line. All
scrubs are idempotent and cheap — a no-op on a current install, safe to run
every time.

Changelog:
  2026-07-28 (created): collected the gmail import's inline legacy unlinks
    (`ledger.json`, `candidates.csv`) into the one quarantine module.
  2026-07-30: messages section — pre-interaction-counts people.csv probe and
    the retired `import/messages/` artifacts scrub.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from packs.shared.csv_io import CsvIO


def scrub_gmail_import(import_dir: Path) -> None:
    """Upgrade an old install's gmail import dir in place.

    2026-07-23 ledger era — remove once no install predates powerpacks-v1.0.0.
    2026-07-25 candidates.csv fold-in (#339) — remove once no install predates
    powerpacks-v1.2.1; the candidate pool merges into people.csv now, so the
    file has no writer and a stale copy would shadow the folded rows.
    """
    (import_dir / "ledger.json").unlink(missing_ok=True)
    (import_dir / "candidates.csv").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------

# Files and directories in `import/messages/` that older Powerpacks versions
# wrote and nothing reads today. Each entry carries the version that orphaned it
# and the condition for deleting the entry from this list.
#
#   people.input.csv  2026-07-23 — the review-era import input. Its producer went
#                     with the in-import research/review flow retired in #315.
#                     DELETE this entry once no supported install can predate
#                     #315 (i.e. once a fresh-install-only floor is declared).
#   enrichment/       2026-07-23 — the review-era per-run enrichment scratch dir,
#                     retired with the same flow. Same removal condition.
#   candidates.csv    2026-07-26 — the separate research-candidate pool. #339
#                     folded candidates into `people.csv`, leaving this file with
#                     a reader and no writer. DELETE this entry once no supported
#                     install can predate #339.
MESSAGES_RETIRED_IMPORT_ARTIFACTS = (
    "people.input.csv",
    "enrichment",
    "candidates.csv",
)


def scrub_messages_import_dir(import_dir: Path) -> None:
    """Delete the retired `import/messages/` artifacts listed above.

    Called from the messages import's MATERIALIZE path, not from its stage entry
    — the documented exception to this file's stage-entry rule. The no-op gate
    promises that a current run writes nothing; scrubbing at stage entry would
    make a "nothing to do" run mutate the import dir anyway. Materialize is the
    first point at which the stage is already rewriting this directory.
    """
    for name in MESSAGES_RETIRED_IMPORT_ARTIFACTS:
        target = import_dir / name
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)


def messages_people_csv_predates_interaction_counts(path: Path) -> bool:
    """True when an existing `import/messages/people.csv` was written before the
    interaction-count columns existed (2026-07-23).

    The messages import's fingerprint no-op cannot catch this: the CODE changed,
    not the input data, so the fingerprints still match and the stage would keep
    serving a people.csv missing `interaction_counts`. The import calls this
    first in `execute()` and self-invalidates instead of trusting its manifest.

    DELETE this entry once no supported install can carry a people.csv written
    before that column landed.
    """
    if not path.exists():
        return False
    with path.open(newline="", encoding="utf-8") as handle:
        header = next(CsvIO.reader(handle), [])
    return bool(header) and "interaction_counts" not in header
