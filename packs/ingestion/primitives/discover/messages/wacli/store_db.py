"""Read-only SQLite access to the local wacli store (`<store>/wacli.db`).

wacli owns this database; Powerpacks only reads it, and only ever reads
METADATA. `assert_metadata_query` enforces that at the query level: the
extractor's ROW reads all go through `select_rows`, which refuses any SQL
naming a body column (`text`, `display_text`, `media_caption`, attachment
paths/keys, …); the depth aggregates below select only counts and identifiers.
The privacy contract is a code path here, not a convention.

Two groups of readers live here:

- generic access the extractor uses — open the read-only connection, probe
  tables/columns, run a guarded SELECT, list group chat JIDs;
- the history-depth queries — visible/direct-message predicates plus the four
  aggregates the depth stage runs on (`chat_states`, `total_count`, `targets`,
  `counts`). They sit beside the generic helpers because every one of them is a
  `wacli.db` read; the stage that interprets them is `depth.py`.

Changelog:
  2026-07-30 (wacli split): extracted from the single-file `whatsapp_wacli.py`.
    All wacli.db SQL now lives in this one module (it used to be interleaved
    with the binary lifecycle and the stage loop). Queries unchanged.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[6]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.discover.messages.wacli.payloads import HistoryDepthTarget  # noqa: E402
from packs.ingestion.primitives.discover.messages.wacli.runtime import PrimitiveFailed  # noqa: E402
from packs.ingestion.primitives.discover.messages.wacli.util import history_chat_ref  # noqa: E402

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
# A chat at or below this message count is "shallow" — the depth stage's
# selection threshold, applied in the target query's HAVING clause.
DEFAULT_HISTORY_DEPTH_MAX_COUNT = int(os.environ.get("POWERPACKS_WACLI_DEPTH_MAX_COUNT", "20"))


def open_wacli_db(store: Path) -> sqlite3.Connection:
    db_path = store / "wacli.db"
    if not db_path.exists():
        raise PrimitiveFailed(f"wacli database not found at {db_path}")
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


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


def group_chat_jids(store: Path) -> list[str]:
    db_path = store / "wacli.db"
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if not table_exists(conn, "chats"):
            return []
        rows = select_rows(conn, "SELECT jid FROM chats WHERE kind = 'group' OR jid LIKE '%@g.us' ORDER BY jid")
        return [str(row["jid"]) for row in rows if row["jid"]]
    finally:
        conn.close()


def history_depth_visible_predicates(conn: sqlite3.Connection, alias: str = "m") -> list[str]:
    columns = table_columns(conn, "messages")
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
    conn = open_wacli_db(store)
    try:
        if not table_exists(conn, "messages") or not table_exists(conn, "chats"):
            return {}
        visibility = history_depth_visible_predicates(conn)
        where_sql = " AND ".join([
            *history_depth_direct_predicates(),
            *visibility,
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
            str(row["chat_jid"]): (
                int(row["message_count"]),
                int(row["latest_ts"] or 0),
            )
            for row in rows
        }
    finally:
        conn.close()


def history_depth_total_count(store: Path) -> int:
    if not (store / "wacli.db").exists():
        return 0
    conn = open_wacli_db(store)
    try:
        if not table_exists(conn, "messages"):
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
    conn = open_wacli_db(store)
    try:
        visibility = history_depth_visible_predicates(conn)
        where_sql = " AND ".join([
            *history_depth_direct_predicates(),
            *visibility,
        ])
        rows = conn.execute(
            f"""
            SELECT
                m.chat_jid,
                c.kind,
                COUNT(*) AS message_count,
                MAX(m.ts) AS latest_ts
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
            current_state = (
                int(row["message_count"]),
                int(row["latest_ts"] or 0),
            )
            state_changed = (
                before_states is not None
                and previous.get(chat_jid) != current_state
            )
            if (
                bootstrap
                or chat_ref in resumable
                or state_changed
            ):
                targets.append(
                    HistoryDepthTarget(
                        chat_jid=chat_jid,
                        chat_ref=chat_ref,
                        kind=str(row["kind"]),
                        current_count=current_state[0],
                        current_latest_ts=current_state[1],
                        state_changed=state_changed,
                    )
                )
        return targets
    finally:
        conn.close()


def history_depth_counts(store: Path, chat_jid: str) -> tuple[int, int, int]:
    conn = open_wacli_db(store)
    try:
        visibility = history_depth_visible_predicates(conn)
        where_sql = " AND ".join(["m.chat_jid = ?", *visibility])
        target = conn.execute(
            f"SELECT COUNT(*), MAX(m.ts) FROM messages m WHERE {where_sql}",
            (chat_jid,),
        ).fetchone()
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()
        return (
            int(target[0] or 0),
            int(total[0] or 0),
            int(target[1] or 0),
        )
    finally:
        conn.close()
