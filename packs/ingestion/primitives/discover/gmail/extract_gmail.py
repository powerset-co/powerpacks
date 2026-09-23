#!/usr/bin/env python3
"""Extract msgvault metadata into fixed per-account Gmail artifacts.

Reads contact metadata only. Writes accounts, thread counts, aggregated contacts,
targeted emails, the LinkedIn queue, people.csv, and manifest.json. Identity
matching belongs to Deep Context.

Changelog:
  2026-09-23 (typed rows): every aggregated contact row is folded ONCE into
    `msgvault.util.MsgvaultContactRow` at the top of `write_msgvault_artifacts`,
    and the three row projections (`people_rows_from_msgvault`,
    `linkedin_resolution_queue_rows`, `write_msgvault_artifacts`) work on
    attributes instead of probing the dict. The public row builders still accept
    the store's dict rows (deep_context and the tests pass those) — `from_row`
    does the tolerant read. The per-account prior manifest is parsed once into
    `util.GmailManifestResume`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import emit, now_iso, read_json, short_hash, write_json  # noqa: E402
from packs.ingestion.primitives.common.paths import DEFAULT_BASE_DIR, gmail_discover_dir  # noqa: E402
from packs.ingestion.primitives.discover.common import (  # noqa: E402
    GMAIL_INTERACTION_CALCULATION_VERSION,
)
from packs.ingestion.primitives.discover.gmail.msgvault.store import MsgvaultStore  # noqa: E402
from packs.ingestion.primitives.discover.gmail.util import (  # noqa: E402
    GMAIL_CALCULATION_FULL_RECOUNT,
    GmailManifestResume,
)
from packs.ingestion.primitives.discover.gmail.msgvault.util import (  # noqa: E402
    DEFAULT_MSGVAULT_DB,
    MsgvaultContactRow,
    classify_email,
    default_excluded_labels,
    domain_guess,
    has_round_trip_interaction,
    normalize_label_names,
    split_name,
)
from packs.ingestion.schemas.gmail_artifacts import (  # noqa: E402
    LINKEDIN_RESOLUTION_QUEUE_COLUMNS,
)
from packs.ingestion.schemas.people_schema import (  # noqa: E402
    PEOPLE_SCHEMA_COLUMNS,
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
ACCOUNT_COLUMNS = ["account_id", "account_email", "provider", "source", "added_at"]
PEOPLE_COLUMNS = list(PEOPLE_SCHEMA_COLUMNS)


def people_rows_from_msgvault(rows: Iterable[Any], source_artifacts: list[str]) -> list[dict[str, Any]]:
    """Project aggregated msgvault contacts onto the canonical people schema.

    Accepts the store's dict rows or already-typed `MsgvaultContactRow` values."""
    people: list[dict[str, Any]] = []
    for row in rows:
        contact = MsgvaultContactRow.from_row(row)
        first_name, last_name = split_name(contact.display_name)
        person = {col: "" for col in PEOPLE_COLUMNS}
        total_messages = contact.total_messages
        person.update({
            "id": f"gmail:{short_hash(contact.email, 16)}",
            "first_name": first_name,
            "last_name": last_name,
            "full_name": contact.display_name,
            "primary_email": contact.email,
            "all_emails": json.dumps([contact.email]),
            "source_channels": "gmail_msgvault",
            "source_artifacts": json.dumps(source_artifacts, ensure_ascii=False),
            "interaction_counts": json.dumps({"gmail": total_messages}) if total_messages > 0 else "",
            "last_interaction": normalize_interaction_timestamp(contact.last_interaction),
        })
        people.append(person)
    return people


def linkedin_resolution_queue_rows(rows: Iterable[Any]) -> list[dict[str, Any]]:
    """Derive LinkedIn-resolution queue rows from aggregated contact rows.

    Accepts the store's dict rows or already-typed `MsgvaultContactRow` values.
    Single home for this shape: `write_msgvault_artifacts` emits it as
    `linkedin_resolution_queue.csv`, and
    `deep_context/build_email_context.py` imports it to re-derive the same
    candidate set."""
    queue: list[dict[str, Any]] = []
    for row in rows:
        contact = MsgvaultContactRow.from_row(row)
        email = contact.email.strip().lower()
        if not email:
            continue
        guess = domain_guess(email)
        queue.append({
            "handle": email,
            "id": f"gmail:{short_hash(email, 16)}",
            "account_emails": json.dumps(contact.account_emails, ensure_ascii=False),
            "source_ids": json.dumps(contact.source_ids, ensure_ascii=False),
            "display_name": contact.display_name,
            "full_name": contact.display_name,
            "primary_email": email,
            "company_guess": guess["company_guess"],
            "primary_email_type": contact.primary_email_type or classify_email(email),
            "total_messages": contact.total_messages or "",
            "thread_count": contact.thread_count or "",
            "last_interaction": contact.last_interaction,
            "source": "gmail_msgvault",
            "source_channels": "gmail_msgvault",
        })
    return queue


def write_msgvault_artifacts(rows: list[dict[str, Any]], out_dir: Path, account_email: str = "", *, include_automated: bool = False, limit: int | None = None, excluded_labels: Iterable[str] | None = None) -> dict[str, Any]:
    """Filter aggregated contacts (automated + one-way dropped), upsert every
    discover artifact CSV in the fixed account directory, and write the stage
    manifest. Returns the manifest payload.

    The store's dict rows are folded into typed `MsgvaultContactRow` values ONCE
    here; everything below reads attributes."""
    contacts = [MsgvaultContactRow.from_row(row) for row in rows]
    automated_filtered = [contact for contact in contacts if contact.automated_filtered and not include_automated]
    non_automated = [contact for contact in contacts if include_automated or not contact.automated_filtered]
    one_way_filtered = [contact for contact in non_automated if not has_round_trip_interaction(contact)]
    filtered = [contact for contact in non_automated if has_round_trip_interaction(contact)]
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
    for contact in filtered:
        for account in contact.account_emails:
            if account in seen_accounts:
                continue
            seen_accounts.add(account)
            account_rows.append({"account_id": f"msgvault:{short_hash(account, 12)}", "account_email": account, "provider": "gmail", "source": "msgvault", "added_at": discovered_at})
    if account_email and account_email not in seen_accounts:
        account_rows.append({"account_id": f"msgvault:{short_hash(account_email, 12)}", "account_email": account_email, "provider": "gmail", "source": "msgvault", "added_at": discovered_at})
    upserts: dict[str, dict[str, int]] = {}
    upserts["accounts_csv"] = CsvIO.upsert_dict_rows(accounts_path, ACCOUNT_COLUMNS, account_rows, ["account_email"])

    threads_rows = [{
        "email": contact.email,
        "display_name": contact.display_name,
        "thread_id": "",
        "received_count": contact.total_received,
        "sent_count": contact.total_sent,
        "message_count": contact.total_messages,
        "first_message_at": contact.first_interaction,
        "last_message_at": contact.last_interaction,
        "subject": "",
        "discovered_at": discovered_at,
    } for contact in filtered]
    aggregated_rows = [{
        "email": contact.email,
        "display_name": contact.display_name,
        "total_sent": contact.total_sent,
        "total_received": contact.total_received,
        "total_messages": contact.total_messages,
        "one_to_one_sent": contact.one_to_one_sent,
        "one_to_one_received": contact.one_to_one_received,
        "one_to_one_messages": contact.one_to_one_messages,
        "group_sent": contact.group_sent,
        "group_received": contact.group_received,
        "group_messages": contact.group_messages,
        "one_to_one_thread_count": contact.one_to_one_thread_count,
        "group_thread_count": contact.group_thread_count,
        "thread_count": contact.thread_count,
        "first_interaction": contact.first_interaction,
        "last_interaction": contact.last_interaction,
        "sample_subjects": "[]",
    } for contact in filtered]
    targeted_rows = [{
        "display_name": contact.display_name,
        "primary_email": contact.email,
        "primary_email_type": contact.primary_email_type,
        "all_emails": json.dumps([contact.email]),
        "email_count": 1,
        "total_sent": contact.total_sent,
        "total_received": contact.total_received,
        "total_messages": contact.total_messages,
        "one_to_one_sent": contact.one_to_one_sent,
        "one_to_one_received": contact.one_to_one_received,
        "one_to_one_messages": contact.one_to_one_messages,
        "group_sent": contact.group_sent,
        "group_received": contact.group_received,
        "group_messages": contact.group_messages,
        "one_to_one_thread_count": contact.one_to_one_thread_count,
        "group_thread_count": contact.group_thread_count,
        "thread_count": contact.thread_count,
        "first_interaction": contact.first_interaction,
        "last_interaction": contact.last_interaction,
        "is_duplicate": False,
        "potential_same_person_emails": "[]",
        "sample_subjects": "[]",
        "sample_calendar_titles": "[]",
    } for contact in filtered]
    resolution_queue_rows = linkedin_resolution_queue_rows(filtered)
    people_rows = people_rows_from_msgvault(filtered, [str(targeted_path), str(aggregated_path), str(resolution_queue_path)])

    upserts["gmail_threads_csv"] = CsvIO.upsert_dict_rows(threads_path, THREAD_COLUMNS, threads_rows, ["email"])
    upserts["gmail_contacts_aggregated_csv"] = CsvIO.upsert_dict_rows(aggregated_path, AGGREGATED_COLUMNS, aggregated_rows, ["email"])
    upserts["targeted_emails_csv"] = CsvIO.upsert_dict_rows(targeted_path, TARGETED_COLUMNS, targeted_rows, ["primary_email"])
    upserts["linkedin_resolution_queue_csv"] = CsvIO.upsert_dict_rows(resolution_queue_path, LINKEDIN_RESOLUTION_QUEUE_COLUMNS, resolution_queue_rows, ["handle"])
    upserts["people_csv"] = CsvIO.upsert_dict_rows(people_path, PEOPLE_COLUMNS, people_rows, ["primary_email"])

    existing_manifest = GmailManifestResume.from_document(read_json(manifest_path, {}) or {})

    manifest = {
        "task": "import_gmail_network_msgvault",
        "version": 2,
        "calculation_version": GMAIL_INTERACTION_CALCULATION_VERSION,
        # Counts cover the whole local archive; sync's --after only bounds downloads.
        "calculation_mode": GMAIL_CALCULATION_FULL_RECOUNT,
        "created_at": existing_manifest.created_at or discovered_at,
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


class GmailExtractor:
    """Read msgvault account/contact metadata and write local discovery artifacts."""

    def list_msgvault_accounts(self, *, db: str | Path) -> dict[str, Any]:
        """List the Gmail source accounts in the local msgvault archive.

        Returns the `status: ok` payload the CLI `msgvault-accounts` subcommand
        emits verbatim."""
        with MsgvaultStore(Path(db)) as store:
            store.require_schema()
            accounts = store.list_accounts()
        return {
            "status": "ok",
            "source": "msgvault",
            "db": str(Path(db).expanduser()),
            "accounts": accounts,
            "count": len(accounts),
            "privacy": {
                "message_bodies_read": False,
                "message_subjects_included": False,
                "raw_mime_read": False,
                "local_artifacts_only": True,
            },
        }

    def run_msgvault(
        self,
        *,
        db: str | Path,
        account_email: str,
        output_dir: str | Path,
        include_automated: bool = False,
        include_category_mail: bool = False,
        limit: int | None = None,
        exclude_labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """Aggregate one account's msgvault contacts and write its discover
        artifacts under `gmail_discover_dir(output_dir, account_email)`.

        Automated/noreply addresses are dropped unless `include_automated`; the
        default Gmail category labels are excluded unless `include_category_mail`,
        alongside any `exclude_labels`. Returns the `status: completed` payload the
        CLI `msgvault` subcommand emits (artifact_dir + artifacts + counts +
        privacy)."""
        excluded_labels = default_excluded_labels(include_category_mail, list(exclude_labels or []))
        with MsgvaultStore(Path(db)) as store:
            store.require_schema()
            rows = store.aggregate_contacts(account_email, excluded_labels)
        out_dir = gmail_discover_dir(Path(output_dir), account_email)
        manifest = write_msgvault_artifacts(
            rows,
            out_dir,
            account_email=account_email,
            include_automated=include_automated,
            limit=limit,
            excluded_labels=excluded_labels,
        )
        return {
            "status": "completed",
            "artifact_dir": str(out_dir),
            "calculation_mode": manifest["calculation_mode"],
            "artifacts": manifest["artifacts"],
            "counts": manifest["counts"],
            "privacy": manifest["privacy"],
            "summary": "Imported Gmail contact metadata from msgvault and wrote a LinkedIn resolution queue. No message bodies, subjects, raw MIME, external APIs, uploads, or prod writes were used.",
        }


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse tree: msgvault-accounts and msgvault."""
    parser = argparse.ArgumentParser(description="Gmail discovery engine: msgvault metadata -> local network artifacts")
    sub = parser.add_subparsers(dest="command", required=True)

    sources = sub.add_parser("msgvault-accounts", aliases=["msgvault-sources"], help="List Gmail source accounts in a local msgvault SQLite archive")
    sources.add_argument("--db", default=str(DEFAULT_MSGVAULT_DB), help="Path to msgvault.db (default: $MSGVAULT_HOME/msgvault.db or ~/.msgvault/msgvault.db)")

    msgvault = sub.add_parser("msgvault", aliases=["import-msgvault"], help="Import Gmail contact metadata from a local msgvault SQLite archive")
    msgvault.add_argument("--db", default=str(DEFAULT_MSGVAULT_DB), help="Path to msgvault.db (default: $MSGVAULT_HOME/msgvault.db or ~/.msgvault/msgvault.db)")
    msgvault.add_argument("--account-email", default="", help="Optional Gmail source account filter")
    msgvault.add_argument("--output-dir", default=str(DEFAULT_BASE_DIR))
    msgvault.add_argument("--limit", type=int)
    msgvault.add_argument("--include-automated", action="store_true", help="Include noreply/automated service addresses")
    msgvault.add_argument("--exclude-label", action="append", default=[], help="Exclude messages with this msgvault/Gmail label name; may be repeated")
    msgvault.add_argument("--include-category-mail", action="store_true", help="Do not exclude default Gmail category labels: Social, Promotions, Forums, Updates")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse args, construct the engine, dispatch to the matching method, emit its
    payload, and return the exit code: 0 on success, 2 on ValueError, 130 on
    interrupt (the mapping the subprocess CLI has always exposed).

    Aliases are matched explicitly (`msgvault-sources` for `msgvault-accounts`,
    `import-msgvault` for `msgvault`): argparse stores the alias the user typed in
    `args.command`, not the canonical subparser name."""
    parser = build_parser()
    args = parser.parse_args(argv)
    engine = GmailExtractor()
    try:
        if args.command in ("msgvault-accounts", "msgvault-sources"):
            payload = engine.list_msgvault_accounts(db=args.db)
        elif args.command in ("msgvault", "import-msgvault"):
            payload = engine.run_msgvault(
                db=args.db,
                account_email=args.account_email,
                output_dir=args.output_dir,
                include_automated=bool(args.include_automated),
                include_category_mail=bool(args.include_category_mail),
                limit=args.limit,
                exclude_labels=args.exclude_label,
            )
    except ValueError as exc:
        emit({"status": "error", "error": str(exc)})
        return 2
    except KeyboardInterrupt:
        emit({"status": "interrupted"})
        return 130
    emit(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
