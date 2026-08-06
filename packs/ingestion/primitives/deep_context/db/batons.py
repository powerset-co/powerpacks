"""Write-only human-readable CSV exports rendered from canonical SQLite."""
from __future__ import annotations

import csv
import os
from dataclasses import fields
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.models import ReviewExportRow


OVERRIDE_COLUMNS = [field.name for field in fields(ReviewExportRow) if field.name != "key"]


def _atomic_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def _write_override_rows(path: Path, rows: dict[str, dict[str, str]]) -> None:
    """Atomic: a crash mid-write must never leave a truncated baton."""
    _atomic_csv(path, OVERRIDE_COLUMNS, [
        {column: rows[key].get(column, "") for column in OVERRIDE_COLUMNS}
        for key in sorted(rows)
    ])


def _write_synthetic_rows(path: Path, rows: list[dict[str, object]]) -> None:
    """Write the complete canonical synthetic projection, not gate patches."""
    fieldnames: list[str] = []
    for row in rows:
        for name in row:
            if name not in fieldnames:
                fieldnames.append(name)
    if not fieldnames:
        fieldnames = ["public_identifier", "linkedin_url", "approved"]
    _atomic_csv(path, fieldnames, [
        {name: row.get(name, "") for name in fieldnames} for row in rows
    ])
