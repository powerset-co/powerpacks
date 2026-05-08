#!/usr/bin/env python3
"""Extract iMessage contact metadata with Python stdlib only.

The primitive reads local SQLite databases in read-only mode and exports only:
phone, name, source, group flags/names, message counts, and last-message time.
It does not select message text/body columns.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


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

DEFAULT_CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
DEFAULT_ADDRESSBOOK_GLOB = str(
    Path.home()
    / "Library"
    / "Application Support"
    / "AddressBook"
    / "Sources"
    / "*"
    / "AddressBook-v22.abcddb"
)
DEFAULT_OUT_DIR = Path(".powerpacks/messages")
APPLE_EPOCH_OFFSET = 978_307_200
NS_PER_SEC = 1_000_000_000
GROUP_SEPARATOR = " | "
PRIVACY_SETTINGS_URLS = {
    "full-disk-access": "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles",
    "contacts": "x-apple.systempreferences:com.apple.preference.security?Privacy_Contacts",
}


@dataclass
class Contact:
    phone: str
    name: str = ""
    source: str = "imessage"
    is_in_group_chats: bool = False
    group_names: list[str] | None = None
    message_count: int | None = None
    last_message: str | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def lookup_key(raw: str) -> str:
    digits = re.sub(r"[^\d]", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return digits


def is_phone_identifier(identifier: str) -> bool:
    if not identifier or "@" in identifier or identifier.startswith("urn:") or identifier.startswith("chat"):
        return False
    return len(re.sub(r"[^\d]", "", identifier)) >= 7


def clean_name(first: str, last: str) -> str:
    first = re.sub(r"/\d+$", "", (first or "").strip())
    last = re.sub(r"/\d+$", "", (last or "").strip())
    if first and last:
        name = f"{first} {last}"
    else:
        name = first or last
    if ";" in name:
        left, right = name.split(";", 1)
        name = f"{right} {left}"
    return re.sub(r"\s+", " ", name).strip()


def apple_timestamp_to_iso(value: int | float | None) -> str | None:
    if value is None:
        return None
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return None

    # Messages usually stores nanoseconds since 2001. Some old/local variants
    # use seconds since 2001 or Unix-ish timestamps; handle all three.
    if raw > 10_000_000_000:
        unix_ts = (raw / NS_PER_SEC) + APPLE_EPOCH_OFFSET
    elif raw < 2_000_000_000:
        unix_ts = raw + APPLE_EPOCH_OFFSET
    else:
        unix_ts = raw
    try:
        return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def open_sqlite_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def sqlite_tables(path: Path) -> set[str]:
    with open_sqlite_readonly(path) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(row["name"]) for row in rows}


def check_chat_db(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "readable": False,
        "required_tables": ["message", "handle"],
        "missing_tables": [],
        "error": None,
    }
    if not path.exists():
        result["error"] = "chat.db does not exist"
        return result
    try:
        tables = sqlite_tables(path)
        result["readable"] = True
        result["missing_tables"] = [table for table in result["required_tables"] if table not in tables]
        result["has_group_tables"] = "chat" in tables and "chat_handle_join" in tables
    except sqlite3.Error as exc:
        result["error"] = str(exc)
    return result


def check_addressbook(addressbook_glob: str) -> dict[str, Any]:
    matches = sorted(glob.glob(addressbook_glob))
    contacts, diagnostics = read_addressbook_contacts(addressbook_glob)
    error_diagnostics = [item for item in diagnostics if item.get("status") == "error"]
    read_diagnostics = [item for item in diagnostics if item.get("status") == "read"]
    return {
        "glob": addressbook_glob,
        "matches": len(matches),
        "readable": bool(read_diagnostics) and not error_diagnostics,
        "readable_databases": len(read_diagnostics),
        "error_databases": len(error_diagnostics),
        "contacts": len(contacts),
        "diagnostics": diagnostics,
    }


def read_addressbook_contacts(addressbook_glob: str) -> tuple[dict[str, str], list[dict[str, Any]]]:
    contacts: dict[str, str] = {}
    diagnostics: list[dict[str, Any]] = []
    query = """
        SELECT p.ZFULLNUMBER, r.ZFIRSTNAME, r.ZLASTNAME
        FROM ZABCDPHONENUMBER p
        JOIN ZABCDRECORD r ON p.ZOWNER = r.Z_PK
        WHERE p.ZFULLNUMBER IS NOT NULL AND p.ZFULLNUMBER <> ''
    """
    for db_name in glob.glob(addressbook_glob):
        path = Path(db_name)
        try:
            with open_sqlite_readonly(path) as conn:
                for row in conn.execute(query):
                    phone = canonicalize_phone(row["ZFULLNUMBER"] or "")
                    if not phone:
                        continue
                    name = clean_name(row["ZFIRSTNAME"] or "", row["ZLASTNAME"] or "")
                    if name and (not contacts.get(phone) or len(name) > len(contacts[phone])):
                        contacts[phone] = name
            diagnostics.append({"path": str(path), "status": "read"})
        except sqlite3.Error as exc:
            diagnostics.append({"path": str(path), "status": "error", "error": str(exc)})
    return contacts, diagnostics


def aggregate_message_stats(chat_db: Path) -> dict[str, dict[str, Any]]:
    query = """
        SELECT
            h.id AS identifier,
            COUNT(*) AS msg_count,
            MAX(m.date) AS last_date
        FROM message m
        JOIN handle h ON h.ROWID = m.handle_id
        WHERE h.id IS NOT NULL
          AND h.id <> ''
          AND (m.associated_message_type IS NULL
               OR m.associated_message_type < 2000
               OR m.associated_message_type > 3006)
        GROUP BY h.id
    """
    stats: dict[str, dict[str, Any]] = {}
    with open_sqlite_readonly(chat_db) as conn:
        for row in conn.execute(query):
            identifier = row["identifier"] or ""
            if not is_phone_identifier(identifier):
                continue
            key = lookup_key(identifier)
            phone = canonicalize_phone(identifier)
            if not key or not phone:
                continue
            current = stats.setdefault(key, {"phone": phone, "message_count": 0, "last_message": None})
            current["message_count"] += int(row["msg_count"] or 0)
            last_message = apple_timestamp_to_iso(row["last_date"])
            if last_message and (not current["last_message"] or last_message > current["last_message"]):
                current["last_message"] = last_message
    return stats


def resolve_group_chat_name(chat_identifier: str, display_name: str | None, room_name: str | None) -> str:
    for candidate in (display_name, room_name):
        cleaned = re.sub(r"\s+", " ", (candidate or "").strip())
        if cleaned and cleaned != chat_identifier:
            return cleaned
    return ""


def read_group_metadata(chat_db: Path) -> dict[str, set[str]]:
    query = """
        SELECT
            h.id AS identifier,
            c.chat_identifier,
            c.display_name,
            c.room_name
        FROM chat c
        JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
        JOIN handle h ON h.ROWID = chj.handle_id
        WHERE c.chat_identifier LIKE 'chat%'
    """
    groups_by_key: dict[str, set[str]] = {}
    try:
        with open_sqlite_readonly(chat_db) as conn:
            for row in conn.execute(query):
                identifier = row["identifier"] or ""
                if not is_phone_identifier(identifier):
                    continue
                key = lookup_key(identifier)
                if not key:
                    continue
                group_name = resolve_group_chat_name(row["chat_identifier"] or "", row["display_name"], row["room_name"])
                groups_by_key.setdefault(key, set())
                if group_name:
                    groups_by_key[key].add(group_name)
    except sqlite3.Error:
        return {}
    return groups_by_key


def build_contacts(
    message_stats: dict[str, dict[str, Any]],
    addressbook_contacts: dict[str, str],
    group_metadata: dict[str, set[str]],
    include_contact_only: bool,
) -> list[Contact]:
    contacts_by_phone: dict[str, Contact] = {}

    for key, stats in message_stats.items():
        phone = stats["phone"]
        name = addressbook_contacts.get(phone, "")
        groups = sorted(group_metadata.get(key, set()), key=str.casefold)
        contacts_by_phone[phone] = Contact(
            phone=phone,
            name=name,
            is_in_group_chats=key in group_metadata,
            group_names=groups,
            message_count=stats["message_count"] or None,
            last_message=stats["last_message"],
        )

    if include_contact_only:
        for phone, name in addressbook_contacts.items():
            contacts_by_phone.setdefault(phone, Contact(phone=phone, name=name))

    return sorted(
        contacts_by_phone.values(),
        key=lambda contact: (contact.message_count or 0, contact.last_message or ""),
        reverse=True,
    )


def contact_to_csv_row(contact: Contact) -> list[str]:
    return [
        contact.phone,
        contact.name,
        contact.source,
        str(contact.is_in_group_chats).lower(),
        GROUP_SEPARATOR.join(contact.group_names or []),
        str(contact.message_count) if contact.message_count is not None else "",
        contact.last_message or "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
    ]


def contact_to_json(contact: Contact) -> dict[str, Any]:
    return {
        "phone": contact.phone,
        "name": contact.name,
        "sources": ["imessage"],
        "is_in_group_chats": contact.is_in_group_chats,
        "group_names": contact.group_names or [],
        "message_count": contact.message_count,
        "last_message": contact.last_message,
        "skip": False,
        "match": {
            "status": None,
            "person_id": None,
            "name": None,
            "linkedin_url": None,
            "confidence": None,
            "method": None,
            "reason": None,
        },
    }


def write_csv(path: Path, contacts: list[Contact]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADERS)
        for contact in contacts:
            writer.writerow(contact_to_csv_row(contact))


def write_jsonl(path: Path, contacts: list[Contact]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for contact in contacts:
            handle.write(json.dumps(contact_to_json(contact), sort_keys=True) + "\n")


def cmd_check(args: argparse.Namespace) -> None:
    chat_db = Path(args.chat_db).expanduser()
    addressbook = check_addressbook(args.addressbook_glob)
    result = {
        "primitive": "extract_imessage_contacts",
        "checked_at": now_iso(),
        "chat_db": check_chat_db(chat_db),
        "addressbook": addressbook,
        "addressbook_glob": args.addressbook_glob,
        "addressbook_matches": addressbook["matches"],
        "python": sys.version.split()[0],
        "platform": sys.platform,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.strict and (
        not result["chat_db"]["readable"]
        or result["chat_db"]["missing_tables"]
        or (addressbook["matches"] > 0 and not addressbook["readable"])
    ):
        raise SystemExit(1)


def cmd_open_privacy_settings(args: argparse.Namespace) -> None:
    targets = ["full-disk-access", "contacts"] if args.target == "both" else [args.target]
    urls = [PRIVACY_SETTINGS_URLS[target] for target in targets]
    result: dict[str, Any] = {
        "primitive": "extract_imessage_contacts",
        "command": "open-privacy-settings",
        "platform": sys.platform,
        "targets": targets,
        "urls": urls,
        "opened": False,
        "error": None,
    }
    if sys.platform != "darwin":
        result["error"] = "privacy settings helper is macOS-only"
        print(json.dumps(result, indent=2, sort_keys=True))
        raise SystemExit(2)
    if args.print_only:
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    try:
        for url in urls:
            subprocess.run(["open", url], check=True)
        result["opened"] = True
    except (OSError, subprocess.CalledProcessError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        print(json.dumps(result, indent=2, sort_keys=True))
        raise SystemExit(2)
    print(json.dumps(result, indent=2, sort_keys=True))


def failure_manifest(args: argparse.Namespace, run_id: str, started_at: str, error: str, diagnostics: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "created_at": started_at,
        "completed_at": now_iso(),
        "primitive": "extract_imessage_contacts",
        "status": "failed",
        "error": error,
        "diagnostics": diagnostics,
        "artifacts": {
            "csv": str(args.output_csv),
            "jsonl": str(args.output_jsonl),
            "manifest": str(args.manifest),
        },
        "counts": {
            "contacts": 0,
            "with_messages": 0,
            "with_group_context": 0,
        },
    }


def cmd_extract(args: argparse.Namespace) -> None:
    started = time.time()
    started_at = now_iso()
    run_id = args.run_id or f"imessage-{uuid4()}"
    chat_db = Path(args.chat_db).expanduser()
    output_csv = Path(args.output_csv)
    output_jsonl = Path(args.output_jsonl)
    manifest_path = Path(args.manifest)

    args.output_csv = output_csv
    args.output_jsonl = output_jsonl
    args.manifest = manifest_path

    chat_check = check_chat_db(chat_db)
    if not chat_check["readable"] or chat_check["missing_tables"]:
        manifest = failure_manifest(
            args,
            run_id,
            started_at,
            "Messages database is not readable or is missing required tables",
            {"chat_db": chat_check},
        )
        write_csv(output_csv, [])
        write_jsonl(output_jsonl, [])
        write_json(manifest_path, manifest)
        print(json.dumps(manifest, indent=2, sort_keys=True))
        raise SystemExit(2)

    try:
        addressbook_contacts, addressbook_diagnostics = read_addressbook_contacts(args.addressbook_glob)
        message_stats = aggregate_message_stats(chat_db)
        group_metadata = read_group_metadata(chat_db)
        contacts = build_contacts(
            message_stats=message_stats,
            addressbook_contacts=addressbook_contacts,
            group_metadata=group_metadata,
            include_contact_only=args.include_contact_only,
        )
        if args.limit is not None:
            contacts = contacts[: args.limit]

        write_csv(output_csv, contacts)
        write_jsonl(output_jsonl, contacts)
        manifest = {
            "run_id": run_id,
            "created_at": started_at,
            "completed_at": now_iso(),
            "elapsed_ms": int((time.time() - started) * 1000),
            "primitive": "extract_imessage_contacts",
            "status": "completed",
            "source": {
                "type": "imessage_chat_db",
                "chat_db": str(chat_db),
                "addressbook_glob": args.addressbook_glob,
            },
            "diagnostics": {
                "chat_db": chat_check,
                "addressbook": addressbook_diagnostics,
            },
            "artifacts": {
                "csv": str(output_csv),
                "jsonl": str(output_jsonl),
                "manifest": str(manifest_path),
            },
            "counts": {
                "contacts": len(contacts),
                "with_messages": sum(1 for contact in contacts if contact.message_count),
                "with_group_context": sum(1 for contact in contacts if contact.is_in_group_chats),
                "addressbook_contacts": len(addressbook_contacts),
                "message_handles": len(message_stats),
                "contact_only": sum(1 for contact in contacts if not contact.message_count),
            },
        }
        write_json(manifest_path, manifest)
        print(json.dumps(manifest, indent=2, sort_keys=True))
    except Exception as exc:
        manifest = failure_manifest(
            args,
            run_id,
            started_at,
            str(exc),
            {"chat_db": chat_check, "exception_type": type(exc).__name__},
        )
        write_csv(output_csv, [])
        write_jsonl(output_jsonl, [])
        write_json(manifest_path, manifest)
        print(json.dumps(manifest, indent=2, sort_keys=True))
        raise SystemExit(2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract iMessage contact metadata with stdlib Python")
    parser.add_argument("--chat-db", default=str(DEFAULT_CHAT_DB))
    parser.add_argument("--addressbook-glob", default=DEFAULT_ADDRESSBOOK_GLOB)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check")
    check.add_argument("--strict", action="store_true")
    check.set_defaults(func=cmd_check)

    privacy = sub.add_parser("open-privacy-settings", help="Open macOS privacy settings for Messages/Contacts access")
    privacy.add_argument(
        "--target",
        choices=["full-disk-access", "contacts", "both"],
        default="full-disk-access",
        help="Privacy pane to open. Use contacts for AddressBook name matching.",
    )
    privacy.add_argument("--print-only", action="store_true", help="Print target URLs without opening System Settings")
    privacy.set_defaults(func=cmd_open_privacy_settings)

    extract = sub.add_parser("extract")
    extract.add_argument("--output-csv", type=Path, default=DEFAULT_OUT_DIR / "imessage.contacts.csv")
    extract.add_argument("--output-jsonl", type=Path, default=DEFAULT_OUT_DIR / "imessage.contacts.jsonl")
    extract.add_argument("--manifest", type=Path, default=DEFAULT_OUT_DIR / "imessage.manifest.json")
    extract.add_argument("--run-id")
    extract.set_defaults(include_contact_only=True)
    extract.add_argument(
        "--include-contact-only",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    extract.add_argument(
        "--message-handles-only",
        dest="include_contact_only",
        action="store_false",
        help="Only export phone handles that appear in iMessage history",
    )
    extract.add_argument("--limit", type=int)
    extract.set_defaults(func=cmd_extract)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
