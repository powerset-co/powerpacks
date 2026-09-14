"""Read-only wacli connection, schema, JID, contact, and group metadata helpers.

wacli owns the database; Powerpacks only reads it. Guarded metadata extraction
lives here, body reads live in `message_db.py`, and history-depth aggregates
live in `depth_db.py`.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[6]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.discover.messages.wacli.runtime import PrimitiveFailed  # noqa: E402
from packs.ingestion.primitives.discover.messages.wacli.util import (  # noqa: E402
    canonicalize_phone,
    jid_to_phone,
)

BODY_COLUMN_NAMES = {
    "text",
    "display_text",
    "media_caption",
    "filename",
    "direct_path",
    "media_key",
    "file_sha256",
    "file_enc_sha256",
    "local_path",
}
DatabaseError = sqlite3.Error


def open_readonly_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise PrimitiveFailed(f"wacli database not found at {db_path}")
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def open_wacli_db(store: Path) -> sqlite3.Connection:
    return open_readonly_db(store / "wacli.db")


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not table_exists(conn, table):
        return set()
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


def assert_metadata_query(sql: str) -> None:
    lowered = sql.lower()
    for name in BODY_COLUMN_NAMES:
        if re.search(rf"\b{name}\b", lowered):
            raise PrimitiveFailed(f"internal error: query selects body column {name}")


def select_rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    assert_metadata_query(sql)
    return list(conn.execute(sql, params))


def whatsapp_epoch_to_iso(value: Any) -> str | None:
    if value in (None, "", 0):
        return None
    try:
        timestamp = float(value)
        if timestamp <= 0:
            return None
        if timestamp > 1e12:
            timestamp /= 1000
        return (
            datetime.fromtimestamp(timestamp, tz=timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (TypeError, ValueError, OSError):
        return None


def whatsapp_phone_digit_forms(phone: str | None) -> tuple[str, ...]:
    """Full WhatsApp digits plus the comparison form used by older readers.

    WhatsApp stores the country code in a DM JID. For a US number, the full
    ``1XXXXXXXXXX`` form must therefore remain present even when a comparison
    key strips the leading ``1``. A bare ten-digit US number gets the inverse
    treatment: its canonical ``1``-prefixed form is added too.
    """
    value = str(phone or "").strip()
    full_digits = re.sub(r"\D", "", value.split("@", 1)[0])
    canonical = canonicalize_phone(value)
    canonical_digits = canonical.removeprefix("+") if canonical else ""
    if not canonical_digits:
        return ()
    forms: list[str] = []
    for digits in (full_digits, canonical_digits):
        if digits and digits not in forms:
            forms.append(digits)
    if len(canonical_digits) == 11 and canonical_digits.startswith("1"):
        local_digits = canonical_digits[1:]
        if local_digits not in forms:
            forms.append(local_digits)
    return tuple(forms)


def whatsapp_dm_jids(phones: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    jids: list[str] = []
    for phone in phones:
        for digits in whatsapp_phone_digit_forms(phone):
            jid = f"{digits}@s.whatsapp.net"
            if jid not in jids:
                jids.append(jid)
    return tuple(jids)


def load_lid_map(store: Path) -> dict[str, str]:
    db_path = store / "session.db"
    if not db_path.exists():
        return {}
    conn = open_readonly_db(db_path)
    try:
        columns = table_columns(conn, "whatsmeow_lid_map")
        if not {"lid", "pn"}.issubset(columns):
            return {}
        mapping: dict[str, str] = {}
        for row in select_rows(conn, "SELECT lid, pn FROM whatsmeow_lid_map"):
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


def phone_for_jid(
    jid: str,
    contacts_by_jid: dict[str, dict[str, Any]],
    lid_map: dict[str, str],
) -> str:
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


def contact_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    columns = table_columns(conn, "contacts")
    if "jid" not in columns:
        return []
    projection = [
        column if column in columns else f"NULL AS {column}"
        for column in (
            "jid",
            "phone",
            "push_name",
            "full_name",
            "first_name",
            "business_name",
            "system_name",
        )
    ]
    return [dict(row) for row in select_rows(conn, f"SELECT {', '.join(projection)} FROM contacts")]


def contacts_by_jid(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("jid") or ""): row
        for row in contact_rows(conn)
    }


def message_stats(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    columns = table_columns(conn, "messages")
    if "chat_jid" not in columns:
        return {}
    where = []
    if "revoked" in columns:
        where.append("revoked = 0")
    if "deleted_for_me" in columns:
        where.append("deleted_for_me = 0")
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    last_ts = "MAX(ts)" if "ts" in columns else "NULL"
    rows = select_rows(
        conn,
        f"SELECT chat_jid, COUNT(*) AS message_count, {last_ts} AS last_ts "
        f"FROM messages{where_sql} GROUP BY chat_jid",
    )
    return {
        str(row["chat_jid"]): {
            "message_count": int(row["message_count"] or 0),
            "last_message": whatsapp_epoch_to_iso(row["last_ts"]),
        }
        for row in rows
    }


def group_participant_counts(conn: sqlite3.Connection) -> dict[str, int]:
    columns = table_columns(conn, "group_participants")
    if "group_jid" not in columns:
        return {}
    rows = select_rows(
        conn,
        "SELECT group_jid, COUNT(*) AS participant_count "
        "FROM group_participants GROUP BY group_jid",
    )
    return {
        str(row["group_jid"]): int(row["participant_count"] or 0)
        for row in rows
    }


def group_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    columns = table_columns(conn, "groups")
    if "jid" not in columns:
        return []
    name = "name" if "name" in columns else "NULL AS name"
    left_at = "left_at" if "left_at" in columns else "NULL AS left_at"
    return select_rows(conn, f"SELECT jid, {name}, {left_at} FROM groups")


def chat_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    columns = table_columns(conn, "chats")
    if "jid" not in columns:
        return []
    projection = [
        column if column in columns else f"NULL AS {column}"
        for column in ("jid", "kind", "name", "last_message_ts")
    ]
    return select_rows(conn, f"SELECT {', '.join(projection)} FROM chats")


def group_participant_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    columns = table_columns(conn, "group_participants")
    if not {"group_jid", "user_jid"}.issubset(columns):
        return []
    return select_rows(conn, "SELECT group_jid, user_jid FROM group_participants")


def group_chat_jids(store: Path) -> list[str]:
    db_path = store / "wacli.db"
    if not db_path.exists():
        return []
    conn = open_wacli_db(store)
    try:
        if not table_exists(conn, "chats"):
            return []
        rows = select_rows(conn, "SELECT jid FROM chats WHERE kind = 'group' OR jid LIKE '%@g.us' ORDER BY jid")
        return [str(row["jid"]) for row in rows if row["jid"]]
    finally:
        conn.close()
