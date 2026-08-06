"""Boundary parsers for the export batons and sidecar inputs.

The ONLY module that reads or writes the stage's CSV/JSON file formats:
review.csv + synthetic-people.csv (export batons other stages read) and
index.json (parent/child sidecar, until build_parents writes the db
directly). Tolerance for file looseness lives here and nowhere else;
everything past these functions is typed rows and queries.
"""
from __future__ import annotations

import csv
import os
from pathlib import Path

OVERRIDE_COLUMNS = [
    "public_identifier",
    "worth_person_ids",
    "action",
    "approved",
    "new_linkedin_url",
    "new_public_identifier",
    "linkedin_url",
    "match_emails",
    "match_phones",
    "confidence",
    "reason",
    "person_id",
    "source",
    "updated_at",
    "llm_reject",
    "llm_reject_confidence",
    "llm_reject_reason",
    "llm_judge_fingerprint",
    "llm_worth",
    "llm_worth_reason",
    "network_worth",
    "user_worth_note",
]


def load_override_rows(path: Path) -> dict[str, dict[str, str]]:
    """review.csv rows keyed by normalized public_identifier."""
    rows: dict[str, dict[str, str]] = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                key = (row.get("public_identifier") or "").strip().lower()
                if key:
                    rows[key] = row
    return rows


def write_override_rows(path: Path, rows: dict[str, dict[str, str]]) -> None:
    """Atomic: a crash mid-write must never leave a truncated baton."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=OVERRIDE_COLUMNS)
        writer.writeheader()
        for key in sorted(rows):
            writer.writerow({column: rows[key].get(column, "") for column in OVERRIDE_COLUMNS})
    os.replace(tmp, path)


def load_synthetic_rows(path: Path | None) -> list[dict[str, str]]:
    """Legacy input parser. Canonical runtime code does not call this."""
    if path is None or not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_synthetic_rows(path: Path, rows: list[dict[str, object]]) -> None:
    """Write the complete canonical synthetic projection, not gate patches."""
    fieldnames: list[str] = []
    for row in rows:
        for name in row:
            if name not in fieldnames:
                fieldnames.append(name)
    if not fieldnames:
        fieldnames = ["public_identifier", "linkedin_url", "approved"]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows({name: row.get(name, "") for name in fieldnames} for row in rows)
    os.replace(tmp, path)
