#!/usr/bin/env python3
"""Merge/dedupe local network-import sources into one people schema CSV.

Dedupe rule:
1. Merge rows with the same LinkedIn public identifier / URL.
2. Keep non-LinkedIn rows separate, but emit similar-name review pairs.

Stdlib-only. Local artifacts only. No uploads or external API calls.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from packs.ingestion.schemas.people_schema import (
        PEOPLE_SCHEMA_COLUMNS,
        normalize_people_row,
        stable_linkedin_key,
        extract_public_identifier,
    )
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.schemas.people_schema import (
        PEOPLE_SCHEMA_COLUMNS,
        normalize_people_row,
        stable_linkedin_key,
        extract_public_identifier,
    )

DEFAULT_OUTPUT_DIR = Path(".powerpacks/network-import/merged")
MERGED_COLUMNS = PEOPLE_SCHEMA_COLUMNS + ["merge_key", "merge_confidence", "merge_sources", "merged_row_count", "needs_review"]
REVIEW_COLUMNS = ["left_id", "right_id", "left_name", "right_name", "similarity", "left_sources", "right_sources", "reason"]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in fieldnames})


def sha(value: str, n: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:n]


def normalize_name(value: str) -> str:
    value = (value or "").lower()
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"[^a-z0-9 ]+", " ", value)
    parts = [p for p in value.split() if p not in {"jr", "sr", "ii", "iii", "phd", "mba", "md"}]
    return " ".join(parts)


def row_name(row: dict[str, str]) -> str:
    return row.get("full_name") or " ".join(x for x in [row.get("first_name", ""), row.get("last_name", "")] if x).strip() or row.get("name", "") or row.get("display_name", "")


def discover_inputs(base: Path) -> list[Path]:
    """Discover canonical provider-neutral people inputs under .powerpacks."""

    paths = list(base.glob("network-import/*/*/people.csv"))
    msg = base / "messages" / "contacts.csv"
    if msg.exists():
        paths.append(msg)
    return sorted(set(paths))


def source_label(path: Path) -> str:
    text = str(path)
    if "/linkedin/" in text:
        return "linkedin"
    if "/twitter/" in text:
        return "twitter"
    if "/gmail/" in text:
        return "gmail"
    if "/messages/" in text:
        return "messages"
    return path.parent.name


def message_row_to_people(row: dict[str, str], path: Path) -> dict[str, str]:
    linkedin = row.get("matched_linkedin_url", "")
    full_name = row.get("matched_name") or row.get("name") or ""
    parts = full_name.split(" ", 1)
    people = {
        "id": row.get("matched_person_id") or f"message:{sha((row.get('phone') or '') + full_name)}",
        "linkedin_url": linkedin,
        "public_identifier": extract_public_identifier(linkedin),
        "first_name": parts[0] if parts else "",
        "last_name": parts[1] if len(parts) > 1 else "",
        "full_name": full_name,
        "primary_phone": row.get("phone", ""),
        "all_phones": row.get("phone", ""),
        "source_channels": row.get("source") or "messages",
        "source_artifacts": str(path),
        "summary": f"message_count={row.get('message_count','')}; last_message={row.get('last_message','')}",
        "enrichment_provider": row.get("match_method") or "messages_contact_match",
    }
    return normalize_people_row(people)


def load_people_file(path: Path) -> list[dict[str, str]]:
    rows = read_csv(path)
    label = source_label(path)
    out: list[dict[str, str]] = []
    for row in rows:
        if path.name == "contacts.csv" and label == "messages":
            normalized = message_row_to_people(row, path)
        else:
            normalized = normalize_people_row(row)
            normalized["source_artifacts"] = normalized.get("source_artifacts") or str(path)
            normalized["source_channels"] = normalized.get("source_channels") or label
        out.append(normalized)
    return out


def choose(current: str, incoming: str) -> str:
    if not current and incoming:
        return incoming
    if incoming and len(incoming) > len(current) and current in {"", "[]", "{}"}:
        return incoming
    return current


def merge_group(key: str, rows: list[dict[str, str]]) -> dict[str, Any]:
    merged = {col: "" for col in PEOPLE_SCHEMA_COLUMNS}
    sources: set[str] = set()
    artifacts: set[str] = set()
    for row in rows:
        for col in PEOPLE_SCHEMA_COLUMNS:
            merged[col] = choose(merged.get(col, ""), row.get(col, ""))
        for src in (row.get("source_channels") or "").split(","):
            if src.strip():
                sources.add(src.strip())
        if row.get("source_artifacts"):
            artifacts.add(row["source_artifacts"])
    merged["source_channels"] = ",".join(sorted(sources))
    merged["source_artifacts"] = json.dumps(sorted(artifacts))
    merged["merge_key"] = key
    merged["merge_confidence"] = "1.0" if key.startswith("linkedin:") else "0.0"
    merged["merge_sources"] = merged["source_channels"]
    merged["merged_row_count"] = len(rows)
    merged["needs_review"] = "false"
    if not merged.get("id"):
        merged["id"] = f"merged:{sha(key + row_name(merged))}"
    return merged


def build_groups(rows: list[dict[str, str]]) -> tuple[dict[str, list[dict[str, str]]], list[dict[str, str]]]:
    groups: dict[str, list[dict[str, str]]] = {}
    singletons: list[dict[str, str]] = []
    for row in rows:
        key = stable_linkedin_key(row)
        if key:
            groups.setdefault(key, []).append(row)
        else:
            singletons.append(row)
    return groups, singletons


def similar_pairs(rows: list[dict[str, Any]], threshold: float) -> list[dict[str, Any]]:
    review: list[dict[str, Any]] = []
    named = [(i, normalize_name(row_name(r)), r) for i, r in enumerate(rows) if normalize_name(row_name(r))]
    for i in range(len(named)):
        _, left_name, left = named[i]
        for j in range(i + 1, len(named)):
            _, right_name, right = named[j]
            if left.get("merge_key") == right.get("merge_key"):
                continue
            if not left_name or not right_name:
                continue
            ratio = difflib.SequenceMatcher(None, left_name, right_name).ratio()
            exact_parts = set(left_name.split()) == set(right_name.split()) and len(left_name.split()) >= 2
            if ratio >= threshold or exact_parts:
                left["needs_review"] = "true"
                right["needs_review"] = "true"
                review.append({
                    "left_id": left.get("id", ""),
                    "right_id": right.get("id", ""),
                    "left_name": row_name(left),
                    "right_name": row_name(right),
                    "similarity": round(ratio, 3),
                    "left_sources": left.get("source_channels", ""),
                    "right_sources": right.get("source_channels", ""),
                    "reason": "similar_name_no_shared_linkedin",
                })
    return review


def cmd_run(args: argparse.Namespace) -> int:
    inputs = [Path(p) for p in args.input] if args.input else discover_inputs(Path(args.base_dir))
    all_rows: list[dict[str, str]] = []
    per_file: dict[str, int] = {}
    for path in inputs:
        if not path.exists():
            continue
        rows = load_people_file(path)
        all_rows.extend(rows)
        per_file[str(path)] = len(rows)
    groups, singletons = build_groups(all_rows)
    merged_rows = [merge_group(key, rows) for key, rows in sorted(groups.items())]
    for row in singletons:
        normalized = normalize_people_row(row)
        normalized.update({
            "merge_key": f"source:{sha((row.get('source_artifacts','') or '') + row_name(row) + row.get('primary_phone','') + row.get('primary_email',''))}",
            "merge_confidence": "0.0",
            "merge_sources": normalized.get("source_channels", ""),
            "merged_row_count": 1,
            "needs_review": "false",
        })
        if not normalized.get("id"):
            normalized["id"] = f"merged:{sha(normalized['merge_key'])}"
        merged_rows.append(normalized)
    review = similar_pairs(merged_rows, args.name_threshold)
    output_dir = Path(args.output_dir)
    output = output_dir / "people.csv"
    write_csv(output, MERGED_COLUMNS, merged_rows)
    manifest_payload = {
        "created_at": now_iso(),
        "inputs": per_file,
        "input_rows": len(all_rows),
        "merged_rows": len(merged_rows),
        "linkedin_groups": len(groups),
        "review_pairs": len(review),
        "output": str(output),
    }
    emit({"status": "completed", **manifest_payload})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Merge/dedupe local network import people artifacts")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--input", action="append", help="Input provider-neutral people.csv or messages contacts.csv; repeatable. Defaults to discovery under .powerpacks")
    run.add_argument("--base-dir", default=".powerpacks")
    run.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    run.add_argument("--name-threshold", type=float, default=0.92)
    run.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
