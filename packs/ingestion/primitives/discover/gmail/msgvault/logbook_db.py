"""Thread, body, and participant queries used by the local logbook export."""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from packs.ingestion.primitives.discover.gmail.msgvault import context_db

_NOT_DELETED = (
    "m.message_type = 'email' "
    "AND (m.deleted_at IS NULL OR m.deleted_at = '') "
    "AND (m.deleted_from_source_at IS NULL OR m.deleted_from_source_at = '')"
)
_CONVERSATIONS_SQL = f"""
CREATE TEMP TABLE lb_convs AS
SELECT DISTINCT m.conversation_id AS cid
FROM cand_pid cp JOIN messages m ON m.sender_id = cp.pid
WHERE {_NOT_DELETED} AND m.conversation_id IS NOT NULL
UNION
SELECT DISTINCT m.conversation_id
FROM cand_pid cp
JOIN message_recipients mr ON mr.participant_id = cp.pid
JOIN messages m ON m.id = mr.message_id
WHERE {_NOT_DELETED} AND m.conversation_id IS NOT NULL
"""
_THREADS_SQL = f"""
SELECT m.conversation_id AS cid,
       COALESCE(m.sent_at, m.received_at, m.internal_date) AS at,
       m.id AS mid, m.source_message_id AS src_id, m.is_from_me AS is_from_me,
       m.subject AS subject,
       LOWER(sp.email_address) AS sender_email, sp.display_name AS sender_name,
       c.source_conversation_id AS thread_id, c.title AS title
FROM lb_convs
JOIN messages m ON m.conversation_id = lb_convs.cid
LEFT JOIN participants sp ON sp.id = m.sender_id
LEFT JOIN conversations c ON c.id = m.conversation_id
WHERE {_NOT_DELETED} AND m.id > ?
ORDER BY m.conversation_id, at, m.id
"""


def prepare_conversations(con: sqlite3.Connection, emails: Iterable[str]) -> int:
    """Create the temporary set of conversations involving selected contacts."""
    if not context_db.create_candidate_pid_table(con, emails):
        return 0
    con.execute("DROP TABLE IF EXISTS lb_convs")
    con.execute(_CONVERSATIONS_SQL)
    con.execute("CREATE INDEX lb_convs_cid ON lb_convs(cid)")
    return int(con.execute("SELECT COUNT(*) FROM lb_convs").fetchone()[0])


def stream_thread_rows(con: sqlite3.Connection, since_id: int = 0) -> sqlite3.Cursor:
    """Stream lightweight message rows in conversation and time order."""
    return con.execute(_THREADS_SQL, (int(since_id),))


def body_parts(
    con: sqlite3.Connection,
    message_id: int,
    raw_head_cap: int,
) -> dict[str, Any]:
    """Fetch one raw MIME prefix plus extracted-body fallbacks."""
    raw = con.execute(
        "SELECT substr(raw_data, 1, ?) AS head, compression "
        "FROM message_raw WHERE message_id = ?",
        (int(raw_head_cap), int(message_id)),
    ).fetchone()
    fallback = con.execute(
        "SELECT body_text, body_html FROM message_bodies WHERE message_id = ?",
        (int(message_id),),
    ).fetchone()
    return {
        "head": raw["head"] if raw else None,
        "compression": raw["compression"] if raw else None,
        "body_text": fallback["body_text"] if fallback else None,
        "body_html": fallback["body_html"] if fallback else None,
    }


def count_messages(con: sqlite3.Connection) -> int:
    """Count messages in the prepared conversation set."""
    row = con.execute(
        "SELECT COUNT(*) FROM lb_convs JOIN messages m "
        f"ON m.conversation_id = lb_convs.cid WHERE {_NOT_DELETED}",
    ).fetchone()
    return int(row[0] or 0)


def participant_phone_names(con: sqlite3.Connection) -> list[dict[str, str]]:
    """Return non-empty phone/display-name pairs when the schema provides them."""
    try:
        rows = con.execute(
            "SELECT phone_number, display_name FROM participants "
            "WHERE phone_number IS NOT NULL AND phone_number != '' "
            "AND display_name IS NOT NULL AND display_name != ''",
        )
        return [
            {
                "phone_number": str(row["phone_number"]),
                "display_name": str(row["display_name"]),
            }
            for row in rows
        ]
    except sqlite3.Error:
        return []
