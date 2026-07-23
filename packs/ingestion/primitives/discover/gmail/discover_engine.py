#!/usr/bin/env python3
"""Gmail discovery engine: msgvault aggregation -> local network artifacts.

The CLI child spawned per account by `gmail/discover.py discover` (which
`$import-gmail` runs). Reads msgvault metadata through
`gmail/msgvault_store.py` and writes Powerpacks-local artifacts; it never
reads Gmail message bodies, subjects, snippets, raw MIME, or attachments.
Product-flow docs: `packs/ingestion/docs/gmail-import-pipeline.md`.

Usage:
    discover_engine.py msgvault-accounts --db ~/.msgvault/msgvault.db
    discover_engine.py msgvault --db ~/.msgvault/msgvault.db --account-email me@gmail.com
    discover_engine.py apply-resolutions --people-csv PATH --resolutions-csv PATH

`msgvault` writes to `.powerpacks/network-import/discover/gmail/<account>/`:
`accounts.csv`, `gmail_threads.csv`, `gmail_contacts_aggregated.csv`,
`targeted_emails.csv`, `linkedin_resolution_queue.csv`, canonical `people.csv`
(`source_channels=gmail_msgvault`), and `manifest.json`. Automated/noreply
addresses are filtered unless `--include-automated`; Gmail category labels
(Social/Promotions/Forums/Updates) are excluded unless `--include-category-mail`.
Multiple Gmail accounts are separate msgvault source accounts: list them with
`msgvault-accounts`, then run `msgvault` once per `--account-email`
(`gmail/discover.py` loops the selected accounts).

`apply-resolutions` attaches a `linkedin_resolutions.csv` back onto a Gmail
`people.csv` (`--min-confidence` defaults to 0.75); the live caller is the
import chain's `gmail_apply_and_enrich` step
(`imports/gmail/import_steps.py`), which
applies STORED resolutions only.

Changelog:
  2026-07-23 (audit batch 17): split out of the retired
    `gmail/network_import.py` monolith — this module keeps artifact emission
    plus the argparse entry; the msgvault reader/aggregation moved to
    `gmail/msgvault_store.py`. Deleted with the split (no live consumers):
    the one-person seed cluster (`run`/`continue`/`approve`/`status`
    subcommands, OnePersonInput, make_artifacts, append_account), its
    `gmail-one` ledger machinery (load/save_ledger, step functions), the
    PipelineBlocked/PipelineFailed exceptions that only served it, and the
    never-honored `--operator-id` flag on `msgvault`. Generic helpers
    (emit/now_iso/read_json/write_json/source_slug) now come from the stage
    `common.py` instead of local duplicates.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.discover.common import (  # noqa: E402
    DEFAULT_BASE_DIR,
    GMAIL_INTERACTION_CALCULATION_VERSION,
    emit,
    now_iso,
    read_json,
    source_slug,
    write_json,
)
from packs.ingestion.primitives.discover.gmail.msgvault_store import (  # noqa: E402
    DEFAULT_MSGVAULT_DB,
    MsgvaultStore,
    classify_email,
    default_excluded_labels,
    domain_guess,
    has_round_trip_interaction,
    normalize_label_names,
    split_name,
)
from packs.ingestion.schemas.people_schema import (  # noqa: E402
    PEOPLE_SCHEMA_COLUMNS,
    generate_person_id as generate_linkedin_person_id,
    normalize_interaction_timestamp,
)
from packs.shared.csv_io import CsvIO  # noqa: E402

THREAD_COLUMNS = [
    "email",
    "display_name",
    "thread_id",
    "received_count",
    "sent_count",
    "message_count",
    "first_message_at",
    "last_message_at",
    "subject",
    "discovered_at",
]
AGGREGATED_COLUMNS = [
    "email",
    "display_name",
    "total_sent",
    "total_received",
    "total_messages",
    "one_to_one_sent",
    "one_to_one_received",
    "one_to_one_messages",
    "group_sent",
    "group_received",
    "group_messages",
    "one_to_one_thread_count",
    "group_thread_count",
    "thread_count",
    "first_interaction",
    "last_interaction",
    "sample_subjects",
]
TARGETED_COLUMNS = [
    "display_name",
    "primary_email",
    "primary_email_type",
    "all_emails",
    "email_count",
    "total_sent",
    "total_received",
    "total_messages",
    "one_to_one_sent",
    "one_to_one_received",
    "one_to_one_messages",
    "group_sent",
    "group_received",
    "group_messages",
    "one_to_one_thread_count",
    "group_thread_count",
    "thread_count",
    "first_interaction",
    "last_interaction",
    "is_duplicate",
    "potential_same_person_emails",
    "sample_subjects",
    "sample_calendar_titles",
]
LINKEDIN_RESOLUTION_QUEUE_COLUMNS = [
    "handle",
    "id",
    "account_emails",
    "source_ids",
    "display_name",
    "full_name",
    "primary_email",
    "company_guess",
    "primary_email_type",
    "total_messages",
    "thread_count",
    "last_interaction",
    "source",
    "source_channels",
]
LINKEDIN_RESOLUTION_COLUMNS = ["handle", "status", "linkedin_url", "confidence", "matched_name", "matched_headline", "evidence", "reasoning"]
ACCOUNT_COLUMNS = ["account_id", "account_email", "provider", "source", "added_at"]
PEOPLE_COLUMNS = list(PEOPLE_SCHEMA_COLUMNS)


def short_hash(value: str, length: int = 10) -> str:
    """Return the first `length` hex chars of the value's SHA-256."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def extract_public_identifier(linkedin_url: str) -> str:
    """Extract the lowercase /in/ public identifier from a LinkedIn URL.

    Kept local (NOT the people_schema version): this one does not
    percent-decode the identifier, and the stored-resolution apply path is
    pinned to that behavior."""
    match = re.search(r"linkedin\.com/in/([^/?#]+)", linkedin_url or "", re.IGNORECASE)
    return match.group(1).strip().rstrip("/").lower() if match else ""


def normalize_linkedin_url(value: str) -> str:
    """Canonicalize a LinkedIn URL to https://www.linkedin.com/in/<public_id>.

    Kept local for the same no-percent-decoding reason as
    extract_public_identifier above."""
    url = (value or "").strip()
    if not url:
        return ""
    if url.startswith("linkedin.com/"):
        url = "https://www." + url
    elif url.startswith("www.linkedin.com/"):
        url = "https://" + url
    url = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    public_id = extract_public_identifier(url)
    return f"https://www.linkedin.com/in/{public_id}" if public_id else url


def read_csv(path: Path) -> list[dict[str, str]]:
    """Read a CSV into dict rows (BOM-tolerant)."""
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        return list(CsvIO.dict_reader(handle))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    """Write dict rows to a CSV, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def gmail_discover_dir(base_dir: Path, account_email: str = "") -> Path:
    """Return the fixed per-account discover output directory."""
    return base_dir / "discover" / "gmail" / source_slug(account_email or "all")


def csv_key(row: dict[str, Any], fields: list[str]) -> tuple[str, ...] | None:
    """Build a normalized upsert key from the given fields; None when empty."""
    key = tuple(str(row.get(field) or "").strip().lower() for field in fields)
    return key if any(key) else None


def normalize_csv_row(fieldnames: list[str], row: dict[str, Any]) -> dict[str, Any]:
    """Project a row onto exactly the given fieldnames (missing -> '')."""
    return {field: row.get(field, "") for field in fieldnames}


def merge_csv_row(fieldnames: list[str], existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Overlay non-empty incoming values onto an existing row; `added_at` is
    first-write-wins."""
    merged = normalize_csv_row(fieldnames, existing)
    for field in fieldnames:
        value = incoming.get(field, "")
        if value in ("", None):
            continue
        if field == "added_at" and merged.get(field):
            continue
        merged[field] = value
    return merged


def upsert_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]], key_fields: list[str]) -> dict[str, int]:
    """Merge incoming rows into an existing CSV by key, preserving rows the
    incoming set does not restate; returns upsert counters."""
    existing_rows = read_csv(path) if path.exists() else []
    keyed: dict[tuple[str, ...], dict[str, Any]] = {}
    keyless_existing: list[dict[str, Any]] = []
    for row in existing_rows:
        normalized = normalize_csv_row(fieldnames, row)
        key = csv_key(normalized, key_fields)
        if key is None:
            keyless_existing.append(normalized)
            continue
        keyed[key] = merge_csv_row(fieldnames, keyed[key], normalized) if key in keyed else normalized

    incoming_keys: set[tuple[str, ...]] = set()
    for row in rows:
        normalized = normalize_csv_row(fieldnames, row)
        key = csv_key(normalized, key_fields)
        if key is None:
            keyless_existing.append(normalized)
            continue
        incoming_keys.add(key)
        keyed[key] = merge_csv_row(fieldnames, keyed[key], normalized) if key in keyed else normalized

    output_rows = [keyed[key] for key in sorted(keyed)]
    output_rows.extend(keyless_existing)
    write_csv(path, fieldnames, output_rows)
    return {
        "incoming": len(rows),
        "existing": len(existing_rows),
        "written": len(output_rows),
        "preserved_existing": len([key for key in keyed if key not in incoming_keys]),
        "upserted": len(incoming_keys),
    }


def people_rows_from_msgvault(rows: list[dict[str, Any]], source_artifacts: list[str]) -> list[dict[str, Any]]:
    """Project aggregated msgvault contacts onto the canonical people schema."""
    people: list[dict[str, Any]] = []
    for row in rows:
        first_name, last_name = split_name(row.get("display_name") or "")
        person = {col: "" for col in PEOPLE_COLUMNS}
        try:
            total_messages = int(float(row.get("total_messages") or 0))
        except (TypeError, ValueError):
            total_messages = 0
        person.update({
            "id": f"gmail:{short_hash(row['email'], 16)}",
            "first_name": first_name,
            "last_name": last_name,
            "full_name": row.get("display_name") or "",
            "enrichment_provider": "msgvault_metadata",
            "enriched_at": now_iso(),
            "primary_email": row["email"],
            "all_emails": json.dumps([row["email"]]),
            "source_channels": "gmail_msgvault",
            "source_artifacts": json.dumps(source_artifacts, ensure_ascii=False),
            "interaction_counts": json.dumps({"gmail": total_messages}) if total_messages > 0 else "",
            "last_interaction": normalize_interaction_timestamp(row.get("last_interaction")),
        })
        people.append(person)
    return people


def linkedin_resolution_queue_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Derive LinkedIn-resolution queue rows from aggregated contact rows.

    Single home for this shape: `write_msgvault_artifacts` emits it as
    `linkedin_resolution_queue.csv`, and
    `deep_context/build_email_context.py` imports it to re-derive the same
    candidate set."""
    queue: list[dict[str, Any]] = []
    for row in rows:
        email = str(row.get("email") or "").strip().lower()
        if not email:
            continue
        guess = domain_guess(email)
        queue.append({
            "handle": email,
            "id": f"gmail:{short_hash(email, 16)}",
            "account_emails": json.dumps(row.get("account_emails") or [], ensure_ascii=False),
            "source_ids": json.dumps(row.get("source_ids") or [], ensure_ascii=False),
            "display_name": row.get("display_name") or "",
            "full_name": row.get("display_name") or "",
            "primary_email": email,
            "company_guess": guess.get("company_guess", ""),
            "primary_email_type": row.get("primary_email_type") or classify_email(email),
            "total_messages": row.get("total_messages", ""),
            "thread_count": row.get("thread_count", ""),
            "last_interaction": row.get("last_interaction", ""),
            "source": "gmail_msgvault",
            "source_channels": "gmail_msgvault",
        })
    return queue


def load_resolution_map(path: Path, min_confidence: float) -> dict[str, dict[str, str]]:
    """Load found resolutions at/above min_confidence, keyed by handle."""
    resolutions: dict[str, dict[str, str]] = {}
    for row in read_csv(path):
        status = (row.get("status") or "").strip().lower()
        linkedin_url = normalize_linkedin_url(row.get("linkedin_url") or "")
        try:
            confidence = float(row.get("confidence") or 0)
        except ValueError:
            confidence = 0.0
        handle = (row.get("handle") or "").strip().lower()
        if status == "found" and linkedin_url and handle and confidence >= min_confidence:
            row = dict(row)
            row["linkedin_url"] = linkedin_url
            row["confidence"] = str(confidence)
            resolutions[handle] = row
    return resolutions


def apply_linkedin_resolutions_to_people(people_csv: Path, resolutions_csv: Path, output_dir: Path, *, min_confidence: float = 0.75) -> dict[str, Any]:
    """Attach stored LinkedIn resolutions onto a Gmail people.csv, rewriting
    matched rows to LinkedIn identity (id/public_identifier/linkedin_url)."""
    people_rows = read_csv(people_csv)
    resolutions = load_resolution_map(resolutions_csv, min_confidence)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "people.csv"
    applied_path = output_dir / "linkedin_resolutions_applied.csv"
    applied: list[dict[str, Any]] = []
    output_rows: list[dict[str, Any]] = []
    for row in people_rows:
        normalized = {col: row.get(col, "") for col in PEOPLE_COLUMNS}
        email = (normalized.get("primary_email") or "").strip().lower()
        resolution = resolutions.get(email) or resolutions.get((normalized.get("id") or "").strip().lower())
        if resolution:
            linkedin_url = normalize_linkedin_url(resolution.get("linkedin_url") or "")
            public_id = extract_public_identifier(linkedin_url)
            if public_id:
                normalized["id"] = generate_linkedin_person_id(public_id)
                normalized["public_identifier"] = public_id
                normalized["linkedin_url"] = linkedin_url
                if resolution.get("matched_name") and not normalized.get("full_name"):
                    normalized["full_name"] = resolution["matched_name"]
                if resolution.get("matched_headline"):
                    normalized["headline"] = resolution["matched_headline"]
                normalized["enrichment_provider"] = "parallel_linkedin_resolution"
                normalized["enriched_at"] = now_iso()
                artifacts = [str(people_csv), str(resolutions_csv)]
                try:
                    existing = json.loads(normalized.get("source_artifacts") or "[]")
                    if isinstance(existing, list):
                        artifacts = [str(x) for x in existing] + [str(resolutions_csv)]
                except json.JSONDecodeError:
                    pass
                normalized["source_artifacts"] = json.dumps(sorted(set(artifacts)), ensure_ascii=False)
                applied.append({
                    "primary_email": email,
                    "linkedin_url": linkedin_url,
                    "public_identifier": public_id,
                    "confidence": resolution.get("confidence", ""),
                    "matched_name": resolution.get("matched_name", ""),
                })
        output_rows.append(normalized)
    write_csv(out_path, PEOPLE_COLUMNS, output_rows)
    write_csv(applied_path, ["primary_email", "linkedin_url", "public_identifier", "confidence", "matched_name"], applied)
    return {
        "status": "completed",
        "input_people_csv": str(people_csv),
        "resolutions_csv": str(resolutions_csv),
        "people_csv": str(out_path),
        "applied_csv": str(applied_path),
        "rows": len(output_rows),
        "resolved": len(applied),
        "min_confidence": min_confidence,
    }


def write_msgvault_artifacts(rows: list[dict[str, Any]], out_dir: Path, account_email: str = "", *, include_automated: bool = False, limit: int | None = None, excluded_labels: Iterable[str] | None = None) -> dict[str, Any]:
    """Filter aggregated contacts (automated + one-way dropped), upsert every
    discover artifact CSV in the fixed account directory, and write the stage
    manifest. Returns the manifest payload."""
    automated_filtered = [row for row in rows if row.get("automated_filtered") and not include_automated]
    non_automated = [row for row in rows if include_automated or not row.get("automated_filtered")]
    one_way_filtered = [row for row in non_automated if not has_round_trip_interaction(row)]
    filtered = [row for row in non_automated if has_round_trip_interaction(row)]
    if limit is not None:
        filtered = filtered[: max(0, int(limit))]
    out_dir.mkdir(parents=True, exist_ok=True)
    threads_path = out_dir / "gmail_threads.csv"
    aggregated_path = out_dir / "gmail_contacts_aggregated.csv"
    targeted_path = out_dir / "targeted_emails.csv"
    resolution_queue_path = out_dir / "linkedin_resolution_queue.csv"
    people_path = out_dir / "people.csv"
    accounts_path = out_dir / "accounts.csv"
    manifest_path = out_dir / "manifest.json"
    discovered_at = now_iso()

    account_rows = []
    seen_accounts: set[str] = set()
    for row in filtered:
        for account in row.get("account_emails") or []:
            if account in seen_accounts:
                continue
            seen_accounts.add(account)
            account_rows.append({"account_id": f"msgvault:{short_hash(account, 12)}", "account_email": account, "provider": "gmail", "source": "msgvault", "added_at": discovered_at})
    if account_email and account_email not in seen_accounts:
        account_rows.append({"account_id": f"msgvault:{short_hash(account_email, 12)}", "account_email": account_email, "provider": "gmail", "source": "msgvault", "added_at": discovered_at})
    upserts: dict[str, dict[str, int]] = {}
    upserts["accounts_csv"] = upsert_csv(accounts_path, ACCOUNT_COLUMNS, account_rows, ["account_email"])

    threads_rows = [{
        "email": row["email"],
        "display_name": row["display_name"],
        "thread_id": "",
        "received_count": row["total_received"],
        "sent_count": row["total_sent"],
        "message_count": row["total_messages"],
        "first_message_at": row["first_interaction"],
        "last_message_at": row["last_interaction"],
        "subject": "",
        "discovered_at": discovered_at,
    } for row in filtered]
    aggregated_rows = [{
        "email": row["email"],
        "display_name": row["display_name"],
        "total_sent": row["total_sent"],
        "total_received": row["total_received"],
        "total_messages": row["total_messages"],
        "one_to_one_sent": row["one_to_one_sent"],
        "one_to_one_received": row["one_to_one_received"],
        "one_to_one_messages": row["one_to_one_messages"],
        "group_sent": row["group_sent"],
        "group_received": row["group_received"],
        "group_messages": row["group_messages"],
        "one_to_one_thread_count": row["one_to_one_thread_count"],
        "group_thread_count": row["group_thread_count"],
        "thread_count": row["thread_count"],
        "first_interaction": row["first_interaction"],
        "last_interaction": row["last_interaction"],
        "sample_subjects": "[]",
    } for row in filtered]
    targeted_rows = [{
        "display_name": row["display_name"],
        "primary_email": row["email"],
        "primary_email_type": row["primary_email_type"],
        "all_emails": json.dumps([row["email"]]),
        "email_count": 1,
        "total_sent": row["total_sent"],
        "total_received": row["total_received"],
        "total_messages": row["total_messages"],
        "one_to_one_sent": row["one_to_one_sent"],
        "one_to_one_received": row["one_to_one_received"],
        "one_to_one_messages": row["one_to_one_messages"],
        "group_sent": row["group_sent"],
        "group_received": row["group_received"],
        "group_messages": row["group_messages"],
        "one_to_one_thread_count": row["one_to_one_thread_count"],
        "group_thread_count": row["group_thread_count"],
        "thread_count": row["thread_count"],
        "first_interaction": row["first_interaction"],
        "last_interaction": row["last_interaction"],
        "is_duplicate": False,
        "potential_same_person_emails": "[]",
        "sample_subjects": "[]",
        "sample_calendar_titles": "[]",
    } for row in filtered]
    resolution_queue_rows = linkedin_resolution_queue_rows(filtered)
    people_rows = people_rows_from_msgvault(filtered, [str(targeted_path), str(aggregated_path), str(resolution_queue_path)])

    upserts["gmail_threads_csv"] = upsert_csv(threads_path, THREAD_COLUMNS, threads_rows, ["email"])
    upserts["gmail_contacts_aggregated_csv"] = upsert_csv(aggregated_path, AGGREGATED_COLUMNS, aggregated_rows, ["email"])
    upserts["targeted_emails_csv"] = upsert_csv(targeted_path, TARGETED_COLUMNS, targeted_rows, ["primary_email"])
    upserts["linkedin_resolution_queue_csv"] = upsert_csv(resolution_queue_path, LINKEDIN_RESOLUTION_QUEUE_COLUMNS, resolution_queue_rows, ["handle"])
    upserts["people_csv"] = upsert_csv(people_path, PEOPLE_COLUMNS, people_rows, ["primary_email"])

    existing_manifest = read_json(manifest_path, {}) or {}

    manifest = {
        "task": "import_gmail_network_msgvault",
        "version": 2,
        "calculation_version": GMAIL_INTERACTION_CALCULATION_VERSION,
        "calculation_mode": "full_recount",
        "created_at": existing_manifest.get("created_at") or discovered_at,
        "updated_at": discovered_at,
        "status": "completed",
        "source": "msgvault",
        "artifact_dir": str(out_dir),
        "account_slug": out_dir.name,
        "privacy": {
            "message_bodies_read": False,
            "message_subjects_included": False,
            "raw_mime_read": False,
            "local_artifacts_only": True,
        },
        "account_email": account_email,
        "counts": {
            "contacts_seen": len(rows),
            "contacts_written": len(filtered),
            "contacts_final": upserts["people_csv"]["written"],
            "contacts_preserved_existing": upserts["people_csv"]["preserved_existing"],
            "automated_filtered": len(automated_filtered),
            "one_way_filtered": len(one_way_filtered),
            "round_trip_required": True,
            "accounts": upserts["accounts_csv"]["written"],
            "excluded_labels": normalize_label_names(excluded_labels),
        },
        "upserts": upserts,
        "artifacts": {
            "accounts_csv": str(accounts_path),
            "gmail_threads_csv": str(threads_path),
            "gmail_contacts_aggregated_csv": str(aggregated_path),
            "targeted_emails_csv": str(targeted_path),
            "linkedin_resolution_queue_csv": str(resolution_queue_path),
            "people_csv": str(people_path),
            "manifest_json": str(manifest_path),
        },
        "schema_reference": {
            "msgvault_tables": ["sources", "participants", "messages", "message_recipients"],
            "key_fields": ["participants.email_address", "participants.display_name", "message_recipients.display_name", "messages.sent_at", "sources.identifier"],
        },
    }
    write_json(manifest_path, manifest)
    return manifest


def command_msgvault_accounts(args: argparse.Namespace) -> int:
    """`msgvault-accounts`: list Gmail source accounts in the local archive."""
    with MsgvaultStore(Path(args.db)) as store:
        store.require_schema()
        accounts = store.list_accounts()
    emit({
        "status": "ok",
        "source": "msgvault",
        "db": str(Path(args.db).expanduser()),
        "accounts": accounts,
        "count": len(accounts),
        "privacy": {
            "message_bodies_read": False,
            "message_subjects_included": False,
            "raw_mime_read": False,
            "local_artifacts_only": True,
        },
    })
    return 0


def command_msgvault(args: argparse.Namespace) -> int:
    """`msgvault`: aggregate one account's contacts and write the discover
    artifacts for it."""
    excluded_labels = default_excluded_labels(bool(args.include_category_mail), args.exclude_label)
    with MsgvaultStore(Path(args.db)) as store:
        store.require_schema()
        rows = store.aggregate_contacts(args.account_email, excluded_labels)
    out_dir = gmail_discover_dir(Path(args.output_dir), args.account_email)
    manifest = write_msgvault_artifacts(
        rows,
        out_dir,
        account_email=args.account_email,
        include_automated=bool(args.include_automated),
        limit=args.limit,
        excluded_labels=excluded_labels,
    )
    emit({
        "status": "completed",
        "artifact_dir": str(out_dir),
        "artifacts": manifest["artifacts"],
        "counts": manifest["counts"],
        "privacy": manifest["privacy"],
        "summary": "Imported Gmail contact metadata from msgvault and wrote a LinkedIn resolution queue. No message bodies, subjects, raw MIME, external APIs, uploads, or prod writes were used.",
    })
    return 0


def command_apply_resolutions(args: argparse.Namespace) -> int:
    """`apply-resolutions`: attach stored resolutions onto a people.csv."""
    payload = apply_linkedin_resolutions_to_people(
        Path(args.people_csv),
        Path(args.resolutions_csv),
        Path(args.output_dir),
        min_confidence=args.min_confidence,
    )
    emit(payload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse tree: msgvault-accounts, msgvault, apply-resolutions."""
    parser = argparse.ArgumentParser(description="Gmail discovery engine: msgvault metadata -> local network artifacts")
    sub = parser.add_subparsers(dest="command", required=True)

    sources = sub.add_parser("msgvault-accounts", aliases=["msgvault-sources"], help="List Gmail source accounts in a local msgvault SQLite archive")
    sources.add_argument("--db", default=str(DEFAULT_MSGVAULT_DB), help="Path to msgvault.db (default: $MSGVAULT_HOME/msgvault.db or ~/.msgvault/msgvault.db)")
    sources.set_defaults(func=command_msgvault_accounts)

    msgvault = sub.add_parser("msgvault", aliases=["import-msgvault"], help="Import Gmail contact metadata from a local msgvault SQLite archive")
    msgvault.add_argument("--db", default=str(DEFAULT_MSGVAULT_DB), help="Path to msgvault.db (default: $MSGVAULT_HOME/msgvault.db or ~/.msgvault/msgvault.db)")
    msgvault.add_argument("--account-email", default="", help="Optional Gmail source account filter")
    msgvault.add_argument("--output-dir", default=str(DEFAULT_BASE_DIR))
    msgvault.add_argument("--limit", type=int)
    msgvault.add_argument("--include-automated", action="store_true", help="Include noreply/automated service addresses")
    msgvault.add_argument("--exclude-label", action="append", default=[], help="Exclude messages with this msgvault/Gmail label name; may be repeated")
    msgvault.add_argument("--include-category-mail", action="store_true", help="Do not exclude default Gmail category labels: Social, Promotions, Forums, Updates")
    msgvault.set_defaults(func=command_msgvault)

    apply = sub.add_parser("apply-resolutions", help="Apply LinkedIn resolution results to a Gmail/msgvault people.csv")
    apply.add_argument("--people-csv", required=True)
    apply.add_argument("--resolutions-csv", required=True)
    apply.add_argument("--output-dir", default=str(DEFAULT_BASE_DIR))
    apply.add_argument("--min-confidence", type=float, default=0.75)
    apply.set_defaults(func=command_apply_resolutions)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse args and dispatch; JSON error payloads on ValueError/interrupt."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        emit({"status": "error", "error": str(exc)})
        return 2
    except KeyboardInterrupt:
        emit({"status": "interrupted"})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
