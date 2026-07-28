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
"""

from __future__ import annotations

from pathlib import Path


def scrub_gmail_import(import_dir: Path) -> None:
    """Upgrade an old install's gmail import dir in place.

    2026-07-23 ledger era — remove once no install predates powerpacks-v1.0.0.
    2026-07-25 candidates.csv fold-in (#339) — remove once no install predates
    powerpacks-v1.2.1; the candidate pool merges into people.csv now, so the
    file has no writer and a stale copy would shadow the folded rows.
    """
    (import_dir / "ledger.json").unlink(missing_ok=True)
    (import_dir / "candidates.csv").unlink(missing_ok=True)
