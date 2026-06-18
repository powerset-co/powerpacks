#!/usr/bin/env python3
"""Migrate messages import artifacts to the final review CSV schema.

Stdlib-only. This is intentionally mechanical:
- canonicalize review buckets to yes|maybe|no
- join existing Powerset network matches from contacts.csv
- add missing in-network contact rows to the review CSV
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from packs.shared.csv_io import CsvIO
except ModuleNotFoundError:  # pragma: no cover - direct script fallback
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.shared.csv_io import CsvIO


BASE_FIELDS = [
    "bucket",
    "handle",
    "full_name",
    "phone_e164",
    "area_code",
    "total_messages",
    "imessage_message_count",
    "whatsapp_message_count",
    "message_source",
    "last_message",
    "imessage_last_message",
    "whatsapp_last_message",
    "group_names",
    "location_city",
    "location_country",
    "top_titles",
    "top_companies",
    "top_title_company_pairs",
    "schools",
    "short_reason",
    "identity_risk",
    "signals",
    "retarget_hint",
    "exclude",
    "enrich_decision",
    "retarget_status",
    "retarget_handle",
    "retarget_researched_at",
    "retarget_linkedin_url",
    "retarget_name_confidence",
    "retarget_notes",
    "retarget_profile_status",
    "linkedin_url",
]

FINAL_FIELDS = BASE_FIELDS + [
    "in_network",
    "network_match_status",
    "network_person_id",
    "network_name",
    "network_linkedin_url",
    "network_match_confidence",
    "network_match_method",
    "network_match_reason",
    "review_source",
]

BUCKET_ALIASES = {
    "yes": "yes",
    "confident": "yes",
    "maybe": "maybe",
    "medium": "maybe",
    "review": "maybe",
    "no": "no",
}
BUCKET_ORDER = {"yes": 0, "maybe": 1, "no": 2}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def digits_only(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def phone_key(value: str) -> str:
    digits = digits_only(value)
    return digits[-10:] if len(digits) >= 10 else digits


def phone_handle(phone: str, name: str = "") -> str:
    key = phone_key(phone)
    if key:
        return f"phone-{key}"
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in (name or "unknown")).strip("-")
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe or "unknown"


def area_code(phone: str) -> str:
    key = phone_key(phone)
    return key[:3] if len(key) == 10 else ""


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        return [], []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = CsvIO.dict_reader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = [{key: value or "" for key, value in row.items()} for row in reader]
    return fieldnames, rows


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})
    os.replace(tmp, path)


def normalize_bucket(value: str) -> str:
    return BUCKET_ALIASES.get((value or "").strip().lower(), "maybe")


def is_network_match(row: dict[str, str]) -> bool:
    return (
        (row.get("match_status") or "").strip().lower() == "matched"
        and bool((row.get("matched_person_id") or "").strip())
    )


def contact_network_fields(contact: dict[str, str]) -> dict[str, str]:
    matched = is_network_match(contact)
    return {
        "in_network": "true" if matched else "false",
        "network_match_status": (contact.get("match_status") or "").strip(),
        "network_person_id": (contact.get("matched_person_id") or "").strip() if matched else "",
        "network_name": (contact.get("matched_name") or "").strip() if matched else "",
        "network_linkedin_url": (contact.get("matched_linkedin_url") or "").strip() if matched else "",
        "network_match_confidence": (contact.get("match_confidence") or "").strip(),
        "network_match_method": (contact.get("match_method") or "").strip(),
        "network_match_reason": (contact.get("match_reason") or "").strip(),
    }


def canonical_review_row(row: dict[str, str], contact: dict[str, str] | None) -> dict[str, str]:
    out = {field: row.get(field, "") for field in FINAL_FIELDS}
    original_bucket = normalize_bucket(row.get("bucket", ""))
    network = contact_network_fields(contact or {})
    in_network = network["in_network"] == "true"
    out.update(network)
    out["bucket"] = original_bucket
    out["review_source"] = "in_network_match" if in_network else (row.get("review_source") or "llm_network_review")
    if in_network:
        out["full_name"] = out["network_name"] or out["full_name"]
        out["linkedin_url"] = out["network_linkedin_url"] or out.get("linkedin_url", "")
        if not out.get("short_reason"):
            out["short_reason"] = "Matched existing Powerset network contact."
        signals = [part.strip() for part in (out.get("signals") or "").split("|") if part.strip()]
        if "in_network" not in signals:
            signals.insert(0, "in_network")
        out["signals"] = " | ".join(signals)
    return out


def network_contact_row(contact: dict[str, str]) -> dict[str, str]:
    phone = (contact.get("phone") or "").strip()
    name = (contact.get("matched_name") or contact.get("name") or "").strip()
    network = contact_network_fields(contact)
    row = {field: "" for field in FINAL_FIELDS}
    row.update({
        "bucket": "maybe",
        "handle": phone_handle(phone, name),
        "full_name": name,
        "phone_e164": phone,
        "area_code": area_code(phone),
        "total_messages": contact.get("message_count", "") or "0",
        "imessage_message_count": contact.get("imessage_message_count", "") or "",
        "whatsapp_message_count": contact.get("whatsapp_message_count", "") or "",
        "message_source": contact.get("source", "") or "",
        "last_message": contact.get("last_message", "") or "",
        "imessage_last_message": contact.get("imessage_last_message", "") or "",
        "whatsapp_last_message": contact.get("whatsapp_last_message", "") or "",
        "group_names": contact.get("group_names", "") or "",
        "short_reason": "Matched existing Powerset network contact.",
        "signals": "in_network",
        "linkedin_url": contact.get("matched_linkedin_url", "") or "",
        "review_source": "in_network_match",
    })
    row.update(network)
    return row


def sort_key(row: dict[str, str]) -> tuple[int, int, str]:
    try:
        messages = int(row.get("total_messages") or 0)
    except ValueError:
        messages = 0
    network_rank = 0 if row.get("in_network") == "true" else 1
    return (network_rank, BUCKET_ORDER.get(row.get("bucket", ""), 99), -messages, (row.get("full_name") or "").lower())


def migrate(review_csv: Path, contacts_csv: Path, output_csv: Path, *, backup: bool) -> dict[str, Any]:
    review_fields, review_rows = read_csv(review_csv)
    _, contacts = read_csv(contacts_csv)
    if not review_fields:
        raise FileNotFoundError(f"review CSV missing or empty: {review_csv}")
    contact_by_phone = {
        phone_key(row.get("phone", "")): row
        for row in contacts
        if phone_key(row.get("phone", ""))
    }

    rows: list[dict[str, str]] = []
    seen_phone_keys: set[str] = set()
    bucket_conversions: dict[str, int] = {}
    in_network_review_rows = 0
    for row in review_rows:
        key = phone_key(row.get("phone_e164", ""))
        if key:
            seen_phone_keys.add(key)
        old_bucket = (row.get("bucket") or "").strip().lower()
        contact = contact_by_phone.get(key)
        migrated = canonical_review_row(row, contact)
        if migrated["bucket"] != old_bucket:
            bucket_conversions[f"{old_bucket or '<blank>'}->{migrated['bucket']}"] = bucket_conversions.get(f"{old_bucket or '<blank>'}->{migrated['bucket']}", 0) + 1
        if migrated.get("in_network") == "true":
            in_network_review_rows += 1
        rows.append(migrated)

    in_network_added = 0
    for contact in contacts:
        key = phone_key(contact.get("phone", ""))
        if not key or key in seen_phone_keys or not is_network_match(contact):
            continue
        rows.append(network_contact_row(contact))
        seen_phone_keys.add(key)
        in_network_added += 1

    rows.sort(key=sort_key)
    fieldnames = FINAL_FIELDS + [field for field in review_fields if field not in FINAL_FIELDS]
    if backup and output_csv.resolve() == review_csv.resolve() and review_csv.exists():
        backup_path = review_csv.with_suffix(review_csv.suffix + ".pre-final-schema")
        if not backup_path.exists():
            backup_path.write_bytes(review_csv.read_bytes())
    write_csv(output_csv, fieldnames, rows)
    manifest = {
        "primitive": "migrate_review_schema",
        "command": "migrate",
        "status": "ok",
        "created_at": now_iso(),
        "review_csv": str(review_csv),
        "contacts_csv": str(contacts_csv),
        "output_csv": str(output_csv),
        "rows_in": len(review_rows),
        "rows_written": len(rows),
        "in_network_added": in_network_added,
        "in_network_review_rows": in_network_review_rows,
        "research_bucket_counts": {
            bucket: sum(1 for row in rows if row.get("bucket") == bucket and row.get("in_network") != "true")
            for bucket in BUCKET_ORDER
        },
        "tab_counts": {
            "in_network": sum(1 for row in rows if row.get("in_network") == "true"),
            **{
                bucket: sum(1 for row in rows if row.get("bucket") == bucket and row.get("in_network") != "true")
                for bucket in BUCKET_ORDER
            },
        },
        "bucket_conversions": bucket_conversions,
    }
    output_csv.with_suffix(output_csv.suffix + ".manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate messages review artifacts to the final schema")
    sub = parser.add_subparsers(dest="command", required=True)
    migrate_parser = sub.add_parser("migrate", help="Migrate a review CSV in-place by default")
    migrate_parser.add_argument("--artifacts-dir", type=Path, default=Path(".powerpacks/messages"))
    migrate_parser.add_argument("--review-csv", type=Path)
    migrate_parser.add_argument("--contacts-csv", type=Path)
    migrate_parser.add_argument("--output-csv", type=Path)
    migrate_parser.add_argument("--no-backup", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    artifacts_dir = Path(args.artifacts_dir)
    review_csv = Path(args.review_csv) if args.review_csv else artifacts_dir / "research_review.csv"
    contacts_csv = Path(args.contacts_csv) if args.contacts_csv else artifacts_dir / "contacts.csv"
    output_csv = Path(args.output_csv) if args.output_csv else review_csv
    emit(migrate(review_csv, contacts_csv, output_csv, backup=not args.no_backup))


if __name__ == "__main__":
    main()
