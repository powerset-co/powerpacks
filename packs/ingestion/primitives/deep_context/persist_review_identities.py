"""Persist approved Deep Context identities into the shared directory.

Runs after LinkedIn review and before fan-in/indexing.  It turns approved real
LinkedIn decisions into durable email/phone -> LinkedIn mappings in
``directory.csv``.  Future source imports and the immediate fan-in can then
resolve the same contact without depending on transient review artifacts.

Flow:
  review.csv + consolidated contacts + hydrated retarget contacts
    -> directory.csv
    -> fan-in -> merged/people.csv

Detached and synthetic identities are intentionally excluded: neither is a
confirmed real-LinkedIn identity to cache in the shared directory.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.contact_fields import emails_from_row, phones_from_row
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.common import (
    CONSOLIDATE_PEOPLE_CSV,
    DEFAULT_PEOPLE_CSV,
    LINKEDIN_OVERRIDES_CSV,
    RETARGET_PEOPLE_CSV,
    emit,
)
from packs.ingestion.primitives.imports.directory import (
    commit_directory_rows,
    directory_identity_key,
)
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url
from packs.shared.csv_io import CsvIO

DIRECTORY_SOURCE = "deep_context_review"
APPROVED = {"auto", "yes"}


def _rows(path: Path) -> list[dict[str, str]]:
    return CsvIO.read_dict_rows(path) if path.exists() else []


def _people_by_id(path: Path) -> dict[str, dict[str, str]]:
    return {str(row.get("id") or ""): row for row in _rows(path) if row.get("id")}


def _pipe_values(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split("|") if part.strip()]


def _identity_rows(
    *,
    emails: list[str],
    phones: list[str],
    name: str,
    linkedin_url: str,
    person_id: str,
    reason: str,
    source_artifact: str,
) -> list[dict[str, str]]:
    url = normalize_linkedin_url(linkedin_url)
    public_identifier = extract_public_identifier(url)
    if not public_identifier:
        return []
    evidence = json.dumps({
        "decision": "approved_deep_context_identity",
        "person_id": person_id,
        "public_identifier": public_identifier,
    }, sort_keys=True)
    rows: list[dict[str, str]] = []
    for email in dict.fromkeys(emails):
        rows.append({
            "source": DIRECTORY_SOURCE,
            "source_key": directory_identity_key(email, "", name, public_identifier),
            "source_id": person_id,
            "source_channels": "deep_context",
            "status": "found",
            "email": email,
            "phone": "",
            "name": name,
            "linkedin_url": url,
            "public_identifier": public_identifier,
            "confidence": "1.00",
            "matched_name": name,
            "evidence": evidence,
            "reasoning": reason,
            "source_artifact": source_artifact,
            "updated_at": now_iso(),
            "_priority": "100",
        })
    for phone in dict.fromkeys(phones):
        rows.append({
            "source": DIRECTORY_SOURCE,
            "source_key": directory_identity_key("", phone, name, public_identifier),
            "source_id": person_id,
            "source_channels": "deep_context",
            "status": "found",
            "email": "",
            "phone": phone,
            "name": name,
            "linkedin_url": url,
            "public_identifier": public_identifier,
            "confidence": "1.00",
            "matched_name": name,
            "evidence": evidence,
            "reasoning": reason,
            "source_artifact": source_artifact,
            "updated_at": now_iso(),
            "_priority": "100",
        })
    return rows


def rows_from_review(review_csv: Path, people_csv: Path) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Approved verify/retarget rows, anchored by their current person contact fields."""
    people = _people_by_id(people_csv)
    rows: list[dict[str, str]] = []
    stats = {"review_considered": 0, "review_persisted": 0, "review_unanchored": 0}
    for decision in _rows(review_csv):
        action = str(decision.get("action") or "").strip().lower()
        if action not in {"verify", "retarget"} or str(decision.get("approved") or "").strip().lower() not in APPROVED:
            continue
        stats["review_considered"] += 1
        url = decision.get("new_linkedin_url") if action == "retarget" else decision.get("linkedin_url")
        person_id = str(decision.get("person_id") or "")
        person = people.get(person_id, {})
        emails = emails_from_row(person) + _pipe_values(decision.get("match_emails") or "")
        phones = phones_from_row(person) + _pipe_values(decision.get("match_phones") or "")
        materialized = _identity_rows(
            emails=emails, phones=phones,
            name=str(person.get("full_name") or decision.get("matched_name") or ""),
            linkedin_url=str(url or ""), person_id=person_id,
            reason="Approved Deep Context identity review",
            source_artifact=str(review_csv),
        )
        if materialized:
            stats["review_persisted"] += 1
            rows.extend(materialized)
        else:
            stats["review_unanchored"] += 1
    return rows, stats


def rows_from_people_artifact(path: Path, *, reason: str) -> tuple[list[dict[str, str]], int]:
    """Persist every real-LinkedIn row from a reviewed contact-carry artifact."""
    rows: list[dict[str, str]] = []
    people = 0
    for person in _rows(path):
        materialized = _identity_rows(
            emails=emails_from_row(person), phones=phones_from_row(person),
            name=str(person.get("full_name") or ""),
            linkedin_url=str(person.get("linkedin_url") or ""),
            person_id=str(person.get("id") or ""), reason=reason, source_artifact=str(path),
        )
        if materialized:
            people += 1
            rows.extend(materialized)
    return rows, people


def run(args: argparse.Namespace) -> dict[str, Any]:
    review_rows, review_stats = rows_from_review(Path(args.review_csv), Path(args.people_csv))
    consolidated, consolidation_people = rows_from_people_artifact(
        Path(args.consolidate_people_csv), reason="Approved Deep Context consolidation",
    )
    retargeted, retarget_people = rows_from_people_artifact(
        Path(args.retarget_people_csv), reason="Approved Deep Context retarget",
    )
    rows = review_rows + consolidated + retargeted
    payload = {
        "source": "persist_review_identities",
        "status": "dry_run" if args.dry_run else "completed",
        **review_stats,
        "consolidated_people": consolidation_people,
        "retarget_people": retarget_people,
        "identity_rows": len(rows),
    }
    if args.dry_run:
        payload["directory_csv"] = str(args.directory_csv)
        return payload
    payload.update(commit_directory_rows(Path(args.directory_csv), rows))
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Persist approved Deep Context LinkedIn identities to directory.csv")
    parser.add_argument("--review-csv", default=str(LINKEDIN_OVERRIDES_CSV))
    parser.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    parser.add_argument("--consolidate-people-csv", default=str(CONSOLIDATE_PEOPLE_CSV))
    parser.add_argument("--retarget-people-csv", default=str(RETARGET_PEOPLE_CSV))
    parser.add_argument("--directory-csv", default=".powerpacks/network-import/directory.csv")
    parser.add_argument("--dry-run", action="store_true", help="Report mappings without writing directory.csv")
    return parser


def main() -> int:
    emit(run(build_parser().parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
