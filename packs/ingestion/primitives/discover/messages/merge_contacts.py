#!/usr/bin/env python3
"""Combine selected message-channel metadata by normalized phone or email.

Flow: parse contact CSVs -> union names/groups/channels -> write contacts + manifest.
The first nonempty name wins. Later rows replace counts for the same channel.
Identity matching and person review belong to Deep Context.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Any, Iterable

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
    parse_groups,
    parse_int,
    total_message_count,
)
from packs.ingestion.primitives.common.jsonio import emit, now_iso, write_json  # noqa: E402
from packs.ingestion.schemas.message_contacts import (  # noqa: E402
    CSV_HEADERS,
    GROUP_SEPARATOR,
    REQUIRED_INPUT_HEADERS,
    SCHEMA_DOC,
    SCHEMA_JSON,
)
from packs.shared.csv_io import CsvIO  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def schema_error(path: Path, fieldnames: list[str] | None) -> str:
    fields = ",".join(fieldnames or []) or "<none>"
    header = ",".join(CSV_HEADERS)
    return (
        f"CSV schema mismatch for {path}. Please convert this file into the Powerpacks messages contacts CSV schema before retrying. "
        f"Required input columns: phone,name. Canonical header: {header}. "
        f"Detected columns: {fields}. Schema docs: {SCHEMA_DOC}. JSON schema: {SCHEMA_JSON}. "
        "Common legacy mappings: phone_e164/phone_number -> phone; display_name/full_name -> name; "
        "total_messages -> message_count; imessage_count/imessage_messages -> imessage_message_count; "
        "whatsapp_count/whatsapp_messages -> whatsapp_message_count; message_source/source_channel -> source."
    )


def validate_input_headers(path: Path, fieldnames: list[str] | None) -> None:
    names = {str(value or "").strip() for value in (fieldnames or [])}
    if not REQUIRED_INPUT_HEADERS.issubset(names):
        raise SystemExit(schema_error(path, fieldnames))


def parse_sources(value: str | None) -> list[str]:
    sources: list[str] = []
    for part in (value or "").split(","):
        token = part.strip().lower()
        if token and token not in sources:
            sources.append(token)
    return sources


def serialize_sources(sources: Iterable[str]) -> str:
    return ",".join(sources)


def serialize_groups(groups: Iterable[str]) -> str:
    deduped: list[str] = []
    seen: set[str] = set()
    for group in groups:
        cleaned = re.sub(r"\s+", " ", str(group or "").strip())
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        deduped.append(cleaned)
    deduped.sort(key=str.casefold)
    return GROUP_SEPARATOR.join(deduped)


# ---------------------------------------------------------------------------
# Row -> internal record
# ---------------------------------------------------------------------------

def _record_from_row(row: dict[str, str]) -> dict[str, Any] | None:
    phone = canonicalize_phone(row.get("phone", ""))
    if not phone:
        return None
    sources = parse_sources(row.get("source"))
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
    }


def _merge_records(existing: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    sources = list(dict.fromkeys([*existing["sources"], *new["sources"]]))
    groups = list(dict.fromkeys([*existing["group_names"], *new["group_names"]]))
    channel_counts = dict(existing.get("channel_counts") or {})
    for channel, value in (new.get("channel_counts") or {}).items():
        if value is not None:
            channel_counts[channel] = value
    channel_last_messages = dict(existing.get("channel_last_messages") or {})
    for channel, value in (new.get("channel_last_messages") or {}).items():
        if value:
            channel_last_messages[channel] = value
    name = existing["name"] or new["name"] or ""

    return {
        "phone": existing["phone"],
        "name": name,
        "sources": sources,
        "is_in_group_chats": bool(existing["is_in_group_chats"] or new["is_in_group_chats"] or groups),
        "group_names": groups,
        "channel_counts": channel_counts,
        "channel_last_messages": channel_last_messages,
        "legacy_message_count": new.get("legacy_message_count") if new.get("legacy_message_count") is not None else existing.get("legacy_message_count"),
        "legacy_last_message": max([v for v in (existing.get("legacy_last_message"), new.get("legacy_last_message")) if v], default=None),
    }


# ---------------------------------------------------------------------------
# CSV IO
# ---------------------------------------------------------------------------

def read_input_csv(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not path.exists():
        raise SystemExit(f"input CSV not found: {path}")
    records: list[dict[str, Any]] = []
    counts = {"input_rows": 0, "kept_rows": 0, "invalid_rows": 0}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = CsvIO.dict_reader(handle)
        validate_input_headers(path, reader.fieldnames)
        for row in reader:
            counts["input_rows"] += 1
            record = _record_from_row(row)
            if record is None:
                counts["invalid_rows"] += 1
                continue
            records.append(record)
            counts["kept_rows"] += 1
    return records, counts


def write_output_csv(path: Path, records: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(
        records,
        key=lambda r: (
            -(total_message_count(r) or 0),
            latest_message(r) or "",
            r.get("phone") or "",
        ),
    )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS)
        writer.writeheader()
        for r in rows:
            channel_counts = r.get("channel_counts") or {}
            channel_last_messages = r.get("channel_last_messages") or {}
            message_count = total_message_count(r)
            writer.writerow({
                "phone": r["phone"],
                "name": r.get("name") or "",
                "source": serialize_sources(r.get("sources") or []),
                "is_in_group_chats": "true" if r.get("is_in_group_chats") else "false",
                "group_names": serialize_groups(r.get("group_names") or []),
                "message_count": "" if message_count is None else str(message_count),
                "imessage_message_count": "" if channel_counts.get("imessage") is None else str(channel_counts["imessage"]),
                "whatsapp_message_count": "" if channel_counts.get("whatsapp") is None else str(channel_counts["whatsapp"]),
                "last_message": latest_message(r) or "",
                "imessage_last_message": channel_last_messages.get("imessage") or "",
                "whatsapp_last_message": channel_last_messages.get("whatsapp") or "",
            })
    return len(rows)


# ---------------------------------------------------------------------------
# Subcommand
# ---------------------------------------------------------------------------

class ContactsMerger:
    """Unions N per-channel message-contact CSVs into one canonical contacts.csv,
    deduplicating by canonical phone. Its one method does the read/merge/write and
    returns the manifest (with ``status: ok``) — ``MessagesDiscovery`` calls it
    in-process; the CLI wrapper emits it. A schema-mismatched input still raises
    ``SystemExit`` (via ``read_input_csv``) rather than returning a payload."""

    def merge(
        self,
        *,
        inputs: list[str | Path],
        output: str | Path,
        manifest: str | Path | None = None,
    ) -> dict[str, Any]:
        """Merge ``inputs`` into ``output`` CSV, write the manifest, and return
        it. ``manifest`` defaults next to ``output`` when omitted."""
        input_paths = [Path(p) for p in inputs]
        output_path = Path(output)
        manifest_path = (
            Path(manifest)
            if manifest
            else output_path.with_suffix(output_path.suffix + ".manifest.json")
        )

        by_phone: dict[str, dict[str, Any]] = {}
        per_input_counts: list[dict[str, Any]] = []
        sources_per_phone: dict[str, set[str]] = {}

        for path in input_paths:
            records, counts = read_input_csv(path)
            merged_in_this_file = 0
            new_in_this_file = 0
            for rec in records:
                phone = rec["phone"]
                if phone in by_phone:
                    by_phone[phone] = _merge_records(by_phone[phone], rec)
                    merged_in_this_file += 1
                else:
                    by_phone[phone] = rec
                    new_in_this_file += 1
                sources_per_phone.setdefault(phone, set()).update(rec.get("sources") or [])
            per_input_counts.append({
                "path": str(path),
                **counts,
                "added_new": new_in_this_file,
                "merged_into_existing": merged_in_this_file,
            })

        rows_written = write_output_csv(output_path, list(by_phone.values()))

        cross_channel = sum(1 for s in sources_per_phone.values() if len(s) > 1)
        by_source: dict[str, int] = {}
        for sources in sources_per_phone.values():
            for s in sources:
                by_source[s] = by_source.get(s, 0) + 1

        manifest_payload = {
            "primitive": "messages/merge_contacts",
            "command": "merge",
            "status": "ok",
            "created_at": now_iso(),
            "inputs": per_input_counts,
            "output": str(output_path),
            "manifest_path": str(manifest_path),
            "counts": {
                "rows_written": rows_written,
                "unique_phones": len(by_phone),
                "cross_channel_phones": cross_channel,
                "by_source": by_source,
            },
        }
        write_json(manifest_path, manifest_payload)
        return manifest_payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge per-channel message-contact CSVs into one")
    sub = parser.add_subparsers(dest="command", required=True)
    merge = sub.add_parser("merge", help="Merge N input CSVs into one canonical contacts.csv")
    merge.add_argument("--input", "-i", dest="inputs", action="append", required=True,
                       help="Path to a per-channel CSV (use multiple --input flags to merge several)")
    merge.add_argument("--output", "-o", required=True, help="Path to write the unified contacts.csv")
    merge.add_argument("--manifest", help="Path to write the run manifest JSON")
    args = parser.parse_args()

    # Single subcommand: build the merger, run it, and emit the manifest. A
    # schema-mismatched input still raises SystemExit inside ContactsMerger.merge
    # (via read_input_csv); the happy path exits 0.
    emit(ContactsMerger().merge(
        inputs=list(args.inputs),
        output=args.output,
        manifest=args.manifest if args.manifest else None,
    ))


if __name__ == "__main__":
    main()
