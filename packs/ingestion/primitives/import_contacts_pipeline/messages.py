#!/usr/bin/env python3
"""Import matched Messages contacts + research candidates (contacts-direct).

Reads the match-annotated `.powerpacks/messages/contacts.csv` and materializes
two stage outputs, with no LLM, no research queue, and no enrichment call:

- `import/messages/people.csv` — contacts MATCHED to an existing network
  person (message activity attaches to that person at fan-in).
- `import/messages/candidates.csv` — unmatched contacts passing a
  deterministic "worth researching" floor (real phone, plausibly-real saved
  name, message-count minimum). Identity resolution happens later in the
  deep-context processing layer with cross-channel context.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

try:
    from packs.ingestion.schemas.people_schema import (
        PEOPLE_SCHEMA_COLUMNS,
        extract_public_identifier,
        latest_interaction,
        merge_interaction_counts,
        normalize_linkedin_url,
        normalize_people_row,
    )
    from packs.ingestion.schemas.candidates_schema import (
        CANDIDATES_SCHEMA_COLUMNS,
        candidate_key_for,
        normalize_candidate_row,
    )
    from packs.ingestion.primitives.discover_contacts_pipeline.common import (
        DEFAULT_BASE_DIR,
        DEFAULT_DIRECTORY_CSV,
        emit,
        read_csv_rows,
        read_accounts,
        sha,
        unique_strings,
        write_csv_rows,
    )
    from packs.ingestion.primitives.discover_contacts_pipeline.directory import (
        DIRECTORY_COLUMNS,
        directory_rows_from_people_csv,
        merge_directory_rows,
        normalized_directory_row,
        people_directory_source_key,
        phones_from_value,
    )
    from packs.ingestion.primitives.import_contacts_pipeline.common import (
        DEFAULT_ACCOUNTS,
        DEFAULT_IMPORT_DIR,
        csv_count,
        directory_row_matches_source,
        directory_source_account_quality,
        import_manifest_current,
        normalize_directory_source_accounts,
        write_manifest,
    )
    from packs.shared.csv_io import CsvIO
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.schemas.people_schema import (
        PEOPLE_SCHEMA_COLUMNS,
        extract_public_identifier,
        latest_interaction,
        merge_interaction_counts,
        normalize_linkedin_url,
        normalize_people_row,
    )
    from packs.ingestion.schemas.candidates_schema import (
        CANDIDATES_SCHEMA_COLUMNS,
        candidate_key_for,
        normalize_candidate_row,
    )
    from packs.ingestion.primitives.discover_contacts_pipeline.common import (
        DEFAULT_BASE_DIR,
        DEFAULT_DIRECTORY_CSV,
        emit,
        read_csv_rows,
        read_accounts,
        sha,
        unique_strings,
        write_csv_rows,
    )
    from packs.ingestion.primitives.discover_contacts_pipeline.directory import (
        DIRECTORY_COLUMNS,
        directory_rows_from_people_csv,
        merge_directory_rows,
        normalized_directory_row,
        people_directory_source_key,
        phones_from_value,
    )
    from packs.ingestion.primitives.import_contacts_pipeline.common import (
        DEFAULT_ACCOUNTS,
        DEFAULT_IMPORT_DIR,
        csv_count,
        directory_row_matches_source,
        directory_source_account_quality,
        import_manifest_current,
        normalize_directory_source_accounts,
        write_manifest,
    )
    from packs.shared.csv_io import CsvIO


TRUTHY = {"1", "true", "yes", "y", "on"}
FALSY = {"0", "false", "no", "n", "off"}
MESSAGES_IMPORT_CONTRACT = "messages-contacts-direct-v4"
WORKING_CONTACTS_CSV = Path(".powerpacks/messages/contacts.csv")
MATCH_MANIFEST_JSON = Path(".powerpacks/messages/contacts.csv.match.manifest.json")
DEFAULT_MIN_MESSAGE_COUNT = 1
# Group-appearance-only contacts below this volume are low-signal noise
# (someone from a group thread, not a relationship) unless opted in. A positive
# WhatsApp direct-chat count is explicit relationship evidence and bypasses it.
GROUP_ONLY_MIN_MESSAGES = 10

# Deterministic "worth researching" floor: the historical pre-LLM eligibility
# ruleset from the retired queue-preparer primitive, copied rather than
# imported because that primitive is orphaned from the runtime path and this
# floor is now THIS stage's contract.
MIN_NAME_TOKENS = 2
MIN_TOKEN_LEN = 2
MIN_TOTAL_ALPHA = 5
BLOCKED_LAST_NAME_TOKENS = {"hinge", "raya", "tinder", "bumble"}
NAME_CLEAN_RE = re.compile(r"[^A-Za-zÀ-ÿ'’\-\s]")
MULTISPACE_RE = re.compile(r"\s+")
MIN_PHONE_DIGITS = 10
MAX_PHONE_DIGITS = 15


def normalize_bool(value: Any) -> bool | None:
    raw = str(value or "").strip().lower()
    if raw in TRUTHY:
        return True
    if raw in FALSY:
        return False
    return None


def parse_int_field(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def split_full_name(full_name: str) -> tuple[str, str]:
    parts = (full_name or "").strip().split(None, 1)
    if not parts:
        return "", ""
    return parts[0], parts[1] if len(parts) > 1 else ""


def normalize_name(name: str) -> str:
    cleaned = NAME_CLEAN_RE.sub(" ", name or "")
    return MULTISPACE_RE.sub(" ", cleaned).strip()


def normalize_last_name_tokens(name: str) -> set[str]:
    cleaned = normalize_name(name).lower()
    parts = cleaned.split()
    if len(parts) < 2:
        return set()
    return {token for token in parts[1:] if token}


def normalize_phoneish(value: str) -> str:
    return "".join(ch for ch in value or "" if ch.isdigit())


def has_searchable_name(name: str) -> bool:
    cleaned = normalize_name(name)
    if not cleaned:
        return False
    tokens = [t for t in cleaned.split(" ") if len(t) >= MIN_TOKEN_LEN]
    if len(tokens) < MIN_NAME_TOKENS:
        return False
    alpha = sum(1 for ch in cleaned if ch.isalpha())
    return alpha >= MIN_TOTAL_ALPHA


def bad_name_reason(name: str, phone: str = "") -> str:
    phone_digits = normalize_phoneish(phone)
    raw_name_digits = normalize_phoneish(name)
    if phone_digits and raw_name_digits and phone_digits.endswith(raw_name_digits):
        return "name_is_phone"
    cleaned = normalize_name(name)
    if not cleaned:
        return "no_name"
    if normalize_last_name_tokens(cleaned) & BLOCKED_LAST_NAME_TOKENS:
        return "blocked_name_token"
    if not has_searchable_name(cleaned):
        return "bad_name"
    return ""


def has_whatsapp_direct_messages(row: dict[str, str]) -> bool:
    """Whether discovery found a real WhatsApp direct-message thread."""
    return parse_int_field(row.get("whatsapp_message_count")) > 0


def contact_floor_reason(
    row: dict[str, str],
    *,
    min_message_count: int,
    include_group_only: bool,
) -> str:
    """First failing floor reason for an unmatched contact ("" = passes)."""
    phone = (row.get("phone") or "").strip()
    if "@" in phone:
        return "email_handle"
    digits = normalize_phoneish(phone)
    if not (MIN_PHONE_DIGITS <= len(digits) <= MAX_PHONE_DIGITS):
        return "short_code_or_invalid_phone"
    if normalize_bool(row.get("skip", "")) is True:
        return "skip_flag"
    name_reason = bad_name_reason(row.get("name") or "", phone)
    if name_reason:
        return name_reason
    message_count = parse_int_field(row.get("message_count"))
    if message_count < min_message_count:
        return "below_min_messages"
    if (
        not include_group_only
        and normalize_bool(row.get("is_in_group_chats", "")) is True
        and not has_whatsapp_direct_messages(row)
        and message_count < GROUP_ONLY_MIN_MESSAGES
    ):
        return "group_only_low_signal"
    return ""


def messages_source_channels(row: dict[str, str]) -> list[str]:
    channels: list[str] = []
    raw = str(row.get("source") or row.get("message_source") or "").strip().lower()
    for token in re.split(r"[,|+/;\s]+", raw):
        if token in {"imessage", "whatsapp"} and token not in channels:
            channels.append(token)
    for key, channel in (
        ("imessage_message_count", "imessage"),
        ("whatsapp_message_count", "whatsapp"),
    ):
        if parse_int_field(row.get(key)) > 0 and channel not in channels:
            channels.append(channel)
    return channels or ["messages"]


def contact_interaction_counts(row: dict[str, str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for count_key, channel in (
        ("imessage_message_count", "imessage"),
        ("whatsapp_message_count", "whatsapp"),
    ):
        count = parse_int_field(row.get(count_key))
        if count > 0:
            counts[channel] = count
    return counts


def contact_last_interaction(row: dict[str, str]) -> str:
    return latest_interaction(
        row.get("imessage_last_message"),
        row.get("whatsapp_last_message"),
        row.get("last_message"),
    )


def contact_row_to_messages_people(
    row: dict[str, str],
    contacts_csv: Path,
) -> dict[str, str]:
    """Map a MATCHED contacts.csv row onto the canonical people schema."""
    linkedin_url = normalize_linkedin_url(row.get("matched_linkedin_url") or "")
    public_identifier = extract_public_identifier(linkedin_url)
    full_name = (row.get("matched_name") or "").strip() or (row.get("name") or "").strip()
    first_name, last_name = split_full_name(full_name)
    phone = (row.get("phone") or "").strip()
    is_email_handle = "@" in phone
    summary_parts = ["selection=matched"]
    if row.get("match_method"):
        summary_parts.append(f"match_method={row.get('match_method')}")
    interaction_counts = contact_interaction_counts(row)
    people = {
        "id": (row.get("matched_person_id") or "").strip()
        or f"message-linkedin:{sha(public_identifier or linkedin_url, 16)}",
        "public_identifier": public_identifier,
        "linkedin_url": linkedin_url,
        "first_name": first_name,
        "last_name": last_name,
        "full_name": full_name,
        "summary": "; ".join(summary_parts),
        # Deliberately blank so the fan-in merge keeps the enriched source
        # row's provider (including the `synthetic` keep-gate token).
        "enrichment_provider": "",
        "primary_email": phone if is_email_handle else "",
        "all_emails": json.dumps([phone], ensure_ascii=False) if is_email_handle else "",
        "primary_phone": "" if is_email_handle else phone,
        "all_phones": (
            "" if is_email_handle or not phone else json.dumps([phone], ensure_ascii=False)
        ),
        "source_channels": ",".join(messages_source_channels(row)),
        "source_artifacts": str(contacts_csv),
        "interaction_counts": (
            json.dumps(interaction_counts, ensure_ascii=False) if interaction_counts else ""
        ),
        "last_interaction": contact_last_interaction(row),
    }
    return normalize_people_row(people)


def contact_row_to_candidate(
    row: dict[str, str],
    contacts_csv: Path,
) -> dict[str, str]:
    """Map a floor-passing UNMATCHED contacts.csv row onto the candidates schema."""
    phone = (row.get("phone") or "").strip()
    channels = messages_source_channels(row)
    counts = contact_interaction_counts(row)
    # Single primary channel by DM volume (ties -> first listed channel).
    source = max(counts, key=lambda ch: counts[ch]) if counts else channels[0]
    evidence: dict[str, Any] = {
        "channels": channels,
        "message_count": parse_int_field(row.get("message_count")),
        "is_in_group_chats": normalize_bool(row.get("is_in_group_chats", "")) is True,
        "source_artifacts": str(contacts_csv),
    }
    if (row.get("match_status") or "").strip().lower() == "suggested":
        evidence["suggested_person_id"] = (row.get("matched_person_id") or "").strip()
        evidence["suggested_name"] = (row.get("matched_name") or "").strip()
        evidence["suggested_linkedin_url"] = normalize_linkedin_url(
            row.get("matched_linkedin_url") or ""
        )
        if row.get("match_confidence"):
            evidence["match_confidence"] = row.get("match_confidence")
    candidate = {
        "candidate_key": candidate_key_for("", phone),
        "source": source,
        "full_name": (row.get("name") or "").strip(),
        "primary_phone": phone,
        "all_phones": json.dumps([phone], ensure_ascii=False) if phone else "",
        "interaction_counts": json.dumps(counts, ensure_ascii=False) if counts else "",
        "last_interaction": contact_last_interaction(row),
        "evidence": evidence,
    }
    return normalize_candidate_row(candidate)


def merge_messages_people_candidate(
    existing: dict[str, str],
    incoming: dict[str, str],
) -> dict[str, str]:
    merged = dict(existing)
    for key in (
        "primary_phone",
        "primary_email",
        "full_name",
        "first_name",
        "last_name",
        "headline",
        "summary",
        "city",
        "country",
        "current_title",
        "current_company",
    ):
        if not merged.get(key) and incoming.get(key):
            merged[key] = incoming[key]
    phones = unique_strings([
        *phones_from_value(merged.get("all_phones", "")),
        *phones_from_value(merged.get("primary_phone", "")),
        *phones_from_value(incoming.get("all_phones", "")),
        *phones_from_value(incoming.get("primary_phone", "")),
    ])
    if phones:
        merged["primary_phone"] = merged.get("primary_phone") or phones[0]
        merged["all_phones"] = json.dumps(phones, ensure_ascii=False)
    channels = unique_strings(
        (merged.get("source_channels", "").split(",") if merged.get("source_channels") else [])
        + (incoming.get("source_channels", "").split(",") if incoming.get("source_channels") else [])
    )
    if channels:
        merged["source_channels"] = ",".join(channels)
    providers = unique_strings(
        [merged.get("enrichment_provider", ""), incoming.get("enrichment_provider", "")]
    )
    if providers:
        merged["enrichment_provider"] = ",".join(providers)
    counts = merge_interaction_counts(
        merged.get("interaction_counts"),
        incoming.get("interaction_counts"),
    )
    merged["interaction_counts"] = json.dumps(counts, ensure_ascii=False) if counts else ""
    merged["last_interaction"] = latest_interaction(
        merged.get("last_interaction"), incoming.get("last_interaction")
    )
    return normalize_people_row(merged)


def selected_contacts_people(
    contacts_csv: Path,
    *,
    min_message_count: int = DEFAULT_MIN_MESSAGE_COUNT,
    include_group_only: bool = False,
) -> tuple[dict[str, Any], list[dict[str, str]], list[dict[str, str]]]:
    """Split matched contacts into people rows and floor-passing unmatched
    contacts into candidate rows."""
    if not contacts_csv.exists():
        return ({
            "contacts_csv": str(contacts_csv),
            "total_rows": 0,
            "people_rows": 0,
            "candidate_rows": 0,
            "selection_counts": {},
            "skipped": {"missing_contacts_csv": 1},
        }, [], [])
    _fields, rows = read_csv_rows(contacts_csv)
    people_by_key: dict[str, dict[str, str]] = {}
    candidates_by_key: dict[str, dict[str, str]] = {}
    selection_counts: dict[str, int] = {}
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for row in rows:
        match_status = (row.get("match_status") or "").strip().lower()
        matched_person_id = (row.get("matched_person_id") or "").strip()
        if match_status == "matched" and matched_person_id:
            candidate = contact_row_to_messages_people(row, contacts_csv)
            key = candidate.get("public_identifier") or candidate.get("id", "")
            if key in people_by_key:
                skip("duplicate_matched_person")
                people_by_key[key] = merge_messages_people_candidate(
                    people_by_key[key], candidate
                )
            else:
                people_by_key[key] = candidate
                selection_counts["matched"] = selection_counts.get("matched", 0) + 1
            continue
        if match_status == "suggested":
            # Never auto-attach a suggestion (that was the review gate);
            # the deep-context cluster judge decides. Recorded in evidence.
            skip("suggested_not_attached")
        reason = contact_floor_reason(
            row,
            min_message_count=min_message_count,
            include_group_only=include_group_only,
        )
        if reason:
            skip(reason)
            continue
        candidate_row = contact_row_to_candidate(row, contacts_csv)
        key = candidate_row.get("candidate_key", "")
        if not key:
            skip("short_code_or_invalid_phone")
            continue
        if key in candidates_by_key:
            skip("duplicate_phone")
            continue
        candidates_by_key[key] = candidate_row
        selection_counts["phone_only"] = selection_counts.get("phone_only", 0) + 1

    people_rows = [people_by_key[key] for key in sorted(people_by_key)]
    candidate_rows = [candidates_by_key[key] for key in sorted(candidates_by_key)]
    summary = {
        "contacts_csv": str(contacts_csv),
        "total_rows": len(rows),
        "people_rows": len(people_rows),
        "candidate_rows": len(candidate_rows),
        "selection_counts": selection_counts,
        "skipped": skipped,
    }
    return summary, people_rows, candidate_rows


def directory_source_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    keys: set[str] = set()
    for row in read_csv_rows(path)[1]:
        normalized = normalized_directory_row(row, source="directory")
        if normalized.get("source_key"):
            keys.add(normalized["source_key"])
    return keys


def messages_people_directory_keys(rows: list[dict[str, str]]) -> set[str]:
    keys: set[str] = set()
    for row in rows:
        public_identifier = row.get("public_identifier") or extract_public_identifier(
            row.get("linkedin_url") or ""
        )
        if not public_identifier:
            continue
        key = people_directory_source_key(
            row,
            "messages",
            row.get("source_channels") or "messages",
            public_identifier,
        )
        if key:
            keys.add(key)
    return keys


def existing_csv_column(path: Path, column: str) -> set[str]:
    if not path.exists():
        return set()
    return {
        (row.get(column) or "").strip()
        for row in read_csv_rows(path)[1]
        if (row.get(column) or "").strip()
    }


def messages_import_diff(
    contacts_csv: Path,
    import_dir: Path,
    *,
    min_message_count: int = DEFAULT_MIN_MESSAGE_COUNT,
    include_group_only: bool = False,
) -> dict[str, Any]:
    materialized, people_rows, candidate_rows = selected_contacts_people(
        contacts_csv,
        min_message_count=min_message_count,
        include_group_only=include_group_only,
    )
    people_ids = {row.get("id", "") for row in people_rows if row.get("id")}
    candidate_keys = {
        row.get("candidate_key", "") for row in candidate_rows if row.get("candidate_key")
    }
    existing_people_ids = existing_csv_column(import_dir / "people.csv", "id")
    existing_candidate_keys = existing_csv_column(
        import_dir / "candidates.csv", "candidate_key"
    )
    new_people = len(people_ids - existing_people_ids)
    new_candidates = len(candidate_keys - existing_candidate_keys)
    return {
        "materialized": materialized,
        "people_rows": len(people_ids),
        "candidate_rows": len(candidate_keys),
        "new_people": new_people,
        "new_candidates": new_candidates,
        "new_rows": new_people + new_candidates,
    }


def people_csv_schema_stale(path: Path) -> bool:
    """True when an existing people.csv predates the interaction-count
    columns. Input fingerprints can't catch this (the code changed, not the
    data), so the import self-invalidates instead of trusting its manifest."""
    if not path.exists():
        return False
    with path.open(newline="", encoding="utf-8") as handle:
        header = next(CsvIO.reader(handle), [])
    return bool(header) and "interaction_counts" not in header


def replace_messages_directory_rows(
    people_csv: Path,
    directory_csv: Path | None = None,
) -> dict[str, Any]:
    directory_csv = directory_csv or DEFAULT_DIRECTORY_CSV
    retained: dict[str, dict[str, str]] = {}
    existing_rows = read_csv_rows(directory_csv)[1] if directory_csv.exists() else []
    removed_rows = 0
    for row in existing_rows:
        normalized = normalized_directory_row(row, source="directory")
        if not normalized:
            continue
        if directory_row_matches_source(normalized, "messages") or normalized.get(
            "source_key", ""
        ).startswith("messages:"):
            removed_rows += 1
            continue
        retained[normalized["source_key"]] = normalized
    incoming = directory_rows_from_people_csv(people_csv, source="messages")
    merged = merge_directory_rows(incoming, retained)
    write_csv_rows(directory_csv, DIRECTORY_COLUMNS, merged)
    return {
        "path": str(directory_csv),
        "existing_rows": len(existing_rows),
        "removed_messages_rows": removed_rows,
        "imported_messages_rows": len(incoming),
        "rows": len(merged),
    }


def run(args: argparse.Namespace) -> dict:
    import_dir = DEFAULT_IMPORT_DIR / "messages"
    people_csv_path = import_dir / "people.csv"
    schema_stale = people_csv_schema_stale(people_csv_path)
    min_message_count = int(getattr(args, "min_message_count", DEFAULT_MIN_MESSAGE_COUNT))
    include_group_only = bool(getattr(args, "include_group_only", False))
    expected_input = {
        "pipeline_contract": MESSAGES_IMPORT_CONTRACT,
        "mode": "contacts-direct",
        "min_message_count": min_message_count,
        "include_group_only": include_group_only,
    }
    current = None if schema_stale else import_manifest_current(
        "messages",
        expected_input,
        import_dir=DEFAULT_IMPORT_DIR,
    )
    if current:
        return current
    read_accounts(args.accounts)
    contacts_csv = WORKING_CONTACTS_CSV
    manifest_input = {
        **expected_input,
        "contacts_csv": str(contacts_csv),
        "match_manifest": str(MATCH_MANIFEST_JSON),
        "discovery_manifest": str(DEFAULT_BASE_DIR / "discover" / "messages" / "manifest.json"),
    }
    if not contacts_csv.exists():
        return write_manifest("messages", {
            "status": "failed",
            "reason": "messages_contacts_missing",
            "message": (
                f"Discover Messages contacts before import: {contacts_csv}. "
                "Run: uv run --project . python packs/ingestion/primitives/"
                "discover_contacts_pipeline/messages.py discover"
            ),
            "input": manifest_input,
            "outputs": {},
            "stats": {"people": 0, "candidates": 0},
        }, import_dir=DEFAULT_IMPORT_DIR)
    if not MATCH_MANIFEST_JSON.exists() and not args.allow_unmatched:
        return write_manifest("messages", {
            "status": "failed",
            "reason": "messages_contacts_not_matched",
            "message": (
                "Match contacts against your network before import (or pass "
                "--allow-unmatched). Run: uv run --project . python packs/ingestion/"
                f"primitives/match_local_candidates/match_local_candidates.py match "
                f"--contacts {contacts_csv}"
            ),
            "input": manifest_input,
            "outputs": {},
            "stats": {"people": 0, "candidates": 0},
        }, import_dir=DEFAULT_IMPORT_DIR)
    diff = messages_import_diff(
        contacts_csv,
        import_dir,
        min_message_count=min_message_count,
        include_group_only=include_group_only,
    )
    if diff["new_rows"] > 0 and not args.confirm_import:
        message = (
            f"Import Messages contacts: attach message activity to {diff['people_rows']} "
            f"matched people and add {diff['candidate_rows']} research candidates?"
        )
        return write_manifest("messages", {
            "status": "blocked_approval",
            "approval_type": "import_confirmation",
            "message": message,
            "blocked": {
                "status": "blocked_approval",
                "approval_type": "import_confirmation",
                "source": "messages",
                "message": message,
                "payload": diff,
            },
            "input": manifest_input,
            "outputs": {},
            "stats": {
                "people": 0,
                "candidates": diff["candidate_rows"],
            },
            "diff": diff,
        }, import_dir=DEFAULT_IMPORT_DIR)
    materialized, people_rows, candidate_rows = selected_contacts_people(
        contacts_csv,
        min_message_count=min_message_count,
        include_group_only=include_group_only,
    )
    import_dir.mkdir(parents=True, exist_ok=True)
    # Legacy review-era artifacts: no longer part of this stage's contract.
    legacy_input = import_dir / "people.input.csv"
    if legacy_input.exists():
        legacy_input.unlink()
    legacy_enrichment = import_dir / "enrichment"
    if legacy_enrichment.exists():
        shutil.rmtree(legacy_enrichment)
    write_csv_rows(people_csv_path, PEOPLE_SCHEMA_COLUMNS, people_rows)
    candidates_csv_path = import_dir / "candidates.csv"
    write_csv_rows(candidates_csv_path, CANDIDATES_SCHEMA_COLUMNS, candidate_rows)
    directory_replacement = replace_messages_directory_rows(people_csv_path)
    directory_normalization = normalize_directory_source_accounts("messages")
    directory_quality = directory_source_account_quality("messages")
    status = "completed" if directory_quality["status"] == "ok" else "failed"
    return write_manifest("messages", {
        "status": status,
        "reason": "directory_source_account_quality_failed" if status == "failed" else "",
        "input": manifest_input,
        "outputs": {
            "people_csv": str(people_csv_path),
            "candidates_csv": str(candidates_csv_path),
        },
        "stats": {
            "people": csv_count(str(people_csv_path)),
            "candidates": csv_count(str(candidates_csv_path)),
        },
        "diff": diff,
        "materialized": materialized,
        "directory": {
            "path": str(DEFAULT_DIRECTORY_CSV),
            "replacement": directory_replacement,
            "normalization": directory_normalization,
            "quality": directory_quality,
        },
    }, import_dir=DEFAULT_IMPORT_DIR)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import matched Messages contacts + research candidates"
    )
    parser.add_argument("command", choices=["run"])
    parser.add_argument("--accounts", type=Path, default=DEFAULT_ACCOUNTS)
    parser.add_argument("--operator-id", default="local")
    parser.add_argument("--confirm-import", action="store_true")
    parser.add_argument(
        "--min-message-count", type=int, default=DEFAULT_MIN_MESSAGE_COUNT,
        help="Minimum total DM messages for an unmatched contact to become a candidate",
    )
    parser.add_argument(
        "--include-group-only", action="store_true",
        help="Keep low-DM contacts that only appear via group chats",
    )
    parser.add_argument(
        "--allow-unmatched", action="store_true",
        help="Proceed without a match manifest (all contacts floor-tested as unmatched)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = run(args)
    emit(payload)
    return 20 if payload.get("status") == "blocked_approval" else 1 if payload.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
