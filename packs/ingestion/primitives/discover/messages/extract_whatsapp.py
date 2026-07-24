#!/usr/bin/env python3
"""Extract WhatsApp contact metadata through the openclaw/wacli client.

This is the WhatsApp discovery EXTRACTOR (parallels `extract_imessage.py`): a
`WhatsAppExtractor` whose `run(...)` orchestrates the whole discovery pipeline —
install the pinned wacli, authenticate, sync once, deepen recent shallow history,
then read the local wacli SQLite store and export contact metadata. It composes
the wacli BINARY CLIENT in `whatsapp_wacli.py` (install/auth/sync/history-depth);
this module owns only the `Contact` dataclass, the store→CSV/JSONL parse/write
logic, and the completed/blocked/failed payload the WhatsApp channel consumes.

The export reads only local metadata columns from wacli's SQLite database; it
never selects message body columns. The exported CSV/JSONL contains only phone,
name, source, group metadata, direct-chat message counts, and timestamps.

Stdlib-only (plus the shared Powerpacks jsonio/csv helpers).

Usage:
    extract_whatsapp.py run      # download pinned wacli if needed, authenticate, sync once, deepen, export
    extract_whatsapp.py export   # export from an existing store without syncing

The wacli GO BINARY is still invoked as a subprocess (external tool) from inside
the client this module composes; the extractor itself is called in-process by the
WhatsApp channel. Readiness (`status`) and the re-link (`logout`) flows stay on
the client (`whatsapp_wacli.py status`/`logout`).

Changelog:
- 2026-07-24 (shared IO): the local raw-`csv.DictWriter` and hand-rolled JSONL
  writers were dropped for the shared ones — the CSV goes through the
  discover-stage `write_csv_rows` (LF terminators, unchanged-bytes writes
  skipped) and the JSONL through `common.jsonio.write_jsonl`. The per-contact
  row shapes moved out of the writers into `contact_to_csv_row` /
  `contact_to_json`, mirroring `extract_imessage`; cell values, column order,
  and the returned row counts are unchanged. Output line endings move from CRLF
  to LF (the gitignored derived artifacts are rewritten once); every other byte
  is identical.
- 2026-07-23 (cmd inline): the `cmd_run`/`cmd_export` dispatchers were inlined
  into `main` (an `if args.command == ...` chain replaces `set_defaults(func=)` +
  `args.func`). `run` constructs `WhatsAppExtractor`, calls `run`, emits, and
  returns `run_exit_code(payload)`; `export` (which has no extractor method)
  keeps its store→CSV/JSONL body inline. `main` gained an optional `argv`
  parameter (parity with `extract_gmail.main`) so it can be driven in-process;
  subcommands, flags, stdout JSON, and exit codes (completed 0, blocked 20,
  failed 1) are unchanged. `run_exit_code` stays (it is a shared status→code
  helper, not a dispatcher).
- 2026-07-23 (extractor split): split out of `whatsapp_wacli.py`. The outer
  `run` entry (formerly the `WhatsAppWacli` class) is renamed `WhatsAppExtractor`
  and lives here with the `Contact` dataclass and the store→CSV parse/write
  logic; the `run`/`export` CLI subcommands moved here too. The wacli install /
  auth / QR / sync / history-depth / group-info lifecycle stays in
  `whatsapp_wacli.py` (imported one-directionally: extractor → client). The
  WhatsApp channel now calls `WhatsAppExtractor().run(...)`. CLI stdout JSON and
  exit codes (completed 0, blocked 20, failed 1) are unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import (  # noqa: E402
    emit,
    now_iso,
    write_json,
    write_jsonl as write_jsonl_rows,
)
from packs.ingestion.primitives.discover.common import write_csv_rows  # noqa: E402
from packs.ingestion.schemas.message_contacts import CSV_HEADERS, GROUP_SEPARATOR  # noqa: E402
from packs.shared.csv_io import CsvIO  # noqa: E402
from packs.ingestion.primitives.discover.messages.whatsapp_wacli import (  # noqa: E402
    DEFAULT_AUTH_TIMEOUT,
    DEFAULT_IDLE_EXIT,
    DEFAULT_MAX_MESSAGES,
    DEFAULT_OUT_DIR,
    DEFAULT_STORE,
    DEFAULT_SYNC_TIMEOUT,
    PrimitiveBlocked,
    auth_status,
    canonicalize_phone,
    clean_name,
    emit_status,
    ensure_wacli_installed,
    group_participants_cache_path,
    history_depth_chat_states,
    history_depth_total_count,
    jid_to_phone,
    open_wacli_db,
    pairing_full_sync_status,
    refresh_contacts,
    refresh_group_info,
    resolve_effective_max,
    run_auth,
    run_history_depth_stage,
    run_sync,
    select_rows,
    store_stats,
    table_columns,
    table_exists,
    wacli_json,
    wacli_version,
    write_pairing_marker,
    write_progress,
)


DEFAULT_MAX_GROUP_PARTICIPANTS = int(os.environ.get("POWERPACKS_WACLI_MAX_GROUP_PARTICIPANTS", "30"))
DEFAULT_NAME_FALLBACK_CSV = DEFAULT_OUT_DIR / "contacts.csv"
DEFAULT_OUTPUT_CSV = DEFAULT_OUT_DIR / "wacli.contacts.csv"
DEFAULT_OUTPUT_JSONL = DEFAULT_OUT_DIR / "wacli.contacts.jsonl"
DEFAULT_MANIFEST = DEFAULT_OUTPUT_CSV.with_suffix(DEFAULT_OUTPUT_CSV.suffix + ".manifest.json")
DEFAULT_PROGRESS_JSONL = DEFAULT_MANIFEST.with_suffix(DEFAULT_MANIFEST.suffix + ".progress.jsonl")


@dataclass
class Contact:
    phone: str
    name: str = ""
    source: str = "whatsapp"
    is_in_group_chats: bool = False
    group_names: set[str] = field(default_factory=set)
    message_count: int | None = None
    last_message: str | None = None


def best_contact_name(row: dict[str, Any]) -> str:
    for key in ("full_name", "system_name", "push_name", "business_name", "first_name"):
        name = clean_name(row.get(key))
        if name:
            return name
    return ""


def epoch_to_iso(value: Any) -> str | None:
    if value in (None, "", 0):
        return None
    try:
        ts = float(value)
        if ts <= 0:
            return None
        if ts > 1e12:
            ts /= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return None


def serialize_groups(groups: set[str]) -> str:
    cleaned = sorted({clean_name(group) for group in groups if clean_name(group)}, key=str.casefold)
    return GROUP_SEPARATOR.join(cleaned)


def add_contact(contacts: dict[str, Contact], incoming: Contact) -> None:
    phone = canonicalize_phone(incoming.phone)
    if not phone:
        return
    existing = contacts.get(phone)
    if not existing:
        incoming.phone = phone
        contacts[phone] = incoming
        return
    if not existing.name and incoming.name:
        existing.name = incoming.name
    existing.is_in_group_chats = existing.is_in_group_chats or incoming.is_in_group_chats
    existing.group_names.update(incoming.group_names)
    if incoming.message_count is not None:
        existing.message_count = incoming.message_count
    if incoming.last_message and (not existing.last_message or incoming.last_message > existing.last_message):
        existing.last_message = incoming.last_message


def load_lid_map(store: Path) -> dict[str, str]:
    db_path = store / "session.db"
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if not table_exists(conn, "whatsmeow_lid_map"):
            return {}
        rows = select_rows(conn, "SELECT lid, pn FROM whatsmeow_lid_map")
        mapping: dict[str, str] = {}
        for row in rows:
            lid = str(row["lid"] or "")
            pn = str(row["pn"] or "")
            if not lid or not pn:
                continue
            mapping[lid] = pn
            if "@" not in lid:
                mapping[f"{lid}@lid"] = pn
        return mapping
    finally:
        conn.close()


def phone_for_jid(jid: str, contacts_by_jid: dict[str, dict[str, Any]], lid_map: dict[str, str]) -> str:
    contact = contacts_by_jid.get(jid) or {}
    mapped_jid = lid_map.get(jid) or ""
    mapped_contact = contacts_by_jid.get(mapped_jid) or {}
    return (
        canonicalize_phone(contact.get("phone"))
        or canonicalize_phone(mapped_contact.get("phone"))
        or jid_to_phone(mapped_jid)
        or jid_to_phone(jid)
        or ""
    )


def name_for_jid(jid: str, contacts_by_jid: dict[str, dict[str, Any]], lid_map: dict[str, str]) -> str:
    return best_contact_name(contacts_by_jid.get(jid) or {}) or best_contact_name(contacts_by_jid.get(lid_map.get(jid) or "") or {})


def names_by_phone(contacts_by_jid: dict[str, dict[str, Any]], lid_map: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for jid, row in contacts_by_jid.items():
        phone = phone_for_jid(jid, contacts_by_jid, lid_map)
        name = best_contact_name(row)
        if phone and name and phone not in out:
            out[phone] = name
    return out


def load_name_fallbacks(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    out: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in CsvIO.dict_reader(handle):
            phone = canonicalize_phone(row.get("phone"))
            name = clean_name(
                row.get("name")
                or row.get("display_name")
                or row.get("full_name")
                or row.get("contact_name")
            )
            if phone and name and phone not in out:
                out[phone] = name
    return out


def read_group_participants_cache(store: Path) -> dict[str, Any]:
    path = group_participants_cache_path(store)
    if not path.exists():
        return {"groups": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"groups": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("groups"), dict):
        return {"groups": {}}
    return payload


def load_contacts_by_jid(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    contacts: dict[str, dict[str, Any]] = {}
    if not table_exists(conn, "contacts"):
        return contacts
    for row in select_rows(
        conn,
        "SELECT jid, phone, push_name, full_name, first_name, business_name, system_name FROM contacts",
    ):
        item = dict(row)
        contacts[str(item.get("jid") or "")] = item
    return contacts


def load_message_stats(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    if not table_exists(conn, "messages"):
        return {}
    columns = table_columns(conn, "messages")
    where = []
    if "revoked" in columns:
        where.append("revoked = 0")
    if "deleted_for_me" in columns:
        where.append("deleted_for_me = 0")
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    rows = select_rows(
        conn,
        f"SELECT chat_jid, COUNT(*) AS message_count, MAX(ts) AS last_ts FROM messages{where_sql} GROUP BY chat_jid",
    )
    return {
        str(row["chat_jid"]): {
            "message_count": int(row["message_count"] or 0),
            "last_message": epoch_to_iso(row["last_ts"]),
        }
        for row in rows
    }


def group_participant_counts(conn: sqlite3.Connection) -> dict[str, int]:
    if not table_exists(conn, "group_participants"):
        return {}
    rows = select_rows(conn, "SELECT group_jid, COUNT(*) AS participant_count FROM group_participants GROUP BY group_jid")
    return {str(row["group_jid"]): int(row["participant_count"] or 0) for row in rows}


def export_contacts_from_store(
    store: Path,
    *,
    include_left_groups: bool = False,
    max_group_participants: int = DEFAULT_MAX_GROUP_PARTICIPANTS,
    name_fallback_csv: Path | None = DEFAULT_NAME_FALLBACK_CSV,
) -> tuple[dict[str, Contact], dict[str, Any]]:
    conn = open_wacli_db(store)
    try:
        contacts_by_jid = load_contacts_by_jid(conn)
        lid_map = load_lid_map(store)
        contact_names_by_phone = names_by_phone(contacts_by_jid, lid_map)
        name_fallbacks_by_phone = load_name_fallbacks(name_fallback_csv)
        for phone, name in name_fallbacks_by_phone.items():
            contact_names_by_phone.setdefault(phone, name)
        message_stats = load_message_stats(conn)
        participant_counts = group_participant_counts(conn)
        participant_cache = read_group_participants_cache(store)
        cached_groups = participant_cache.get("groups") if isinstance(participant_cache.get("groups"), dict) else {}
        cached_group_jids = set(cached_groups)
        contacts: dict[str, Contact] = {}
        group_names: dict[str, str] = {}
        active_group_jids: set[str] = set()
        skipped_large_group_jids: set[str] = set()
        diagnostics: dict[str, Any] = {
            "direct_chats": 0,
            "group_chats": 0,
            "groups_seen": 0,
            "left_groups_skipped": 0,
            "group_participants": 0,
            "group_participants_skipped_large": 0,
            "group_participants_skipped_large_members": 0,
            "group_participant_cache_groups": len(cached_group_jids),
            "group_participant_cache_rows": sum(
                len(group.get("participants") or [])
                for group in cached_groups.values()
                if isinstance(group, dict)
            ),
            "message_stats_chats": len(message_stats),
            "lid_map_rows": len(lid_map),
            "name_fallback_rows": len(name_fallbacks_by_phone),
            "queries_read_message_body_columns": False,
        }

        for group_jid, group in cached_groups.items():
            if not isinstance(group, dict):
                continue
            participant_count = int(group.get("participant_count") or len(group.get("participants") or []))
            if max_group_participants > 0 and participant_count > max_group_participants:
                diagnostics["group_participants_skipped_large"] += 1
                diagnostics["group_participants_skipped_large_members"] += participant_count
                continue
            group_name = clean_name(group.get("name")) or str(group_jid)
            group_names[str(group_jid)] = group_name
            active_group_jids.add(str(group_jid))
            for participant in group.get("participants") or []:
                if not isinstance(participant, dict):
                    continue
                phone = canonicalize_phone(participant.get("phone"))
                if not phone:
                    continue
                diagnostics["group_participants"] += 1
                add_contact(contacts, Contact(
                    phone=phone,
                    name=clean_name(participant.get("name")) or contact_names_by_phone.get(phone, ""),
                    is_in_group_chats=True,
                    group_names={group_name},
                ))

        if table_exists(conn, "groups"):
            for row in select_rows(conn, "SELECT jid, name, left_at FROM groups"):
                jid = str(row["jid"] or "")
                if not jid:
                    continue
                diagnostics["groups_seen"] += 1
                group_names[jid] = clean_name(row["name"]) or jid
                left_at = row["left_at"]
                if left_at and not include_left_groups:
                    diagnostics["left_groups_skipped"] += 1
                    continue
                active_group_jids.add(jid)

        if table_exists(conn, "chats"):
            for row in select_rows(conn, "SELECT jid, kind, name, last_message_ts FROM chats"):
                jid = str(row["jid"] or "")
                kind = str(row["kind"] or "unknown")
                name = clean_name(row["name"])
                if kind == "group" or "@g.us" in jid:
                    diagnostics["group_chats"] += 1
                    group_names.setdefault(jid, name or jid)
                    if include_left_groups or jid in active_group_jids:
                        active_group_jids.add(jid)
                    continue

                phone = phone_for_jid(jid, contacts_by_jid, lid_map)
                if not phone:
                    continue
                diagnostics["direct_chats"] += 1
                contact_row = contacts_by_jid.get(jid) or {}
                stats = message_stats.get(jid) or {}
                last_message = stats.get("last_message") or epoch_to_iso(row["last_message_ts"])
                add_contact(contacts, Contact(
                    phone=phone,
                    name=name or best_contact_name(contact_row) or contact_names_by_phone.get(phone, ""),
                    message_count=stats.get("message_count"),
                    last_message=last_message,
                ))

        if table_exists(conn, "group_participants"):
            for row in select_rows(conn, "SELECT group_jid, user_jid FROM group_participants"):
                group_jid = str(row["group_jid"] or "")
                if group_jid in cached_group_jids:
                    continue
                if group_jid not in active_group_jids:
                    continue
                participant_count = participant_counts.get(group_jid, 0)
                if max_group_participants > 0 and participant_count > max_group_participants:
                    if group_jid not in skipped_large_group_jids:
                        skipped_large_group_jids.add(group_jid)
                        diagnostics["group_participants_skipped_large"] += 1
                        diagnostics["group_participants_skipped_large_members"] += participant_count
                    continue
                user_jid = str(row["user_jid"] or "")
                phone = phone_for_jid(user_jid, contacts_by_jid, lid_map)
                if not phone:
                    continue
                diagnostics["group_participants"] += 1
                group_name = group_names.get(group_jid) or group_jid
                add_contact(contacts, Contact(
                    phone=phone,
                    name=name_for_jid(user_jid, contacts_by_jid, lid_map) or contact_names_by_phone.get(phone, ""),
                    is_in_group_chats=True,
                    group_names={group_name},
                ))

        diagnostics.update({
            "contacts_exported": len(contacts),
            "contacts_with_message_count": sum(1 for item in contacts.values() if item.message_count is not None),
            "contacts_in_groups": sum(1 for item in contacts.values() if item.is_in_group_chats),
        })
        return contacts, diagnostics
    finally:
        conn.close()


def sorted_contacts(contacts: dict[str, Contact]) -> list[Contact]:
    return sorted(
        contacts.values(),
        key=lambda item: ((item.message_count or 0), item.last_message or "", item.phone),
        reverse=True,
    )


def contact_to_csv_row(contact: Contact) -> dict[str, str]:
    """One contact as a `CSV_HEADERS`-keyed row. WhatsApp owns only the
    `whatsapp_*` per-channel cells; the imessage and match columns stay empty for
    the import stage to fill."""
    message_count = "" if contact.message_count is None else str(contact.message_count)
    last_message = contact.last_message or ""
    return {
        "phone": contact.phone,
        "name": contact.name,
        "source": contact.source,
        "is_in_group_chats": "true" if contact.is_in_group_chats else "false",
        "group_names": serialize_groups(contact.group_names),
        "message_count": message_count,
        "imessage_message_count": "",
        "whatsapp_message_count": message_count,
        "last_message": last_message,
        "imessage_last_message": "",
        "whatsapp_last_message": last_message,
        "skip": "",
        "match_status": "",
        "matched_person_id": "",
        "matched_name": "",
        "matched_linkedin_url": "",
        "match_confidence": "",
        "match_method": "",
        "match_reason": "",
    }


def contact_to_json(contact: Contact) -> dict[str, Any]:
    """One contact as the JSONL record: typed nulls/booleans where the CSV uses
    empty cells, and the match block the import stage later fills in."""
    return {
        "phone": contact.phone,
        "name": contact.name,
        "sources": [contact.source],
        "is_in_group_chats": contact.is_in_group_chats,
        "group_names": sorted(contact.group_names, key=str.casefold),
        "message_count": contact.message_count,
        "imessage_message_count": None,
        "whatsapp_message_count": contact.message_count,
        "last_message": contact.last_message,
        "imessage_last_message": None,
        "whatsapp_last_message": contact.last_message,
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


def write_csv(path: Path, contacts: dict[str, Contact]) -> int:
    """Write the message-contact CSV through the discover-stage writer, so an
    unchanged rerun leaves the file (and its mtime) alone. Returns the row count."""
    rows = sorted_contacts(contacts)
    write_csv_rows(path, CSV_HEADERS, [contact_to_csv_row(contact) for contact in rows])
    return len(rows)


def write_jsonl(path: Path | None, contacts: dict[str, Contact]) -> int:
    """Write the message-contact JSONL through the shared newline-delimited
    writer, returning the row count. A `None` path means the caller opted out."""
    if path is None:
        return 0
    return write_jsonl_rows(path, (contact_to_json(contact) for contact in sorted_contacts(contacts)))


def completed_payload(
    *,
    store: Path,
    output_csv: Path,
    output_jsonl: Path | None,
    manifest: Path,
    progress_jsonl: Path | None,
    wacli_info: dict[str, Any],
    doctor: dict[str, Any],
    stats: dict[str, Any],
    refresh: dict[str, Any],
    group_info: dict[str, Any],
    diagnostics: dict[str, Any],
    csv_rows: int,
    jsonl_rows: int,
    elapsed_ms: int,
    pairing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "primitive": "messages/extract_whatsapp",
        "status": "completed",
        "message": f"Imported {csv_rows} WhatsApp contacts.",
        "completed_at": now_iso(),
        "elapsed_ms": elapsed_ms,
        "store": str(store),
        "pairing": pairing or {},
        "artifacts": {
            "csv": str(output_csv),
            "jsonl": str(output_jsonl) if output_jsonl else None,
            "manifest": str(manifest),
            "progress_jsonl": str(progress_jsonl) if progress_jsonl else None,
        },
        "wacli": wacli_info,
        "doctor": doctor,
        "refresh_contacts": refresh,
        "group_info_refresh": group_info,
        "store_stats": stats,
        "counts": {
            "csv_rows": csv_rows,
            "jsonl_rows": jsonl_rows,
            "contacts": csv_rows,
            "with_message_count": diagnostics.get("contacts_with_message_count", 0),
            "in_group_chats": diagnostics.get("contacts_in_groups", 0),
        },
        "diagnostics": diagnostics,
        "privacy": {
            "export_reads_message_bodies": False,
            "export_reads_columns": "contacts, chats, groups, group_participants, and aggregate message metadata only",
        },
    }


class WhatsAppExtractor:
    """WhatsApp discovery extractor: install the pinned wacli, authenticate, sync
    once, deepen recent history, and export local metadata. Composes the wacli GO
    BINARY client (still invoked as a subprocess — external tool) but is itself
    called in-process by the WhatsApp channel. ``run`` does all the work and
    returns the completed/blocked/failed payload (with ``status``); it writes the
    manifest and progress artifacts but does not print to stdout — the CLI wrapper
    emits."""

    def __init__(self, *, store: str | Path = DEFAULT_STORE) -> None:
        self.store = Path(store)

    def run(
        self,
        *,
        output_csv: str | Path,
        output_jsonl: str | Path | None,
        manifest: str | Path,
        progress_jsonl: str | Path | None,
        max_messages: int,
        max_group_participants: int = DEFAULT_MAX_GROUP_PARTICIPANTS,
        sync_timeout: int = DEFAULT_SYNC_TIMEOUT,
        name_fallback_csv: str | Path | None = DEFAULT_NAME_FALLBACK_CSV,
        idle_exit: str = DEFAULT_IDLE_EXIT,
        auth_timeout: int = DEFAULT_AUTH_TIMEOUT,
        group_info_timeout: int = 60,
        group_info_interval: float = 0.2,
        include_left_groups: bool = False,
        no_install: bool = False,
        no_open_qr_page: bool = False,
    ) -> dict[str, Any]:
        """Install/auth/sync/deepen/export in one pass. Returns the payload:
        ``status: completed`` on success (with counts + pairing), ``status:
        blocked_user_action`` when a QR scan / install step is needed, or
        ``status: failed`` on any error. Writes the manifest + progress artifacts."""
        started = time.time()
        store = self.store
        output_csv = Path(output_csv)
        output_jsonl = Path(output_jsonl) if output_jsonl else None
        manifest = Path(manifest)
        progress_jsonl = Path(progress_jsonl) if progress_jsonl else None
        name_fallback_csv = Path(name_fallback_csv) if name_fallback_csv else None
        store.mkdir(parents=True, exist_ok=True)
        write_progress(progress_jsonl, {"event": "started", "store": str(store)})

        try:
            wacli_info = ensure_wacli_installed(install=not no_install)
            write_progress(progress_jsonl, {"event": "wacli_ready", "wacli": wacli_info})
            existing_messages_at_start = history_depth_total_count(store)
            doctor = wacli_json(store, ["doctor"], timeout=60)
            status = auth_status(store)
            auth_summary: dict[str, Any] = {"authenticated_before": status.get("authenticated")}
            if not status.get("authenticated"):
                auth_summary.update(run_auth(
                    store,
                    timeout=auth_timeout,
                    idle_exit=idle_exit,
                    open_qr_page=not no_open_qr_page,
                ))
                status = auth_status(store, include_linked_jid=True)
                if not status.get("authenticated"):
                    raise PrimitiveBlocked({
                        "status": "blocked_user_action",
                        "message": "WhatsApp needs a QR scan. Scan it, then rerun $import-messages.",
                        "store": str(store),
                    })
            auth_summary["authenticated_after"] = status.get("authenticated")
            if not auth_summary.get("authenticated_before") and status.get("authenticated"):
                write_pairing_marker(store)  # we just paired with full sync
            pairing = pairing_full_sync_status(store, authenticated=bool(status.get("authenticated")))
            if pairing.get("state") == "pre_full_sync":
                emit_status(pairing["hint"])
            write_progress(progress_jsonl, {"event": "authenticated", "auth": auth_summary, "pairing": pairing})

            cold_start = existing_messages_at_start == 0
            before_states = history_depth_chat_states(store)
            before_total_messages = history_depth_total_count(store)
            effective_max_messages_value = (
                0
                if cold_start
                else resolve_effective_max(max_messages, before_total_messages)
            )
            sync_summary = run_sync(
                store,
                timeout=sync_timeout,
                idle_exit=idle_exit,
                max_messages=effective_max_messages_value,
            )
            sync_summary["strategy"] = "cold_full" if cold_start else "incremental"
            sync_summary["incremental"] = not cold_start
            sync_summary["requested_max_messages"] = max_messages
            sync_summary["existing_messages_at_start"] = existing_messages_at_start
            sync_summary["existing_messages_before_sync"] = before_total_messages
            write_progress(progress_jsonl, {"event": "synced", "sync": sync_summary})
            emit_status("Deepening recent shallow WhatsApp conversations in paced batches.")
            doctor_data = doctor.get("data") if isinstance(doctor.get("data"), dict) else {}
            linked_jid = str(
                status.get("linked_jid")
                or doctor_data.get("linked_jid")
                or ""
            )
            history_depth = run_history_depth_stage(
                store,
                out_dir=manifest.parent / "history-depth",
                before_states=before_states,
                before_total_messages=before_total_messages,
                cold_start=cold_start,
                exclude_jids={linked_jid} if linked_jid else set(),
            )
            write_progress(
                progress_jsonl,
                {
                    "event": "history_depth_completed",
                    "status": history_depth.get("status"),
                    "counts": history_depth.get("counts"),
                },
            )
            group_info = refresh_group_info(
                store,
                timeout=group_info_timeout,
                min_interval=group_info_interval,
            )
            write_progress(progress_jsonl, {"event": "group_info_refreshed", "group_info_refresh": group_info})
            refresh = refresh_contacts(store)
            stats = store_stats(store)
            contacts, diagnostics = export_contacts_from_store(
                store,
                include_left_groups=include_left_groups,
                max_group_participants=max_group_participants,
                name_fallback_csv=name_fallback_csv,
            )
            csv_rows = write_csv(output_csv, contacts)
            jsonl_rows = write_jsonl(output_jsonl, contacts)
            elapsed_ms = int((time.time() - started) * 1000)
            emit_status("WhatsApp sync finished.")
            payload = completed_payload(
                store=store,
                output_csv=output_csv,
                output_jsonl=output_jsonl,
                manifest=manifest,
                progress_jsonl=progress_jsonl,
                wacli_info=wacli_info,
                doctor=doctor,
                stats=stats,
                refresh=refresh,
                group_info=group_info,
                diagnostics=diagnostics,
                csv_rows=csv_rows,
                jsonl_rows=jsonl_rows,
                elapsed_ms=elapsed_ms,
                pairing=pairing,
            )
            payload["command"] = "run"
            payload["auth"] = auth_summary
            payload["sync"] = sync_summary
            payload["history_depth"] = history_depth
            write_json(manifest, payload)
            write_progress(progress_jsonl, {"event": "completed", "counts": payload["counts"]})
            return payload
        except PrimitiveBlocked as exc:
            payload = {
                "primitive": "messages/extract_whatsapp",
                "command": "run",
                **exc.payload,
                "store": str(store),
                "artifacts": {"manifest": str(manifest), "progress_jsonl": str(progress_jsonl) if progress_jsonl else None},
            }
            write_json(manifest, payload)
            write_progress(progress_jsonl, {"event": "blocked", "message": payload.get("message")})
            return payload
        except Exception as exc:
            status_after_failure: dict[str, Any] = {}
            try:
                status_after_failure = auth_status(store)
            except Exception as status_exc:
                status_after_failure = {"error": f"{type(status_exc).__name__}: {status_exc}"}
            payload = {
                "primitive": "messages/extract_whatsapp",
                "command": "run",
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "store": str(store),
                "auth_after_failure": status_after_failure,
                "artifacts": {"manifest": str(manifest), "progress_jsonl": str(progress_jsonl) if progress_jsonl else None},
            }
            write_json(manifest, payload)
            write_progress(progress_jsonl, {"event": "failed", "error": payload["error"]})
            return payload


def run_exit_code(payload: dict[str, Any]) -> int:
    """Map a ``run`` payload status to the CLI exit code (0 completed, 20 blocked,
    1 failed) — the same mapping the old ``cmd_run`` returned directly."""
    status = payload.get("status")
    if status == "completed":
        return 0
    if status == "blocked_user_action":
        return 20
    return 1


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store", default=str(DEFAULT_STORE), help="wacli store directory")


def add_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    parser.add_argument("--output-jsonl", default=str(DEFAULT_OUTPUT_JSONL))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--include-left-groups", action="store_true", help="include groups wacli marks as left")
    parser.add_argument("--max-group-participants", type=int, default=DEFAULT_MAX_GROUP_PARTICIPANTS,
                        help="Export participants only for groups at or below this size; set <=0 to disable the cap")
    parser.add_argument("--name-fallback-csv", default=str(DEFAULT_NAME_FALLBACK_CSV),
                        help="Optional contacts CSV used only to fill missing names by phone")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract WhatsApp contact metadata through openclaw/wacli")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="install wacli if needed, authenticate, sync once, deepen recent history, and export metadata")
    add_common_args(run)
    add_output_args(run)
    run.add_argument("--progress-jsonl", default=str(DEFAULT_PROGRESS_JSONL))
    run.add_argument("--max-messages", type=int, default=DEFAULT_MAX_MESSAGES)
    run.add_argument("--idle-exit", default=DEFAULT_IDLE_EXIT)
    run.add_argument("--auth-timeout", type=int, default=DEFAULT_AUTH_TIMEOUT)
    run.add_argument("--sync-timeout", type=int, default=DEFAULT_SYNC_TIMEOUT)
    run.add_argument("--group-info-timeout", type=int, default=60)
    run.add_argument("--group-info-interval", type=float, default=0.2)
    run.add_argument("--no-install", action="store_true", help="deprecated no-op; the pinned wacli fork always auto-downloads when missing or stale")
    run.add_argument("--no-open-qr-page", action="store_true", help="render QR artifacts without opening the local browser page")

    export = sub.add_parser("export", help="export metadata from an existing wacli store without syncing")
    add_common_args(export)
    add_output_args(export)

    args = parser.parse_args(argv)

    if args.command == "run":
        # Construct the extractor, run the full install/auth/sync/deepen/export
        # pass, emit the payload, and map its status to the exit code.
        payload = WhatsAppExtractor(store=args.store).run(
            output_csv=args.output_csv,
            output_jsonl=args.output_jsonl,
            manifest=args.manifest,
            progress_jsonl=args.progress_jsonl,
            max_messages=args.max_messages,
            max_group_participants=args.max_group_participants,
            sync_timeout=args.sync_timeout,
            name_fallback_csv=args.name_fallback_csv,
            idle_exit=args.idle_exit,
            auth_timeout=args.auth_timeout,
            group_info_timeout=args.group_info_timeout,
            group_info_interval=args.group_info_interval,
            include_left_groups=args.include_left_groups,
            no_install=args.no_install,
            no_open_qr_page=args.no_open_qr_page,
        )
        emit(payload)
        return run_exit_code(payload)

    # args.command == "export": export metadata from an existing wacli store
    # without syncing (no extractor method). completed -> 0, any error -> 1.
    started = time.time()
    store = Path(args.store)
    output_csv = Path(args.output_csv)
    output_jsonl = Path(args.output_jsonl) if args.output_jsonl else None
    manifest = Path(args.manifest)
    name_fallback_csv = Path(args.name_fallback_csv) if getattr(args, "name_fallback_csv", None) else None
    try:
        contacts, diagnostics = export_contacts_from_store(
            store,
            include_left_groups=args.include_left_groups,
            max_group_participants=args.max_group_participants,
            name_fallback_csv=name_fallback_csv,
        )
        csv_rows = write_csv(output_csv, contacts)
        jsonl_rows = write_jsonl(output_jsonl, contacts)
        payload = completed_payload(
            store=store,
            output_csv=output_csv,
            output_jsonl=output_jsonl,
            manifest=manifest,
            progress_jsonl=None,
            wacli_info=wacli_version(),
            doctor={},
            stats={},
            refresh={},
            group_info={},
            diagnostics=diagnostics,
            csv_rows=csv_rows,
            jsonl_rows=jsonl_rows,
            elapsed_ms=int((time.time() - started) * 1000),
        )
        payload["command"] = "export"
        write_json(manifest, payload)
        emit(payload)
        return 0
    except Exception as exc:
        payload = {
            "primitive": "messages/extract_whatsapp",
            "command": "export",
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "store": str(store),
        }
        write_json(manifest, payload)
        emit(payload)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
