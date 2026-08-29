"""Scoped WhatsApp body, direct-chat, and group queries over ``wacli.db``.

Only Deep Context and logbook use this body-access surface. Discovery metadata
stays in ``store_db`` and history-depth selection stays in ``depth_db``.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Iterable

from packs.ingestion.primitives.discover.messages.wacli import store_db


def _message_projection(conn: sqlite3.Connection) -> str | None:
    columns = store_db.table_columns(conn, "messages")
    body_columns = {"text", "display_text", "media_caption", "media_type"}
    if "chat_jid" not in columns or not columns.intersection(body_columns):
        return None
    primary_text = "text" if "text" in columns else "display_text"
    aliases = {
        "msg_id": ("msg_id", "NULL"),
        "sender_name": ("sender_name", "NULL"),
        "ts": ("ts" if "ts" in columns else "timestamp", "NULL"),
        "from_me": ("from_me" if "from_me" in columns else "is_from_me", "0"),
        "primary_text": (primary_text, "NULL"),
        **{column: (column, "NULL") for column in body_columns},
    }
    selected = ["rowid AS rid"]
    for alias, (source, default) in aliases.items():
        selected.append(f"{source if source in columns else default} AS {alias}")
    return ", ".join(selected)


def whatsapp_message_text(row: sqlite3.Row, *, include_media: bool = True) -> str:
    """Choose the logbook fallback body, or Deep Context's primary text column.

    Deep Context historically selects one schema-level body column (``text``
    when present, otherwise ``display_text``) and drops rows where that value is
    empty. Logbook deliberately falls through display text, captions, and media
    placeholders. Keep those two established message universes distinct.
    """
    if not include_media:
        return str(row["primary_text"] or "").strip()
    columns = ("text", "display_text", "media_caption")
    for column in columns:
        value = str(row[column] or "").strip()
        if value:
            return value
    media_type = str(row["media_type"] or "").strip() if include_media else ""
    return f"[{media_type}]" if media_type else ""


def _timestamp_key(value: object) -> tuple[int, float, str]:
    if value is None:
        return (0, 0.0, "")
    try:
        return (1, float(value), "")
    except (TypeError, ValueError):
        return (2, 0.0, str(value))


def _message_content_key(row: sqlite3.Row) -> tuple[object, ...]:
    """Return timestamp plus direction, sender, and every available body field."""
    return (
        _timestamp_key(row["ts"]),
        (
            int(row["from_me"] or 0),
            str(row["sender_name"] or ""),
            tuple(
                str(row[column] or "")
                for column in ("primary_text", "text", "display_text", "media_caption", "media_type")
            ),
        ),
    )


def query_whatsapp_messages(
    conn: sqlite3.Connection,
    *,
    phones: Iterable[str] = (),
    chat_jid: str = "",
    since_rowid: int = 0,
    limit: int | None = None,
    newest_first: bool = False,
) -> list[sqlite3.Row]:
    """Stream normalized rows for one person's DMs or one selected group."""
    projection = _message_projection(conn)
    if projection is None:
        return []
    if chat_jid:
        scope, params = "chat_jid = ?", (chat_jid, int(since_rowid))
    else:
        jids = store_db.whatsapp_dm_jids(tuple(phones))
        if not jids:
            return []
        scope = f"chat_jid IN ({','.join('?' for _ in jids)}) AND chat_jid NOT LIKE '%@g.us'"
        params = (*jids, int(since_rowid))
    rows = conn.execute(
        f"SELECT {projection} FROM messages WHERE {scope} AND rowid > ?",
        params,
    ).fetchall()
    # Timestamp + semantic payload survives re-syncs and VACUUM; ROWID does not.
    rows.sort(key=_message_content_key, reverse=newest_first)
    return rows[:limit] if limit is not None else rows


def count_whatsapp_direct_messages(conn: sqlite3.Connection, phones: Iterable[str]) -> int:
    """Count direct-message rows for the supplied phones."""
    if "chat_jid" not in store_db.table_columns(conn, "messages"):
        return 0
    jids = store_db.whatsapp_dm_jids(tuple(phones))
    if not jids:
        return 0
    placeholders = ",".join("?" for _ in jids)
    row = conn.execute(
        f"SELECT COUNT(*) FROM messages WHERE chat_jid IN ({placeholders}) AND chat_jid NOT LIKE '%@g.us'",
        jids,
    ).fetchone()
    return int(row[0] or 0)


def resolve_whatsapp_groups(
    conn: sqlite3.Connection,
    names: Iterable[str],
    phones: list[str] | tuple[str, ...] = (),
) -> list[dict[str, str]]:
    """Resolve named groups plus groups where one supplied phone has spoken."""
    wanted = {re.sub(r"\s+", " ", name.strip().casefold()) for name in names if name.strip()}
    titles: dict[str, str] = {}
    for row in [*store_db.chat_rows(conn), *store_db.group_rows(conn)]:
        jid, title = str(row["jid"] or ""), str(row["name"] or "")
        if jid.endswith("@g.us") and title and title != jid:
            titles[jid] = title
    found = {jid: title for jid, title in titles.items() if re.sub(r"\s+", " ", title.strip().casefold()) in wanted}
    message_columns = store_db.table_columns(conn, "messages")
    sender_jids = store_db.whatsapp_dm_jids(phones)
    if "chat_jid" in message_columns:
        chat_name = "chat_name" if "chat_name" in message_columns else "NULL AS chat_name"
        sender = "sender_jid" if "sender_jid" in message_columns else "NULL AS sender_jid"
        rows = conn.execute(
            f"SELECT DISTINCT chat_jid, {chat_name}, {sender} FROM messages WHERE chat_jid LIKE '%@g.us'",
        )
        for row in rows:
            jid = str(row["chat_jid"] or "")
            title = titles.get(jid) or str(row["chat_name"] or jid)
            named = re.sub(r"\s+", " ", title.strip().casefold()) in wanted
            if jid and (named or row["sender_jid"] in sender_jids):
                found.setdefault(jid, title)
    return [{"jid": jid, "title": title} for jid, title in found.items()]


def existing_whatsapp_direct_jids(conn: sqlite3.Connection, phones: Iterable[str]) -> list[str]:
    """Return candidate direct JIDs that actually exist in the message store."""
    if "chat_jid" not in store_db.table_columns(conn, "messages"):
        return []
    jids = store_db.whatsapp_dm_jids(tuple(phones))
    if not jids:
        return []
    placeholders = ",".join("?" for _ in jids)
    rows = conn.execute(
        f"SELECT DISTINCT chat_jid FROM messages WHERE chat_jid IN ({placeholders})",
        jids,
    )
    return [str(row["chat_jid"]) for row in rows]
