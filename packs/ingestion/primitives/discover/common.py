#!/usr/bin/env python3
"""Discover-stage helpers: CSV I/O, the `source_slug` helper, and stage manifests.

Holds only the things unique to the discover stage — the fingerprinted LF CSV
reader/writer, the `ordered_unique`/`source_slug` helpers. The typed
`StagePayload` + `write_stage_manifest` manifest contract now lives in
`packs.ingestion.primitives.common.manifests` (re-exported here for callers that
still reach for it via this module); the cross-vertical json/proc/paths/
contact-field helpers live in `packs.ingestion.primitives.common`.

Changelog:
  2026-07-23 (dead accounts.json registry): deleted the last account-state
    accessors — ``read_accounts``/``account_channel``/``account_config``. The
    `accounts.json` registry lost its only writer, and grep proved zero live
    callers of these readers, so they went with it (the `read_json` import went
    too).
  2026-07-23 (messages explicit-selection): deleted ``channel_is_linked`` — its
    sole caller was messages discovery's accounts.json linkage read, which was
    removed when message channel selection became explicit ``--include-*`` only.
  2026-07-23 (audit class-sharing): moved the typed-manifest contract
    (StagePayload, write_stage_manifest, and the collect/artifact/manifest
    fingerprint helpers) to common/manifests.py so non-discover stages can share
    it; kept a StagePayload + write_stage_manifest re-export here for existing
    callers. read_csv_rows / write_csv_rows / account state / source_slug stay.
  2026-07-23 (audit batch 16): deleted the orchestrator-only ledger/step
    machinery after `discover.py` was removed; renamed the progress prefix from
    [discover-contacts] to [discover].
  2026-07-23 (audit consolidation): moved the cross-vertical helpers out — now_iso
    / emit / read_json / write_json / parse_last_json / unique_strings /
    sha256_file to common.jsonio, run_cmd / py_cmd / emit_progress to common.proc,
    DEFAULT_BASE_DIR / DEFAULT_DIRECTORY_CSV / DEFAULT_MSGVAULT_DB to common.paths,
    and deleted the local parse_jsonish (callers import it from
    schemas/people_schema). This module keeps only discover-stage code.
"""

from __future__ import annotations

import csv
import io
import re
import sys
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Re-export the typed-manifest contract from its shared home so callers that still
# import StagePayload / write_stage_manifest from discover.common keep working.
from packs.ingestion.primitives.common.manifests import StagePayload, write_stage_manifest  # noqa: E402,F401
from packs.shared.csv_io import CsvIO  # noqa: E402

GMAIL_INTERACTION_CALCULATION_VERSION = "msgvault-interactions-v2"


def read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read a CSV into `(fieldnames, rows)`, tolerating BOMs and bad bytes."""
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        reader = CsvIO.dict_reader(handle)
        fields = list(reader.fieldnames or [])
        rows = [{str(key): value or "" for key, value in row.items() if key is not None} for row in reader]
    return fields, rows


def write_csv_rows(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    """Write rows with LF line endings, skipping the write when bytes are unchanged."""
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, "") for field in fieldnames})
    content = buffer.getvalue().encode("utf-8")
    if path.exists():
        try:
            if path.read_bytes() == content:
                return
        except OSError:
            pass
    path.write_bytes(content)


def ordered_unique(values: list[Any]) -> list[str]:
    """Order-preserving de-dup of a list into stripped, non-empty strings."""
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def source_slug(value: str) -> str:
    """Filesystem-safe slug for a source label (`source` when empty)."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (value or "").strip().lower()).strip("-._")
    return slug or "source"
