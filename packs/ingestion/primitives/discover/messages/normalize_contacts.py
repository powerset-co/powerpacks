#!/usr/bin/env python3
"""Normalize a message-contacts CSV into canonical Powerpacks messages JSONL.

Usage:
    normalize_contacts.py normalize --input .powerpacks/messages/contacts.csv \
        [--out-jsonl PATH] [--manifest PATH]

Writes the normalized JSONL (default
`.powerpacks/messages/contacts.normalized.jsonl`) plus a manifest JSON next to
it with row counts.

Changelog:
  2026-07-23 (cmd inline): the ``cmd_normalize`` dispatcher was inlined into
    ``main`` — the single ``normalize`` subcommand constructs
    ``ContactsNormalizer``, calls ``normalize``, and emits its manifest directly
    (no ``set_defaults(func=)`` indirection). CLI subcommand, flags, and stdout
    are unchanged.
  2026-07-23 (in-process): the normalize logic moved onto a ``ContactsNormalizer``
    class (``normalize(*, input, out_jsonl, manifest) -> dict`` returning the
    manifest with ``status: ok``). The message channels call it in-process instead
    of spawning this file; the ``normalize`` CLI subcommand, flags, and stdout are
    unchanged.
  2026-07-23 (audit): normalize_contacts.README.md sidecar folded into this
    docstring; dropped the stale contact-exporter naming (the pipeline no
    longer uses contact-exporter).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.contact_fields import (  # noqa: E402
    canonicalize_phone,
    channel_counts_from_row,
    channel_last_messages_from_row,
    latest_message,
    parse_bool,
    parse_float,
    parse_groups,
    parse_int,
    total_message_count,
)
from packs.ingestion.primitives.common.jsonio import emit, now_iso, write_json  # noqa: E402
from packs.ingestion.primitives.common.paths import MESSAGES_OUT_DIR  # noqa: E402
from packs.ingestion.schemas.message_contacts import MESSAGE_CHANNELS  # noqa: E402
from packs.shared.csv_io import CsvIO  # noqa: E402


def parse_sources(value: str | None) -> list[str]:
    sources = []
    for part in (value or "").split(","):
        source = part.strip().lower()
        if source in set(MESSAGE_CHANNELS) and source not in sources:
            sources.append(source)
    return sources


def normalize_row(row: dict[str, str]) -> dict[str, Any] | None:
    phone = canonicalize_phone(row.get("phone", ""))
    if not phone:
        return None
    sources = parse_sources(row.get("source"))
    if not sources:
        return None
    confidence = parse_float(row.get("match_confidence"))
    legacy_count = parse_int(row.get("message_count"))
    legacy_last = (row.get("last_message") or "").strip() or None
    return {
        "phone": phone,
        "name": (row.get("name") or "").strip(),
        "sources": sources,
        "is_in_group_chats": parse_bool(row.get("is_in_group_chats")),
        "group_names": parse_groups(row.get("group_names")),
        "channel_counts": channel_counts_from_row(row, sources, legacy_count),
        "channel_last_messages": channel_last_messages_from_row(row, sources, legacy_last),
        "legacy_message_count": legacy_count,
        "legacy_last_message": legacy_last,
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
    channel_counts = dict(existing.get("channel_counts") or {})
    for channel, value in (new.get("channel_counts") or {}).items():
        if value is not None:
            channel_counts[channel] = value
    channel_last_messages = dict(existing.get("channel_last_messages") or {})
    for channel, value in (new.get("channel_last_messages") or {}).items():
        if value:
            channel_last_messages[channel] = value

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
        "channel_counts": channel_counts,
        "channel_last_messages": channel_last_messages,
        "legacy_message_count": new.get("legacy_message_count") if new.get("legacy_message_count") is not None else existing.get("legacy_message_count"),
        "legacy_last_message": max([value for value in [existing.get("legacy_last_message"), new.get("legacy_last_message")] if value], default=None),
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
        reader = CsvIO.dict_reader(handle)
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
        key=lambda item: (total_message_count(item) or 0, latest_message(item) or ""),
        reverse=True,
    )
    for item in contacts:
        item["message_count"] = total_message_count(item)
        item["last_message"] = latest_message(item)
        channel_counts = item.get("channel_counts") or {}
        channel_last_messages = item.get("channel_last_messages") or {}
        item["imessage_message_count"] = channel_counts.get("imessage")
        item["whatsapp_message_count"] = channel_counts.get("whatsapp")
        item["imessage_last_message"] = channel_last_messages.get("imessage")
        item["whatsapp_last_message"] = channel_last_messages.get("whatsapp")
        item.pop("channel_counts", None)
        item.pop("channel_last_messages", None)
        item.pop("legacy_message_count", None)
        item.pop("legacy_last_message", None)
    counts["normalized_rows"] = len(contacts)
    counts["explicit_skip_rows"] = sum(1 for item in contacts if item.get("skip"))
    counts["imessage_rows"] = sum(1 for item in contacts if "imessage" in item.get("sources", []))
    counts["whatsapp_rows"] = sum(1 for item in contacts if "whatsapp" in item.get("sources", []))
    return contacts, counts


class ContactsNormalizer:
    """Normalizes a message-contacts CSV into canonical messages JSONL. Its one
    method does the read/dedup/write and returns the manifest (with ``status:
    ok``) — the message channels call it in-process; the CLI wrapper prints it."""

    def normalize(
        self,
        *,
        input: str | Path,
        out_jsonl: str | Path | None = None,
        manifest: str | Path | None = None,
    ) -> dict[str, Any]:
        """Read ``input`` CSV, write the normalized JSONL + manifest, and return
        the manifest dict. ``out_jsonl``/``manifest`` default next to the shared
        messages dir when omitted."""
        input_path = Path(input)
        out_jsonl_path = (
            Path(out_jsonl)
            if out_jsonl
            else MESSAGES_OUT_DIR / "contacts.normalized.jsonl"
        )
        manifest_path = (
            Path(manifest)
            if manifest
            else out_jsonl_path.with_suffix(out_jsonl_path.suffix + ".manifest.json")
        )

        contacts, counts = read_contacts(input_path)
        out_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with out_jsonl_path.open("w", encoding="utf-8") as handle:
            for contact in contacts:
                handle.write(json.dumps(contact, sort_keys=True) + "\n")

        manifest_payload = {
            "created_at": now_iso(),
            "primitive": "messages/normalize_contacts",
            "status": "ok",
            "input": str(input_path),
            "artifacts": {
                "jsonl": str(out_jsonl_path),
                "manifest": str(manifest_path),
            },
            "counts": counts,
        }
        write_json(manifest_path, manifest_payload)
        return manifest_payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize contact-exporter CSV output")
    sub = parser.add_subparsers(dest="command", required=True)
    normalize = sub.add_parser("normalize")
    normalize.add_argument("--input", required=True)
    normalize.add_argument("--out-jsonl")
    normalize.add_argument("--manifest")
    args = parser.parse_args()

    # Single subcommand: build the normalizer, run it, and emit the manifest.
    emit(ContactsNormalizer().normalize(
        input=args.input, out_jsonl=args.out_jsonl, manifest=args.manifest,
    ))


if __name__ == "__main__":
    main()
