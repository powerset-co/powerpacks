#!/usr/bin/env python3
"""Normalize contact-exporter CSV into Powerpacks messages JSONL."""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


DEFAULT_OUT_DIR = Path(".powerpacks/messages")
GROUP_SEPARATOR = " | "


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonicalize_phone(raw: str) -> str:
    value = (raw or "").strip()
    digits = re.sub(r"[^\d]", "", value)
    if len(digits) < 7:
        return ""
    if value.startswith("+"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) <= 15:
        return f"+{digits}"
    return digits


def parse_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "y"}


def parse_int(value: str | None) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = int(float(text))
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def parse_float(value: str | None) -> float | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_sources(value: str | None) -> list[str]:
    sources = []
    for part in (value or "").split(","):
        source = part.strip().lower()
        if source in {"imessage", "whatsapp"} and source not in sources:
            sources.append(source)
    return sources


def parse_groups(value: str | None) -> list[str]:
    groups = []
    for part in (value or "").split(GROUP_SEPARATOR):
        group = re.sub(r"\s+", " ", part.strip())
        if group and group not in groups:
            groups.append(group)
    return groups


def normalize_row(row: dict[str, str]) -> dict[str, Any] | None:
    phone = canonicalize_phone(row.get("phone", ""))
    if not phone:
        return None
    sources = parse_sources(row.get("source"))
    if not sources:
        return None
    confidence = parse_float(row.get("match_confidence"))
    return {
        "phone": phone,
        "name": (row.get("name") or "").strip(),
        "sources": sources,
        "is_in_group_chats": parse_bool(row.get("is_in_group_chats")),
        "group_names": parse_groups(row.get("group_names")),
        "message_count": parse_int(row.get("message_count")),
        "last_message": (row.get("last_message") or "").strip() or None,
        "skip": parse_bool(row.get("skip")),
        "match": {
            "status": (row.get("match_status") or "").strip() or None,
            "person_id": (row.get("matched_person_id") or "").strip() or None,
            "name": (row.get("matched_name") or "").strip() or None,
            "linkedin_url": (row.get("matched_linkedin_url") or "").strip() or None,
            "confidence": confidence,
            "method": (row.get("match_method") or "").strip() or None,
            "reason": (row.get("match_reason") or "").strip() or None,
        },
    }


def merge_contact(existing: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    sources = list(dict.fromkeys([*existing.get("sources", []), *new.get("sources", [])]))
    groups = list(dict.fromkeys([*existing.get("group_names", []), *new.get("group_names", [])]))
    existing_count = existing.get("message_count")
    new_count = new.get("message_count")
    message_count = max(existing_count or 0, new_count or 0) if (existing_count is not None or new_count is not None) else None
    last_message = max([value for value in [existing.get("last_message"), new.get("last_message")] if value], default=None)

    match = existing.get("match") or {}
    new_match = new.get("match") or {}
    if (new_match.get("confidence") or 0) > (match.get("confidence") or 0):
        match = new_match
    elif not match.get("person_id") and new_match.get("person_id"):
        match = new_match

    return {
        "phone": existing["phone"],
        "name": existing.get("name") or new.get("name") or "",
        "sources": sources,
        "is_in_group_chats": bool(existing.get("is_in_group_chats") or new.get("is_in_group_chats") or groups),
        "group_names": groups,
        "message_count": message_count,
        "last_message": last_message,
        "skip": bool(existing.get("skip") or new.get("skip")),
        "match": match,
    }


def read_contacts(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    contacts_by_phone: dict[str, dict[str, Any]] = {}
    counts = {
        "input_rows": 0,
        "normalized_rows": 0,
        "skipped_invalid_rows": 0,
        "explicit_skip_rows": 0,
        "imessage_rows": 0,
        "whatsapp_rows": 0,
    }
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            counts["input_rows"] += 1
            contact = normalize_row(row)
            if not contact:
                counts["skipped_invalid_rows"] += 1
                continue
            phone = contact["phone"]
            if phone in contacts_by_phone:
                contacts_by_phone[phone] = merge_contact(contacts_by_phone[phone], contact)
            else:
                contacts_by_phone[phone] = contact

    contacts = sorted(
        contacts_by_phone.values(),
        key=lambda item: (item.get("message_count") or 0, item.get("last_message") or ""),
        reverse=True,
    )
    counts["normalized_rows"] = len(contacts)
    counts["explicit_skip_rows"] = sum(1 for item in contacts if item.get("skip"))
    counts["imessage_rows"] = sum(1 for item in contacts if "imessage" in item.get("sources", []))
    counts["whatsapp_rows"] = sum(1 for item in contacts if "whatsapp" in item.get("sources", []))
    return contacts, counts


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def cmd_normalize(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    run_id = args.run_id or f"messages-normalize-{uuid4()}"
    out_jsonl = Path(args.out_jsonl) if args.out_jsonl else DEFAULT_OUT_DIR / f"{run_id}.contacts.jsonl"
    manifest_path = Path(args.manifest) if args.manifest else out_jsonl.with_suffix(out_jsonl.suffix + ".manifest.json")

    contacts, counts = read_contacts(input_path)
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with out_jsonl.open("w", encoding="utf-8") as handle:
        for contact in contacts:
            handle.write(json.dumps(contact, sort_keys=True) + "\n")

    manifest = {
        "run_id": run_id,
        "created_at": now_iso(),
        "primitive": "normalize_message_contacts",
        "input": str(input_path),
        "artifacts": {
            "jsonl": str(out_jsonl),
            "manifest": str(manifest_path),
        },
        "counts": counts,
    }
    write_json(manifest_path, manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize contact-exporter CSV output")
    sub = parser.add_subparsers(dest="command", required=True)
    normalize = sub.add_parser("normalize")
    normalize.add_argument("--input", required=True)
    normalize.add_argument("--out-jsonl")
    normalize.add_argument("--manifest")
    normalize.add_argument("--run-id")
    normalize.set_defaults(func=cmd_normalize)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
