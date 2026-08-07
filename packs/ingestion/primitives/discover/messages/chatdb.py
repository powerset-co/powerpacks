"""Shared read-only access policy for Apple's Messages ``chat.db``.

The message-contact extractor, dossier collector, and logbook all read the same
Apple store.  This module owns the stable mechanics they share: opening and
probing the database, normalizing handles and timestamps, excluding tapback
rows, and decoding/fetching message bodies.  Callers retain responsibility for
privacy scope and output policy.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


APPLE_EPOCH_OFFSET = 978_307_200
NS_PER_SEC = 1_000_000_000
REACTION_TYPE_MIN = 2_000
REACTION_TYPE_MAX = 3_006
_HANDLE_ROWS_CACHE: dict[str, tuple[tuple[int, str], ...]] = {}
DatabaseError = sqlite3.Error


def open_sqlite_readonly(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    """Open one SQLite file read-only with mapping-style rows."""
    suffix = "&immutable=1" if immutable else ""
    conn = sqlite3.connect(f"file:{Path(path)}?mode=ro{suffix}", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def sqlite_tables(path: Path) -> set[str]:
    """Return the table names visible through a read-only connection."""
    with open_sqlite_readonly(path) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(row["name"]) for row in rows}


def probe_chat_db(
    path: Path,
    *,
    required_tables: tuple[str, ...] = ("message", "handle"),
) -> dict[str, Any]:
    """Report existence, readability, and required Apple Messages tables."""
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "readable": False,
        "required_tables": list(required_tables),
        "missing_tables": [],
        "error": None,
    }
    if not path.exists():
        result["error"] = "chat.db does not exist"
        return result
    try:
        tables = sqlite_tables(path)
        result["readable"] = True
        result["missing_tables"] = [table for table in required_tables if table not in tables]
        result["has_group_tables"] = "chat" in tables and "chat_handle_join" in tables
    except sqlite3.Error as exc:
        result["error"] = str(exc)
    return result


def probe_message_counts(path: Path) -> dict[str, Any]:
    """Probe the live Messages store and return its basic row counts."""
    result: dict[str, Any] = {
        "exists": path.exists(),
        "readable": False,
        "messages": 0,
        "handles": 0,
        "error": None,
    }
    if not path.exists():
        result["error"] = "chat.db does not exist"
        return result
    try:
        with open_sqlite_readonly(path, immutable=True) as conn:
            result["messages"] = conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]
            result["handles"] = conn.execute("SELECT COUNT(*) FROM handle").fetchone()[0]
            result["readable"] = True
    except sqlite3.Error as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def owner_phone_identifiers(path: Path) -> list[str]:
    """Read the owner's phone identifiers from Messages account metadata."""
    if not path.exists():
        return []
    identifiers: list[str] = []
    queries = (
        ("SELECT DISTINCT account_login FROM chat", "P:"),
        (
            "SELECT DISTINCT destination_caller_id FROM message "
            "WHERE is_from_me = 0 AND destination_caller_id LIKE '+%'",
            "",
        ),
    )
    try:
        with open_sqlite_readonly(path) as conn:
            for sql, prefix in queries:
                for row in conn.execute(sql):
                    value = str(row[0] or "")
                    if (not prefix or value.startswith(prefix)) and value.removeprefix(prefix) not in identifiers:
                        identifiers.append(value.removeprefix(prefix))
    except (sqlite3.Error, OSError):
        pass
    return identifiers


def apple_timestamp_to_iso(value: int | float | None) -> str | None:
    """Normalize Apple nanoseconds/seconds or a Unix timestamp to UTC ISO-8601."""
    if value is None:
        return None
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return None
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


def phone_lookup_key(raw: str) -> str:
    """Return the matching key used for Apple phone handles.

    North-American ``+1`` is removed so AddressBook and Messages formatting do
    not split a person; other country codes remain part of the key.
    """
    digits = re.sub(r"[^\d]", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return digits


def is_phone_identifier(identifier: str) -> bool:
    """Whether an Apple handle is plausibly a phone rather than email/chat ID."""
    if not identifier or "@" in identifier or identifier.startswith("urn:") or identifier.startswith("chat"):
        return False
    return len(re.sub(r"[^\d]", "", identifier)) >= 7


def resolve_handle_ids(
    conn: sqlite3.Connection,
    identifiers: Iterable[str],
    *,
    cache_key: Path | str | None = None,
) -> list[int]:
    """Resolve phone/email identifiers to deterministic Apple handle ROWIDs."""
    values = tuple(str(value or "") for value in identifiers)
    wanted_emails = {value.strip().casefold() for value in values if "@" in value and value.strip()}
    wanted_phones = {
        phone_lookup_key(value) for value in values if is_phone_identifier(value) and phone_lookup_key(value)
    }
    if not wanted_emails and not wanted_phones:
        return []
    key = str(cache_key) if cache_key is not None else ""
    rows = _HANDLE_ROWS_CACHE.get(key) if key else None
    if rows is None:
        rows = tuple(
            (int(row["rid"]), str(row["ident"] or ""))
            for row in conn.execute("SELECT ROWID AS rid, id AS ident FROM handle ORDER BY ROWID")
        )
        if key:
            _HANDLE_ROWS_CACHE[key] = rows
    found: list[int] = []
    for rowid, identifier in rows:
        if identifier.casefold() in wanted_emails or (
            is_phone_identifier(identifier) and phone_lookup_key(identifier) in wanted_phones
        ):
            found.append(rowid)
    return found


def is_reaction_type(value: int | None) -> bool:
    """Whether ``associated_message_type`` represents an Apple tapback row."""
    return value is not None and REACTION_TYPE_MIN <= int(value) <= REACTION_TYPE_MAX


def not_reaction_predicate(alias: str = "m") -> str:
    """SQL predicate selecting real messages rather than tapback metadata."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError(f"invalid SQL alias: {alias!r}")
    column = f"{alias}.associated_message_type"
    return f"({column} IS NULL OR {column} < {REACTION_TYPE_MIN} OR {column} > {REACTION_TYPE_MAX})"


def decode_attributed_body(blob: Any) -> str:
    """Extract plain text from Apple's archived ``NSAttributedString`` payload."""
    if not blob:
        return ""
    data = bytes(blob) if not isinstance(blob, bytes) else blob
    try:
        segment = data.split(b"NSString", 1)[1][5:]
        if not segment:
            return ""
        if segment[0] == 0x81:
            length = int.from_bytes(segment[1:3], "little")
            start = 3
        else:
            length = segment[0]
            start = 1
        return segment[start : start + length].decode("utf-8", "replace").strip()
    except (IndexError, UnicodeDecodeError):
        return ""


def message_text(row: sqlite3.Row) -> str:
    """Prefer ``message.text`` and fall back to the attributed-body archive."""
    return str(row["text"] or "").strip() or decode_attributed_body(row["attributed_body"])


def _message_content_key(row: sqlite3.Row) -> tuple[int, tuple[int, str, str]]:
    """Return Apple date plus the emitted direction, handle, and full text."""
    return (
        int(row["date"] or 0),
        (
            int(row["is_from_me"] or 0),
            str(row["handle"] or ""),
            message_text(row),
        ),
    )


def query_direct_messages(
    conn: sqlite3.Connection,
    handle_ids: Iterable[int],
    *,
    since_rowid: int = 0,
    limit: int | None = None,
    newest_first: bool = False,
) -> list[sqlite3.Row]:
    """Fetch non-reaction direct-message rows for resolved Apple handles."""
    ids = tuple(dict.fromkeys(int(value) for value in handle_ids))
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    params: tuple[int, ...] = (*ids, int(since_rowid))
    sql = f"""
SELECT m.ROWID AS rid, m.guid AS guid, m.text AS text,
       m.attributedBody AS attributed_body, m.date AS date,
       m.is_from_me AS is_from_me, h.id AS handle
FROM message m
JOIN chat_message_join cmj ON cmj.message_id=m.ROWID
JOIN chat c ON c.ROWID=cmj.chat_id
LEFT JOIN handle h ON h.ROWID=m.handle_id
WHERE m.handle_id IN ({placeholders})
  AND c.chat_identifier NOT LIKE 'chat%'
  AND {not_reaction_predicate("m")}
  AND m.ROWID > ?
"""
    rows = conn.execute(sql, params).fetchall()
    # Date + semantic payload is total for emitted rows; physical ROWID can change.
    rows.sort(key=_message_content_key, reverse=newest_first)
    return rows[:limit] if limit is not None else rows


def count_direct_messages(conn: sqlite3.Connection, handle_ids: Iterable[int]) -> int:
    """Count non-reaction direct messages for resolved Apple handles."""
    ids = tuple(dict.fromkeys(int(value) for value in handle_ids))
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    sql = f"""
SELECT COUNT(*)
FROM message m
JOIN chat_message_join cmj ON cmj.message_id=m.ROWID
JOIN chat c ON c.ROWID=cmj.chat_id
WHERE m.handle_id IN ({placeholders})
  AND c.chat_identifier NOT LIKE 'chat%'
  AND {not_reaction_predicate("m")}
"""
    return int(conn.execute(sql, ids).fetchone()[0])


def query_group_messages(
    conn: sqlite3.Connection,
    chat_rowid: int,
    *,
    since_rowid: int = 0,
) -> list[sqlite3.Row]:
    """Fetch non-reaction messages from one Apple group chat."""
    sql = f"""
SELECT m.ROWID AS rid, m.guid AS guid, m.text AS text,
       m.attributedBody AS attributed_body, m.date AS date,
       m.is_from_me AS is_from_me, h.id AS handle
FROM chat_message_join cmj
JOIN message m ON m.ROWID=cmj.message_id
LEFT JOIN handle h ON h.ROWID=m.handle_id
WHERE cmj.chat_id=?
  AND {not_reaction_predicate("m")}
  AND m.ROWID > ?
"""
    rows = conn.execute(sql, (int(chat_rowid), int(since_rowid))).fetchall()
    # Date + semantic payload is total for emitted rows; physical ROWID can change.
    rows.sort(key=_message_content_key)
    return rows


def query_group_chats_for_handles(
    conn: sqlite3.Connection,
    handle_ids: Iterable[int],
) -> list[sqlite3.Row]:
    """Fetch group-chat identity rows containing any resolved handle."""
    ids = tuple(dict.fromkeys(int(value) for value in handle_ids))
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT DISTINCT c.ROWID AS cid, c.guid AS guid, "
        "c.display_name AS dn, c.room_name AS rn, c.chat_identifier AS ci "
        "FROM chat c JOIN chat_handle_join chj ON chj.chat_id = c.ROWID "
        f"WHERE chj.handle_id IN ({placeholders}) AND c.chat_identifier LIKE 'chat%'",
        ids,
    ).fetchall()
    # Display content and the logical chat identifier survive a chat.db rebuild.
    rows.sort(
        key=lambda row: (
            str(row["dn"] or ""),
            str(row["rn"] or ""),
            str(row["ci"] or ""),
        )
    )
    return rows


def query_group_members(
    conn: sqlite3.Connection,
    chat_ids: Iterable[int],
) -> sqlite3.Cursor:
    """Fetch Apple handle identifiers belonging to selected group chats."""
    ids = tuple(dict.fromkeys(int(value) for value in chat_ids))
    if not ids:
        return conn.execute("SELECT 1 WHERE 0")
    placeholders = ",".join("?" for _ in ids)
    return conn.execute(
        "SELECT chj.chat_id AS cid, h.id AS handle "
        "FROM chat_handle_join chj JOIN handle h ON h.ROWID = chj.handle_id "
        f"WHERE chj.chat_id IN ({placeholders})",
        ids,
    )


def query_small_group_messages(
    conn: sqlite3.Connection,
    handle_ids: Iterable[int],
    *,
    max_group_size: int,
    limit: int,
) -> list[sqlite3.Row]:
    """Fetch recent bodies from size-capped groups shared with resolved handles."""
    ids = tuple(dict.fromkeys(int(value) for value in handle_ids))
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    sql = f"""
WITH person_groups AS (
    SELECT DISTINCT c.ROWID AS cid, c.display_name AS dn, c.room_name AS rn
    FROM chat c
    JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
    WHERE chj.handle_id IN ({placeholders}) AND c.chat_identifier LIKE 'chat%'
),
sized AS (
    SELECT pg.cid, pg.dn, pg.rn,
           (SELECT COUNT(*) FROM chat_handle_join x WHERE x.chat_id = pg.cid) AS n
    FROM person_groups pg
)
SELECT m.text AS text, m.attributedBody AS attributed_body, m.date AS date,
       m.is_from_me AS is_from_me, s.dn AS dn, s.rn AS rn,
       m.guid AS guid, NULL AS handle
FROM sized s
JOIN chat_message_join cmj ON cmj.chat_id = s.cid
JOIN message m ON m.ROWID = cmj.message_id
WHERE s.n <= ? AND {not_reaction_predicate("m")}
"""
    rows = conn.execute(sql, (*ids, int(max_group_size))).fetchall()
    # Date plus emitted group content is stable; physical ROWID is not.
    rows.sort(
        key=lambda row: (
            int(row["date"] or 0),
            (
                int(row["is_from_me"] or 0),
                str(row["dn"] or ""),
                str(row["rn"] or ""),
                message_text(row),
            ),
        ),
        reverse=True,
    )
    return rows[:limit]
