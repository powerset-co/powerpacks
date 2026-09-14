"""History-depth selection and measurement queries over ``wacli.db``."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from packs.ingestion.primitives.discover.messages.wacli import store_db
from packs.ingestion.primitives.discover.messages.wacli.payloads import HistoryDepthTarget
from packs.ingestion.primitives.discover.messages.wacli.util import history_chat_ref

DEFAULT_HISTORY_DEPTH_MAX_COUNT = int(os.environ.get("POWERPACKS_WACLI_DEPTH_MAX_COUNT", "20"))


def history_depth_visible_predicates(conn: sqlite3.Connection, alias: str = "m") -> list[str]:
    columns = store_db.table_columns(conn, "messages")
    predicates: list[str] = []
    if "revoked" in columns:
        predicates.append(f"COALESCE({alias}.revoked, 0) = 0")
    if "deleted_for_me" in columns:
        predicates.append(f"COALESCE({alias}.deleted_for_me, 0) = 0")
    return predicates


def history_depth_direct_predicates(
    *,
    chat_alias: str = "c",
    message_alias: str = "m",
) -> list[str]:
    return [
        f"COALESCE({chat_alias}.kind, 'unknown') <> 'group'",
        f"{message_alias}.chat_jid NOT LIKE '%@g.us'",
        f"{message_alias}.chat_jid NOT LIKE '%@newsletter'",
        (
            f"({message_alias}.chat_jid LIKE '%@s.whatsapp.net' "
            f"OR {message_alias}.chat_jid LIKE '%@lid')"
        ),
    ]


def history_depth_chat_states(store: Path) -> dict[str, tuple[int, int]]:
    if not (store / "wacli.db").exists():
        return {}
    conn = store_db.open_wacli_db(store)
    try:
        if not store_db.table_exists(conn, "messages") or not store_db.table_exists(conn, "chats"):
            return {}
        where_sql = " AND ".join([
            *history_depth_direct_predicates(),
            *history_depth_visible_predicates(conn),
        ])
        rows = conn.execute(
            f"""
            SELECT m.chat_jid, COUNT(*) AS message_count, MAX(m.ts) AS latest_ts
            FROM messages m
            JOIN chats c ON c.jid = m.chat_jid
            WHERE {where_sql}
            GROUP BY m.chat_jid
            """,
        ).fetchall()
        return {
            str(row["chat_jid"]): (int(row["message_count"]), int(row["latest_ts"] or 0))
            for row in rows
        }
    finally:
        conn.close()


def history_depth_total_count(store: Path) -> int:
    if not (store / "wacli.db").exists():
        return 0
    conn = store_db.open_wacli_db(store)
    try:
        if not store_db.table_exists(conn, "messages"):
            return 0
        row = conn.execute("SELECT COUNT(*) FROM messages").fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def history_depth_targets(
    store: Path,
    *,
    active_since_ts: int,
    max_count: int = DEFAULT_HISTORY_DEPTH_MAX_COUNT,
    before_states: dict[str, tuple[int, int]] | None = None,
    bootstrap: bool = False,
    resume_refs: set[str] | None = None,
    exclude_jids: set[str] | None = None,
) -> list[HistoryDepthTarget]:
    previous = before_states or {}
    resumable = resume_refs or set()
    excluded = exclude_jids or set()
    conn = store_db.open_wacli_db(store)
    try:
        where_sql = " AND ".join([
            *history_depth_direct_predicates(),
            *history_depth_visible_predicates(conn),
        ])
        rows = conn.execute(
            f"""
            SELECT m.chat_jid, c.kind, COUNT(*) AS message_count, MAX(m.ts) AS latest_ts
            FROM messages m
            JOIN chats c ON c.jid = m.chat_jid
            WHERE {where_sql}
            GROUP BY m.chat_jid, c.kind
            HAVING COUNT(*) <= ? AND MAX(m.ts) >= ?
            ORDER BY MAX(m.ts) DESC, m.chat_jid
            """,
            (max_count, active_since_ts),
        ).fetchall()
        targets: list[HistoryDepthTarget] = []
        for row in rows:
            chat_jid = str(row["chat_jid"])
            if chat_jid in excluded:
                continue
            chat_ref = history_chat_ref(chat_jid)
            current_state = (int(row["message_count"]), int(row["latest_ts"] or 0))
            state_changed = before_states is not None and previous.get(chat_jid) != current_state
            if bootstrap or chat_ref in resumable or state_changed:
                targets.append(HistoryDepthTarget(
                    chat_jid=chat_jid,
                    chat_ref=chat_ref,
                    kind=str(row["kind"]),
                    current_count=current_state[0],
                    current_latest_ts=current_state[1],
                    state_changed=state_changed,
                ))
        return targets
    finally:
        conn.close()


def history_depth_counts(store: Path, chat_jid: str) -> tuple[int, int, int]:
    conn = store_db.open_wacli_db(store)
    try:
        where_sql = " AND ".join(["m.chat_jid = ?", *history_depth_visible_predicates(conn)])
        target = conn.execute(
            f"SELECT COUNT(*), MAX(m.ts) FROM messages m WHERE {where_sql}",
            (chat_jid,),
        ).fetchone()
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()
        return int(target[0] or 0), int(total[0] or 0), int(target[1] or 0)
    finally:
        conn.close()
