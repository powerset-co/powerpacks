"""Boundary parsers for the export batons and sidecar inputs.

The ONLY module that reads or writes the stage's CSV/JSON file formats:
review.csv + synthetic-people.csv (export batons other stages read) and
index.json (parent/child sidecar, until build_parents writes the db
directly). Tolerance for file looseness lives here and nowhere else;
everything past these functions is typed rows and queries.
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.schema import (
    DecisionKind,
    DecisionRow,
    HumanWorth,
)

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


def read_synthetic_gates(synthetic_csv: Path | None) -> tuple[list[DecisionRow], list[str]]:
    """The approved gate column of synthetic-people.csv as decision rows.

    The CSV cell conflates outcome and actor: yes/no are human, 'auto' is the
    completeness gate's machine-standing keep — split into (value, approved).
    """
    by_pub: dict[str, DecisionRow] = {}
    errors: list[str] = []
    if not synthetic_csv or not synthetic_csv.exists():
        return [], errors
    with synthetic_csv.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            pub = (row.get("public_identifier") or "").strip().lower()
            approved = (row.get("approved") or "").strip().lower()
            if not approved:
                continue
            if not pub:
                errors.append("synthetic-people.csv: approved gate on a row without public_identifier")
                continue
            if approved == "auto":
                gate = DecisionRow(kind=DecisionKind.SYNTHETIC_GATE.value, target=pub,
                                   value=HumanWorth.YES.value, approved="auto")
            elif approved in set(HumanWorth):
                gate = DecisionRow(kind=DecisionKind.SYNTHETIC_GATE.value, target=pub,
                                   value=approved, approved="yes")
            else:
                errors.append(f"synthetic:{pub}: unknown approved '{approved}'")
                continue
            previous = by_pub.get(pub)
            if previous is not None and previous != gate:
                errors.append(f"synthetic:{pub}: duplicate rows with conflicting approved gates")
                continue
            by_pub[pub] = gate
    return list(by_pub.values()), errors


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


def people_from_index(index_json: Path) -> list[dict[str, str]]:
    """The parent/child relation from index.json: one row per current child
    person id, pointing at its canonical parent."""
    try:
        index = json.loads(index_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    slugs = index.get("slugs") or {}
    out: dict[str, dict[str, str]] = {}
    for parent_slug, parent in (index.get("parents") or {}).items():
        parent_id = str(parent.get("parent_id") or "").strip().lower()
        if not parent_id:
            continue
        for child_slug in parent.get("children") or []:
            person_id = str((slugs.get(child_slug) or {}).get("person_id") or "").strip().lower()
            if person_id:
                out[person_id] = {
                    "person_id": person_id, "parent_id": parent_id,
                    "child_slug": str(child_slug), "parent_slug": str(parent_slug),
                }
    return list(out.values())
