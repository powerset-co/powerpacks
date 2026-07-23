#!/usr/bin/env python3
"""Local name matcher between message contacts and an explicit people catalog.

Stdlib-only port of `contact_exporter.matching.apply_local_name_matching`.

Tiers (highest precedence first):

0. Unique exact phone match (E.164/last-10-digits) → matched, confidence 1.0;
   unique exact email match on an email handle → matched, confidence 1.0.
   These run before name tiers and work for contacts with no name at all.
1. Single exact normalized-name match → matched, confidence 1.0
2. Multiple exact normalized-name matches → suggested, confidence 0.80
3. Single-token first-name-only match → suggested, never matched
4. Same last-name pool with a unique first-name prefix candidate → matched
5. Same last-name pool with multiple prefix candidates → suggested (best score)
6. Fuzzy ratio in same-last-name pool ≥ 0.94 with margin ≥ 0.05 → matched
7. Fuzzy ratio ≥ 0.80 → suggested
8. Otherwise unmatched

Candidates come from the local merged people CSV (`--local-people`) by default;
the canonical `$import-messages` flow supplies already-imported Gmail and
LinkedIn `people.csv` rows through it, and no external candidate catalog is
loaded otherwise. Callers may deliberately union an additional catalog with
`--candidates`.

Usage:
    match_local_candidates.py match \
        --contacts .powerpacks/messages/contacts.csv \
        --local-people .powerpacks/messages/_local_people.csv \
        [--candidates PATH] [--review PATH] [--manifest PATH]

A manifest JSON is written next to the contacts CSV with
`stats: {total, matched, suggested, unmatched}`.

Approval gate: identifier matches never expand the user's approved set on
their own. `matched` from tier 0 is only emitted for contacts the user
already approved in the research review (`in_network=true`); contacts the
user reviewed without approving are left untouched by tier 0; contacts that
were never reviewed get at most `suggested`, which requires human approval
before import.

Known gap: the approvals input (`research_review.csv`) has no living
producer, so on a fresh install the tier-0 gate can never pass and every
identifier match demotes to `suggested`. The replacement approval surface
belongs to $deep-context (suggestions review / conservative auto-attach);
until it exists, matched-people attachment effectively requires that legacy
file.

Updates the message-contacts CSV in place with the
`match_status / matched_person_id / matched_name / matched_linkedin_url /
match_confidence / match_method / match_reason` columns.

Changelog:
  2026-07-23 (audit):
    - match_local_candidates.README.md sidecar folded into this docstring.
    - The research_review.csv producer (the research-review flow) was retired
      in #315, opening the known gap above.
    - Moved from primitives/match_local_candidates/ into
      imports/messages/; the duplicated try/except import
      block became the single repo-root bootstrap stanza.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

# Repo-root bootstrap so packs.* imports work in module AND script mode
# (uv run .../match_local_candidates.py); must be in-file because script mode
# never imports the package __init__.
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import emit, now_iso, write_json  # noqa: E402
from packs.shared.csv_io import CsvIO  # noqa: E402


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
DEFAULT_LOCAL_PEOPLE = Path(".powerpacks/network-import/merged/people.csv")
DEFAULT_REVIEW_CSV = Path(".powerpacks/messages/research_review.csv")
SCHEMA_DOC = "packs/ingestion/schemas/contacts-csv.md"
SCHEMA_JSON = "packs/ingestion/schemas/contacts-csv.schema.json"


@dataclass
class Candidate:
    id: str
    name: str
    linkedin_url: str | None = None
    phone_number: str | None = None
    public_identifier: str | None = None
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_name(raw: str | None) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", (raw or "").strip().lower())
    return re.sub(r"\s+", " ", s).strip()


def phone_match_key(raw: str | None) -> str:
    """Digits-only phone key; 10+ digit numbers compare by their last 10 so
    +14155550123, 14155550123, and 4155550123 all collide."""
    digits = re.sub(r"\D+", "", raw or "")
    if len(digits) < 7:
        return ""
    return digits[-10:] if len(digits) >= 10 else digits


def email_match_key(raw: str | None) -> str:
    value = (raw or "").strip().lower()
    return value if "@" in value else ""


def parse_listish(raw: str | None) -> list[str]:
    value = (raw or "").strip()
    if not value:
        return []
    if value.startswith("["):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    return [part.strip() for part in re.split(r"[;,]", value) if part.strip()]


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
        reader = CsvIO.dict_reader(handle)
        for row in reader:
            cid = (row.get("id") or "").strip()
            name = (row.get("name") or "").strip()
            if not cid or not name:
                continue
            emails_raw = (row.get("emails") or "").strip()
            emails = [e for e in emails_raw.split(";") if e]
            phone = (row.get("phone_number") or "").strip()
            out.append(Candidate(
                id=cid,
                name=name,
                linkedin_url=(row.get("linkedin_url") or "").strip() or None,
                phone_number=phone or None,
                public_identifier=(row.get("public_identifier") or "").strip() or None,
                emails=emails,
                phones=[phone] if phone else [],
            ))
    return out


def load_review_approvals(path: Path) -> dict[str, bool] | None:
    """Map contact identifier keys (phone/email) -> approved (in_network) from
    the research review. None when no review exists (nothing reviewed yet)."""
    if not path.exists():
        return None
    approvals: dict[str, bool] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in CsvIO.dict_reader(handle):
            approved = (row.get("in_network") or "").strip().lower() in {"true", "yes", "1"}
            for raw in [row.get("phone_e164"), row.get("handle")]:
                key = email_match_key(raw) or phone_match_key(raw)
                if key:
                    # Any approved row wins over an unapproved duplicate.
                    approvals[key] = approvals.get(key, False) or approved
    return approvals


def load_people_candidates(path: Path, known_ids: set[str], known_identifiers: set[str]) -> list[Candidate]:
    """Load merged people.csv, skipping entries already in an explicit catalog."""
    if not path.exists():
        return []
    out: list[Candidate] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = CsvIO.dict_reader(handle)
        for row in reader:
            cid = (row.get("id") or "").strip()
            name = (row.get("full_name") or "").strip()
            if not cid or cid in known_ids:
                continue
            public_identifier = (row.get("public_identifier") or "").strip().lower()
            if public_identifier and public_identifier in known_identifiers:
                continue
            phones = parse_listish(row.get("all_phones")) or parse_listish(row.get("primary_phone"))
            emails = [email.lower() for email in (parse_listish(row.get("all_emails")) or parse_listish(row.get("primary_email")))]
            if not name and not phones and not emails:
                continue
            out.append(Candidate(
                id=cid,
                name=name,
                linkedin_url=(row.get("linkedin_url") or "").strip() or None,
                phone_number=phones[0] if phones else None,
                public_identifier=public_identifier or None,
                emails=emails,
                phones=phones,
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

def apply_matching(
    rows: list[dict[str, str]],
    candidates: list[Candidate],
    approvals: dict[str, bool] | None = None,
) -> dict[str, int]:
    if not rows:
        return {"total": 0, "matched": 0, "suggested": 0, "unmatched": 0}
    if not candidates:
        for row in rows:
            _set_unmatched(row, "no local candidate catalog available")
        return {"total": len(rows), "matched": 0, "suggested": 0, "unmatched": len(rows)}

    exact_index: dict[str, list[Candidate]] = {}
    last_name_index: dict[str, list[Candidate]] = {}
    first_name_index: dict[str, list[Candidate]] = {}
    phone_index: dict[str, list[Candidate]] = {}
    email_index: dict[str, list[Candidate]] = {}
    for c in candidates:
        for phone in c.phones or ([c.phone_number] if c.phone_number else []):
            key = phone_match_key(phone)
            if key and not any(existing.id == c.id for existing in phone_index.get(key, [])):
                phone_index.setdefault(key, []).append(c)
        for email in c.emails:
            key = email_match_key(email)
            if key and not any(existing.id == c.id for existing in email_index.get(key, [])):
                email_index.setdefault(key, []).append(c)
        norm = normalize_name(c.name)
        if not norm:
            continue
        exact_index.setdefault(norm, []).append(c)
        parts = norm.split(" ")
        if len(parts) >= 2:
            last_name_index.setdefault(parts[-1], []).append(c)
            first_name_index.setdefault(parts[0], []).append(c)
    for index in (exact_index, last_name_index, first_name_index, phone_index, email_index):
        for bucket in index.values():
            bucket.sort(key=lambda c: c.id)

    matched = suggested = unmatched = 0

    for row in rows:
        # Tier 0: identifier matches run before name tiers and work for
        # contacts with no usable name (the largest unmatched bucket).
        # Approval gate: identifier matches never expand the approved set.
        # approved=True -> matched allowed; approved=False (user reviewed and
        # did not approve) -> tier 0 skips entirely; not reviewed yet -> at
        # most suggested, which requires human approval before import.
        handle = (row.get("phone") or "").strip()
        email_key = email_match_key(handle)
        identifier_key = email_key or phone_match_key(handle)
        approved = approvals.get(identifier_key) if approvals is not None else None
        identifier_pool = [] if (approvals is not None and approved is False) else (
            email_index.get(email_key, []) if email_key else phone_index.get(phone_match_key(handle), [])
        )
        if len(identifier_pool) == 1:
            method = "email_exact" if email_key else "phone_exact"
            if approved:
                matched += 1
                _set_match(row, status="matched", candidate=identifier_pool[0], confidence=1.0,
                           method=method, reason="unique exact identifier match (approved contact)")
            else:
                suggested += 1
                _set_match(row, status="suggested", candidate=identifier_pool[0], confidence=0.95,
                           method=method, reason="unique exact identifier match awaiting approval")
            continue
        if len(identifier_pool) > 1:
            suggested += 1
            method = "email_exact_ambiguous" if email_key else "phone_exact_ambiguous"
            _set_match(row, status="suggested", candidate=identifier_pool[0], confidence=0.90,
                       method=method, reason=f"{len(identifier_pool)} candidates share this identifier")
            continue

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

    # Approval gate, applied to every tier: once the user has reviewed
    # (a research review exists), a match may only carry `matched` status for
    # contacts the user approved. Anything else — including name-tier matches
    # against newly added local candidates — demotes to `suggested` so it goes
    # back through review instead of silently expanding the approved set.
    if approvals is not None:
        for row in rows:
            if row.get("match_status") != "matched":
                continue
            handle = (row.get("phone") or "").strip()
            key = email_match_key(handle) or phone_match_key(handle)
            if not approvals.get(key):
                row["match_status"] = "suggested"
                row["match_reason"] = (row.get("match_reason") or "").rstrip() + " (awaiting approval)"
        matched = sum(1 for row in rows if row.get("match_status") == "matched")
        suggested = sum(1 for row in rows if row.get("match_status") == "suggested")
        unmatched = sum(1 for row in rows if row.get("match_status") == "unmatched")

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
        reader = CsvIO.dict_reader(handle)
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
    candidates_path = Path(args.candidates) if args.candidates else None
    manifest_path = Path(args.manifest) if args.manifest else contacts_path.with_suffix(contacts_path.suffix + ".match.manifest.json")

    started = time.time()
    rows = read_contacts(contacts_path)
    candidates = load_candidates(candidates_path) if candidates_path else []
    local_people_path = Path(args.local_people) if args.local_people else DEFAULT_LOCAL_PEOPLE
    local_candidates: list[Candidate] = []
    if not args.no_local_people:
        known_ids = {c.id for c in candidates}
        known_identifiers = {(c.public_identifier or "").lower() for c in candidates if c.public_identifier}
        local_candidates = load_people_candidates(local_people_path, known_ids, known_identifiers)
    review_path = Path(args.review) if args.review else DEFAULT_REVIEW_CSV
    approvals = load_review_approvals(review_path)
    stats = apply_matching(rows, candidates + local_candidates, approvals=approvals)
    written = write_contacts(contacts_path, rows)

    manifest = {
        "primitive": "match_local_candidates",
        "command": "match",
        "started_at": now_iso(),
        "elapsed_ms": int((time.time() - started) * 1000),
        "contacts_path": str(contacts_path),
        "candidates_path": str(candidates_path) if candidates_path else "",
        "candidates_loaded": len(candidates) + len(local_candidates),
        "explicit_catalog_candidates": len(candidates),
        "local_people_candidates": len(local_candidates),
        "local_people_path": str(local_people_path) if local_candidates else "",
        "review_path": str(review_path) if approvals is not None else "",
        "approved_contacts": sum(1 for value in (approvals or {}).values() if value),
        "rows_written": written,
        "manifest_path": str(manifest_path),
        "stats": stats,
    }
    write_json(manifest_path, manifest)
    emit(manifest)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Match message contacts against local people")
    sub = parser.add_subparsers(dest="command", required=True)
    match = sub.add_parser("match", help="Apply local matching and update contacts CSV in place")
    match.add_argument("--contacts", required=True, help="Path to the message-contacts CSV")
    match.add_argument("--candidates",
                       help="Optional additional candidate CSV; omitted by the canonical local-only flow")
    match.add_argument("--local-people", help="Local merged people CSV to union into the candidate catalog "
                       f"(default: {DEFAULT_LOCAL_PEOPLE} when present)")
    match.add_argument("--no-local-people", action="store_true", help="Match only against an explicit --candidates catalog")
    match.add_argument("--review", help="Research review CSV holding the user's in_network approvals "
                       f"(default: {DEFAULT_REVIEW_CSV} when present)")
    match.add_argument("--manifest", help="Path to write the run manifest JSON")
    match.set_defaults(func=cmd_match)
    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
