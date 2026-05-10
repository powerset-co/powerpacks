#!/usr/bin/env python3
"""Local name matcher between a contacts CSV and a Powerset candidate CSV.

Stdlib-only port of `contact_exporter.matching.apply_local_name_matching`.

Tiers (highest precedence first):

1. Single exact normalized-name match → matched, confidence 1.0
2. Multiple exact normalized-name matches → suggested, confidence 0.80
3. Single-token first-name-only match → suggested, never matched
4. Same last-name pool with a unique first-name prefix candidate → matched
5. Same last-name pool with multiple prefix candidates → suggested (best score)
6. Fuzzy ratio in same-last-name pool ≥ 0.94 with margin ≥ 0.05 → matched
7. Fuzzy ratio ≥ 0.80 → suggested
8. Otherwise unmatched

Updates the message-contacts CSV in place with the
`match_status / matched_person_id / matched_name / matched_linkedin_url /
match_confidence / match_method / match_reason` columns.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


CSV_HEADERS = [
    "phone",
    "name",
    "source",
    "is_in_group_chats",
    "group_names",
    "message_count",
    "imessage_message_count",
    "whatsapp_message_count",
    "last_message",
    "imessage_last_message",
    "whatsapp_last_message",
    "skip",
    "match_status",
    "matched_person_id",
    "matched_name",
    "matched_linkedin_url",
    "match_confidence",
    "match_method",
    "match_reason",
]
REQUIRED_INPUT_HEADERS = {"phone", "name"}
SCHEMA_DOC = "packs/messages/schemas/contacts-csv.md"
SCHEMA_JSON = "packs/messages/schemas/contacts-csv.schema.json"


@dataclass
class Candidate:
    id: str
    name: str
    linkedin_url: str | None = None
    phone_number: str | None = None
    public_identifier: str | None = None
    emails: list[str] = field(default_factory=list)


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


def normalize_name(raw: str | None) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", (raw or "").strip().lower())
    return re.sub(r"\s+", " ", s).strip()


def first_name_prefix_match(a: str, b: str) -> bool:
    """True when first names look like strong prefix variants."""
    a = (a or "").strip()
    b = (b or "").strip()
    if len(a) < 4 or len(b) < 4:
        return False
    return a.startswith(b) or b.startswith(a)


def load_candidates(path: Path) -> list[Candidate]:
    if not path.exists():
        return []
    out: list[Candidate] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            cid = (row.get("id") or "").strip()
            name = (row.get("name") or "").strip()
            if not cid or not name:
                continue
            emails_raw = (row.get("emails") or "").strip()
            emails = [e for e in emails_raw.split(";") if e]
            out.append(Candidate(
                id=cid,
                name=name,
                linkedin_url=(row.get("linkedin_url") or "").strip() or None,
                phone_number=(row.get("phone_number") or "").strip() or None,
                public_identifier=(row.get("public_identifier") or "").strip() or None,
                emails=emails,
            ))
    return out


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


def _set_unmatched(row: dict[str, str], reason: str) -> None:
    row["match_status"] = "unmatched"
    row["matched_person_id"] = ""
    row["matched_name"] = ""
    row["matched_linkedin_url"] = ""
    row["match_confidence"] = ""
    row["match_method"] = "unmatched"
    row["match_reason"] = reason


def _set_match(
    row: dict[str, str],
    *,
    status: str,
    candidate: Candidate,
    confidence: float,
    method: str,
    reason: str,
) -> None:
    row["match_status"] = status
    row["matched_person_id"] = candidate.id
    row["matched_name"] = candidate.name
    row["matched_linkedin_url"] = candidate.linkedin_url or ""
    row["match_confidence"] = f"{confidence:.3f}".rstrip("0").rstrip(".") or "0"
    row["match_method"] = method
    row["match_reason"] = reason


# ---------------------------------------------------------------------------
# Matcher
# ---------------------------------------------------------------------------

def apply_matching(rows: list[dict[str, str]], candidates: list[Candidate]) -> dict[str, int]:
    if not rows:
        return {"total": 0, "matched": 0, "suggested": 0, "unmatched": 0}
    if not candidates:
        for row in rows:
            _set_unmatched(row, "no local candidate catalog available")
        return {"total": len(rows), "matched": 0, "suggested": 0, "unmatched": len(rows)}

    exact_index: dict[str, list[Candidate]] = {}
    last_name_index: dict[str, list[Candidate]] = {}
    first_name_index: dict[str, list[Candidate]] = {}
    for c in candidates:
        norm = normalize_name(c.name)
        if not norm:
            continue
        exact_index.setdefault(norm, []).append(c)
        parts = norm.split(" ")
        if len(parts) >= 2:
            last_name_index.setdefault(parts[-1], []).append(c)
            first_name_index.setdefault(parts[0], []).append(c)
    for bucket in exact_index.values():
        bucket.sort(key=lambda c: c.id)
    for bucket in last_name_index.values():
        bucket.sort(key=lambda c: c.id)
    for bucket in first_name_index.values():
        bucket.sort(key=lambda c: c.id)

    matched = suggested = unmatched = 0

    for row in rows:
        contact_name = (row.get("name") or "").strip()
        norm_contact = normalize_name(contact_name)
        if not norm_contact:
            unmatched += 1
            _set_unmatched(row, "missing contact name")
            continue
        if norm_contact == normalize_name(row.get("phone")):
            unmatched += 1
            _set_unmatched(row, "name is identical to phone")
            continue

        exact = list(exact_index.get(norm_contact, []))
        if len(exact) == 1:
            matched += 1
            _set_match(row, status="matched", candidate=exact[0], confidence=1.0,
                       method="name_exact_linkedin", reason="unique exact name match")
            continue
        if len(exact) > 1:
            suggested += 1
            _set_match(row, status="suggested", candidate=exact[0], confidence=0.80,
                       method="name_exact_ambiguous", reason=f"{len(exact)} exact-name candidates")
            continue

        tokens = norm_contact.split(" ")
        if len(tokens) < 2:
            first_pool = list(first_name_index.get(tokens[0], []))
            if len(first_pool) == 1:
                suggested += 1
                _set_match(
                    row, status="suggested", candidate=first_pool[0], confidence=0.60,
                    method="name_first_only_unique_suggested",
                    reason="single-token first-name-only candidate requires review",
                )
                continue
            if len(first_pool) > 1:
                suggested += 1
                _set_match(
                    row, status="suggested", candidate=first_pool[0], confidence=0.70,
                    method="name_first_only_ambiguous",
                    reason=f"{len(first_pool)} candidates share this first name",
                )
                continue
            unmatched += 1
            _set_unmatched(row, "single-token name with no candidate first-name match")
            continue

        pool = list(last_name_index.get(tokens[-1], []))
        if not pool:
            unmatched += 1
            _set_unmatched(row, "no same-last-name candidates")
            continue

        contact_first = tokens[0]
        prefix_pool = []
        for cand in pool:
            cand_tokens = normalize_name(cand.name).split(" ")
            cand_first = cand_tokens[0] if cand_tokens else ""
            if first_name_prefix_match(contact_first, cand_first):
                prefix_pool.append(cand)

        if len(prefix_pool) == 1:
            cand = prefix_pool[0]
            ratio = SequenceMatcher(None, norm_contact, normalize_name(cand.name)).ratio()
            confidence = round(max(0.95, ratio), 3)
            matched += 1
            _set_match(row, status="matched", candidate=cand, confidence=confidence,
                       method="name_prefix_lastname_linkedin",
                       reason="unique first-name prefix with same last name")
            continue
        if len(prefix_pool) > 1:
            scored = sorted(
                ((SequenceMatcher(None, norm_contact, normalize_name(c.name)).ratio(), c) for c in prefix_pool),
                key=lambda item: item[0], reverse=True,
            )
            best_score, best_candidate = scored[0]
            suggested += 1
            _set_match(row, status="suggested", candidate=best_candidate,
                       confidence=round(float(max(best_score, 0.85)), 3),
                       method="name_prefix_lastname_suggested",
                       reason=f"{len(prefix_pool)} prefix candidates with same last name")
            continue

        scored = sorted(
            ((SequenceMatcher(None, norm_contact, normalize_name(c.name)).ratio(), c) for c in pool),
            key=lambda item: item[0], reverse=True,
        )
        best_score, best_candidate = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0
        confidence = round(float(best_score), 3)

        if best_score >= 0.94 and (best_score - second_score) >= 0.05:
            matched += 1
            _set_match(row, status="matched", candidate=best_candidate,
                       confidence=confidence, method="name_fuzzy_linkedin",
                       reason="high-confidence fuzzy last-name match")
            continue
        if best_score >= 0.80:
            suggested += 1
            _set_match(row, status="suggested", candidate=best_candidate,
                       confidence=confidence, method="name_fuzzy_suggested",
                       reason="high-confidence fuzzy last-name candidate")
            continue

        unmatched += 1
        _set_unmatched(row, "low-confidence fuzzy candidate")

    return {
        "total": len(rows),
        "matched": matched,
        "suggested": suggested,
        "unmatched": unmatched,
    }


# ---------------------------------------------------------------------------
# CSV IO
# ---------------------------------------------------------------------------

def read_contacts(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise SystemExit(f"contacts file not found: {path}")
    rows: list[dict[str, str]] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return []
        validate_input_headers(path, reader.fieldnames)
        for row in reader:
            normalized = {key: (row.get(key) or "") for key in CSV_HEADERS}
            rows.append(normalized)
    return rows


def write_contacts(path: Path, rows: list[dict[str, str]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


# ---------------------------------------------------------------------------
# Subcommand
# ---------------------------------------------------------------------------

def cmd_match(args: argparse.Namespace) -> int:
    contacts_path = Path(args.contacts)
    candidates_path = Path(args.candidates)
    manifest_path = Path(args.manifest) if args.manifest else contacts_path.with_suffix(contacts_path.suffix + ".match.manifest.json")

    started = time.time()
    rows = read_contacts(contacts_path)
    candidates = load_candidates(candidates_path)
    stats = apply_matching(rows, candidates)
    written = write_contacts(contacts_path, rows)

    manifest = {
        "primitive": "match_local_candidates",
        "command": "match",
        "started_at": now_iso(),
        "elapsed_ms": int((time.time() - started) * 1000),
        "contacts_path": str(contacts_path),
        "candidates_path": str(candidates_path),
        "candidates_loaded": len(candidates),
        "rows_written": written,
        "manifest_path": str(manifest_path),
        "stats": stats,
    }
    write_json(manifest_path, manifest)
    emit(manifest)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Match contacts CSV against Powerset candidate CSV")
    sub = parser.add_subparsers(dest="command", required=True)
    match = sub.add_parser("match", help="Apply local matching and update contacts CSV in place")
    match.add_argument("--contacts", required=True, help="Path to the message-contacts CSV")
    match.add_argument("--candidates", default="powerset_contacts.csv",
                       help="Path to the Powerset candidate CSV (from sync_powerset_candidates)")
    match.add_argument("--manifest", help="Path to write the run manifest JSON")
    match.set_defaults(func=cmd_match)
    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
