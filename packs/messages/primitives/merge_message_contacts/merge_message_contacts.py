#!/usr/bin/env python3
"""Merge N per-channel message-contact CSVs into a single canonical contacts.csv.

Stdlib-only. Reads any combination of `imessage.contacts.csv`,
`whatsapp.contacts.csv`, and any future channel CSVs that share the canonical
15-column pack shape, deduplicates by canonicalized phone, and writes one
unified `contacts.csv`.

Per-phone merge rules (consistent with `normalize_message_contacts`):

- `name`: first non-empty value across inputs (later inputs do not overwrite
  an existing non-empty name)
- `source`: comma-joined unique source values, preserving first-seen order
- `is_in_group_chats`: logical OR
- `group_names`: union, sorted case-insensitively, ` | ` joined
- `message_count`: maximum across inputs (treats absent as 0; matches
  `normalize_message_contacts.merge_contact`)
- `last_message`: maximum ISO timestamp across inputs
- `skip`: logical OR
- `match_*`: keep the highest-confidence match block. Tie-breaker: prefer the
  one whose `match_status` is matched > suggested > unmatched > empty.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


CSV_HEADERS = [
    "phone",
    "name",
    "source",
    "is_in_group_chats",
    "group_names",
    "message_count",
    "last_message",
    "skip",
    "match_status",
    "matched_person_id",
    "matched_name",
    "matched_linkedin_url",
    "match_confidence",
    "match_method",
    "match_reason",
]

GROUP_SEPARATOR = " | "
STATUS_RANK = {"matched": 3, "suggested": 2, "unmatched": 1, "": 0}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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


def parse_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def parse_int(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = int(float(text))
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def parse_float(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_sources(value: str | None) -> list[str]:
    sources: list[str] = []
    for part in (value or "").split(","):
        token = part.strip().lower()
        if token and token not in sources:
            sources.append(token)
    return sources


def parse_groups(value: str | None) -> list[str]:
    groups: list[str] = []
    for part in (value or "").split(GROUP_SEPARATOR):
        cleaned = re.sub(r"\s+", " ", part.strip())
        if cleaned and cleaned not in groups:
            groups.append(cleaned)
    return groups


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
    return {
        "phone": phone,
        "name": (row.get("name") or "").strip(),
        "sources": parse_sources(row.get("source")),
        "is_in_group_chats": parse_bool(row.get("is_in_group_chats")),
        "group_names": parse_groups(row.get("group_names")),
        "message_count": parse_int(row.get("message_count")),
        "last_message": (row.get("last_message") or "").strip() or None,
        "skip": parse_bool(row.get("skip")),
        "match_status": (row.get("match_status") or "").strip().lower(),
        "matched_person_id": (row.get("matched_person_id") or "").strip(),
        "matched_name": (row.get("matched_name") or "").strip(),
        "matched_linkedin_url": (row.get("matched_linkedin_url") or "").strip(),
        "match_confidence": parse_float(row.get("match_confidence")),
        "match_method": (row.get("match_method") or "").strip(),
        "match_reason": (row.get("match_reason") or "").strip(),
    }


def _better_match(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Return the match-block dict that should win for a merged row."""
    l_conf = left.get("match_confidence") or 0.0
    r_conf = right.get("match_confidence") or 0.0
    if r_conf > l_conf:
        return right
    if l_conf > r_conf:
        return left
    l_rank = STATUS_RANK.get(left.get("match_status", ""), 0)
    r_rank = STATUS_RANK.get(right.get("match_status", ""), 0)
    if r_rank > l_rank:
        return right
    return left


def _merge_records(existing: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    sources = list(dict.fromkeys([*existing["sources"], *new["sources"]]))
    groups = list(dict.fromkeys([*existing["group_names"], *new["group_names"]]))
    existing_count = existing.get("message_count")
    new_count = new.get("message_count")
    if existing_count is None and new_count is None:
        message_count = None
    else:
        message_count = max(existing_count or 0, new_count or 0)
    last_message = max(
        [v for v in (existing.get("last_message"), new.get("last_message")) if v],
        default=None,
    )
    name = existing["name"] or new["name"] or ""

    match_winner = _better_match(existing, new)

    return {
        "phone": existing["phone"],
        "name": name,
        "sources": sources,
        "is_in_group_chats": bool(existing["is_in_group_chats"] or new["is_in_group_chats"] or groups),
        "group_names": groups,
        "message_count": message_count,
        "last_message": last_message,
        "skip": bool(existing["skip"] or new["skip"]),
        "match_status": match_winner.get("match_status", ""),
        "matched_person_id": match_winner.get("matched_person_id", ""),
        "matched_name": match_winner.get("matched_name", ""),
        "matched_linkedin_url": match_winner.get("matched_linkedin_url", ""),
        "match_confidence": match_winner.get("match_confidence"),
        "match_method": match_winner.get("match_method", ""),
        "match_reason": match_winner.get("match_reason", ""),
    }


# ---------------------------------------------------------------------------
# CSV IO
# ---------------------------------------------------------------------------

def read_input_csv(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not path.exists():
        raise SystemExit(f"input CSV not found: {path}")
    records: list[dict[str, Any]] = []
    counts = {"input_rows": 0, "kept_rows": 0, "invalid_rows": 0}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
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
            -(r.get("message_count") or 0),
            r.get("last_message") or "",
            r.get("phone") or "",
        ),
    )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS)
        writer.writeheader()
        for r in rows:
            confidence = r.get("match_confidence")
            confidence_str = ""
            if confidence is not None:
                confidence_str = f"{confidence:.3f}".rstrip("0").rstrip(".") or "0"
            writer.writerow({
                "phone": r["phone"],
                "name": r.get("name") or "",
                "source": serialize_sources(r.get("sources") or []),
                "is_in_group_chats": "true" if r.get("is_in_group_chats") else "false",
                "group_names": serialize_groups(r.get("group_names") or []),
                "message_count": "" if r.get("message_count") is None else str(r["message_count"]),
                "last_message": r.get("last_message") or "",
                "skip": "yes" if r.get("skip") else "",
                "match_status": r.get("match_status") or "",
                "matched_person_id": r.get("matched_person_id") or "",
                "matched_name": r.get("matched_name") or "",
                "matched_linkedin_url": r.get("matched_linkedin_url") or "",
                "match_confidence": confidence_str,
                "match_method": r.get("match_method") or "",
                "match_reason": r.get("match_reason") or "",
            })
    return len(rows)


# ---------------------------------------------------------------------------
# Subcommand
# ---------------------------------------------------------------------------

def cmd_merge(args: argparse.Namespace) -> int:
    inputs = [Path(p) for p in args.inputs]
    output = Path(args.output)
    manifest_path = Path(args.manifest) if args.manifest else output.with_suffix(output.suffix + ".manifest.json")

    by_phone: dict[str, dict[str, Any]] = {}
    per_input_counts: list[dict[str, Any]] = []
    sources_per_phone: dict[str, set[str]] = {}

    for path in inputs:
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

    rows_written = write_output_csv(output, list(by_phone.values()))

    cross_channel = sum(1 for s in sources_per_phone.values() if len(s) > 1)
    by_source: dict[str, int] = {}
    for sources in sources_per_phone.values():
        for s in sources:
            by_source[s] = by_source.get(s, 0) + 1

    manifest = {
        "primitive": "merge_message_contacts",
        "command": "merge",
        "created_at": now_iso(),
        "inputs": per_input_counts,
        "output": str(output),
        "manifest_path": str(manifest_path),
        "counts": {
            "rows_written": rows_written,
            "unique_phones": len(by_phone),
            "cross_channel_phones": cross_channel,
            "by_source": by_source,
        },
    }
    write_json(manifest_path, manifest)
    emit(manifest)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge per-channel message-contact CSVs into one")
    sub = parser.add_subparsers(dest="command", required=True)
    merge = sub.add_parser("merge", help="Merge N input CSVs into one canonical contacts.csv")
    merge.add_argument("--input", "-i", dest="inputs", action="append", required=True,
                       help="Path to a per-channel CSV (use multiple --input flags to merge several)")
    merge.add_argument("--output", "-o", required=True, help="Path to write the unified contacts.csv")
    merge.add_argument("--manifest", help="Path to write the run manifest JSON")
    merge.set_defaults(func=cmd_merge)
    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
