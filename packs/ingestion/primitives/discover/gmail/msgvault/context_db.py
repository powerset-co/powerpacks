"""Indexed msgvault queries used to assemble Deep Context email dossiers."""

from __future__ import annotations

import itertools
import sqlite3
from typing import Any, Iterable, Iterator

PARTICIPANT_IDS_SQL = "SELECT id FROM participants WHERE LOWER(email_address) = ?"

_RECENT_SELECT = """
SELECT COALESCE(m.sent_at, m.received_at, m.internal_date) AS at,
       m.conversation_id, LOWER(sp.email_address) AS sender_email,
       m.subject, m.snippet, mb.body_text
"""
_RECENT_FROM_SENDER_SQL = _RECENT_SELECT + """
FROM messages m
LEFT JOIN participants sp ON sp.id = m.sender_id
LEFT JOIN message_bodies mb ON mb.message_id = m.id
WHERE m.message_type = 'email'
  AND (m.deleted_at IS NULL OR m.deleted_at = '')
  AND (m.deleted_from_source_at IS NULL OR m.deleted_from_source_at = '')
  AND m.sender_id = ?1
-- Content survives store rebuilds; physical row ids do not.
ORDER BY at DESC,
         LOWER(COALESCE(sp.email_address, '')) DESC,
         COALESCE(m.subject, '') DESC,
         COALESCE(m.snippet, '') DESC,
         COALESCE(mb.body_text, '') DESC
LIMIT ?2
"""
_RECENT_TO_RECIPIENT_SQL = _RECENT_SELECT + """
FROM message_recipients mr
JOIN messages m ON m.id = mr.message_id
LEFT JOIN participants sp ON sp.id = m.sender_id
LEFT JOIN message_bodies mb ON mb.message_id = m.id
WHERE m.message_type = 'email'
  AND (m.deleted_at IS NULL OR m.deleted_at = '')
  AND (m.deleted_from_source_at IS NULL OR m.deleted_from_source_at = '')
  AND mr.participant_id = ?1
-- Content survives store rebuilds; physical row ids do not.
ORDER BY at DESC,
         LOWER(COALESCE(sp.email_address, '')) DESC,
         COALESCE(m.subject, '') DESC,
         COALESCE(m.snippet, '') DESC,
         COALESCE(mb.body_text, '') DESC
LIMIT ?2
"""

WINDOWED_CONTEXT_SQL = """
WITH assoc AS (
    SELECT cp.cemail, m.id AS mid,
           COALESCE(m.sent_at, m.received_at, m.internal_date) AS at,
           m.conversation_id, LOWER(sp.email_address) AS sender_email,
           m.subject, m.snippet, mb.body_text
    FROM cand_pid cp JOIN messages m ON m.sender_id = cp.pid
    LEFT JOIN participants sp ON sp.id = m.sender_id
    LEFT JOIN message_bodies mb ON mb.message_id = m.id
    WHERE m.message_type = 'email'
      AND (m.deleted_at IS NULL OR m.deleted_at = '')
      AND (m.deleted_from_source_at IS NULL OR m.deleted_from_source_at = '')
    UNION
    SELECT cp.cemail, m.id,
           COALESCE(m.sent_at, m.received_at, m.internal_date),
           m.conversation_id, LOWER(sp.email_address),
           m.subject, m.snippet, mb.body_text
    FROM cand_pid cp
    JOIN message_recipients mr ON mr.participant_id = cp.pid
    JOIN messages m ON m.id = mr.message_id
    LEFT JOIN participants sp ON sp.id = m.sender_id
    LEFT JOIN message_bodies mb ON mb.message_id = m.id
    WHERE m.message_type = 'email'
      AND (m.deleted_at IS NULL OR m.deleted_at = '')
      AND (m.deleted_from_source_at IS NULL OR m.deleted_from_source_at = '')
), ranked AS (
    SELECT assoc.*,
           -- Full projected content makes timestamp ties independent of row ids.
           ROW_NUMBER() OVER (
               PARTITION BY cemail
               ORDER BY at DESC,
                        COALESCE(sender_email, '') DESC,
                        COALESCE(subject, '') DESC,
                        COALESCE(snippet, '') DESC,
                        COALESCE(body_text, '') DESC
           ) AS rn
    FROM assoc
)
SELECT r.cemail, r.at, r.conversation_id,
       r.sender_email, r.subject, r.snippet, r.body_text
FROM ranked r
WHERE r.rn <= ?
-- Match the ranking order so streamed groups are total without physical ids.
ORDER BY r.cemail, r.at DESC,
         COALESCE(r.sender_email, '') DESC,
         COALESCE(r.subject, '') DESC,
         COALESCE(r.snippet, '') DESC,
         COALESCE(r.body_text, '') DESC
"""


def _recent_content_key(row: sqlite3.Row) -> tuple[str, str, str, str, str]:
    """Order equal-time rows by projected content, which survives store rebuilds."""
    return (
        str(row["at"] or ""),
        str(row["sender_email"] or ""),
        str(row["subject"] or ""),
        str(row["snippet"] or ""),
        str(row["body_text"] or ""),
    )


def fetch_recent_rows(
    con: sqlite3.Connection,
    email: str,
    fetch_limit: int,
) -> list[sqlite3.Row]:
    ids = [
        row["id"]
        for row in con.execute(PARTICIPANT_IDS_SQL, (email.lower(),)).fetchall()
    ]
    if not ids:
        return []
    rows: list[sqlite3.Row] = []
    for pid in ids:
        rows.extend(con.execute(_RECENT_FROM_SENDER_SQL, (pid, fetch_limit)).fetchall())
        rows.extend(con.execute(_RECENT_TO_RECIPIENT_SQL, (pid, fetch_limit)).fetchall())
    # Content, unlike rowid, remains stable when msgvault is rebuilt or vacuumed.
    rows.sort(key=_recent_content_key, reverse=True)
    return rows[:fetch_limit]


def create_candidate_pid_table(con: sqlite3.Connection, emails: Iterable[str]) -> int:
    con.execute("DROP TABLE IF EXISTS cand_pid")
    con.execute("DROP TABLE IF EXISTS cand_email")
    con.execute("CREATE TEMP TABLE cand_email(email TEXT PRIMARY KEY)")
    con.executemany(
        "INSERT OR IGNORE INTO cand_email(email) VALUES (?)",
        [(email.strip().lower(),) for email in emails if email and email.strip()],
    )
    con.execute("""
        CREATE TEMP TABLE cand_pid AS
        SELECT ce.email AS cemail, p.id AS pid
        FROM cand_email ce
        JOIN participants p ON LOWER(p.email_address) = ce.email
    """)
    con.execute("CREATE INDEX cand_pid_pid ON cand_pid(pid)")
    return int(con.execute("SELECT COUNT(*) AS n FROM cand_pid").fetchone()["n"])


def stream_contact_groups(
    con: sqlite3.Connection,
    fetch_limit: int,
) -> Iterator[tuple[str, list[sqlite3.Row]]]:
    cursor = con.execute(WINDOWED_CONTEXT_SQL, (fetch_limit,))
    for email, group in itertools.groupby(cursor, key=lambda row: row["cemail"]):
        yield email, list(group)


def account_emails(con: sqlite3.Connection) -> set[str]:
    rows = con.execute("SELECT LOWER(identifier) AS ident FROM sources").fetchall()
    return {
        str(row["ident"]).strip()
        for row in rows
        if str(row["ident"] or "").strip()
    }


def owner_identity(con: sqlite3.Connection) -> dict[str, Any]:
    emails = sorted(account_emails(con))
    name = ""
    if emails:
        placeholders = ",".join("?" for _ in emails)
        row = con.execute(
            f"SELECT TRIM(display_name) AS dn, COUNT(*) AS n FROM participants "
            f"WHERE LOWER(email_address) IN ({placeholders}) "
            "AND TRIM(COALESCE(display_name, '')) <> '' "
            "GROUP BY TRIM(display_name) ORDER BY n DESC, dn LIMIT 1",
            emails,
        ).fetchone()
        if row:
            name = str(row["dn"]).strip()
    return {"name": name, "emails": emails}


def count_messages_for(
    con: sqlite3.Connection,
    email: str,
    accounts: set[str],
) -> int:
    contact_ids = [
        row["id"]
        for row in con.execute(PARTICIPANT_IDS_SQL, (email.lower(),)).fetchall()
    ]
    if not contact_ids:
        return 0
    owner_ids: list[Any] = []
    if accounts:
        placeholders = ",".join("?" for _ in accounts)
        owner_ids = [
            row["id"]
            for row in con.execute(
                f"SELECT id FROM participants "
                f"WHERE LOWER(email_address) IN ({placeholders})",
                tuple(sorted(account.lower() for account in accounts)),
            ).fetchall()
        ]
    contact_slots = ",".join("?" for _ in contact_ids)
    not_deleted = (
        "AND (m.deleted_at IS NULL OR m.deleted_at = '') "
        "AND (m.deleted_from_source_at IS NULL OR m.deleted_from_source_at = '')"
    )
    arms = [
        f"SELECT m.id FROM messages m WHERE m.message_type='email' {not_deleted} "
        f"AND m.sender_id IN ({contact_slots})"
    ]
    params: list[Any] = list(contact_ids)
    if owner_ids:
        owner_slots = ",".join("?" for _ in owner_ids)
        arms.append(
            "SELECT m.id FROM message_recipients mr "
            "JOIN messages m ON m.id = mr.message_id "
            f"WHERE m.message_type='email' {not_deleted} "
            f"AND mr.participant_id IN ({contact_slots}) "
            f"AND m.sender_id IN ({owner_slots})"
        )
        params += list(contact_ids) + list(owner_ids)
    sql = f"SELECT COUNT(*) AS n FROM ({' UNION '.join(arms)})"
    return int(con.execute(sql, params).fetchone()["n"])


def thread_participant_rosters(
    con: sqlite3.Connection,
    emails: Iterable[str],
    max_threads: int,
) -> list[dict[str, Any]]:
    normalized = [email.lower() for email in emails if email]
    if not normalized:
        return []
    placeholders = ",".join("?" for _ in normalized)
    participant_ids = [
        row[0]
        for row in con.execute(
            f"SELECT id FROM participants "
            f"WHERE LOWER(email_address) IN ({placeholders})",
            normalized,
        )
    ]
    if not participant_ids:
        return []
    pid_slots = ",".join("?" for _ in participant_ids)
    conversations = con.execute(
        f"""SELECT conversation_id, MAX(at) AS at, MAX(subject) AS subject FROM (
            SELECT m.conversation_id, COALESCE(m.sent_at, m.received_at, m.internal_date) AS at,
                   m.subject
            FROM messages m WHERE m.message_type='email' AND m.conversation_id IS NOT NULL
              AND m.sender_id IN ({pid_slots})
            UNION ALL
            SELECT m.conversation_id, COALESCE(m.sent_at, m.received_at, m.internal_date), m.subject
            FROM message_recipients mr JOIN messages m ON m.id = mr.message_id
            WHERE m.message_type='email' AND m.conversation_id IS NOT NULL
              AND mr.participant_id IN ({pid_slots})
        ) GROUP BY conversation_id ORDER BY at DESC LIMIT ?""",
        (*participant_ids, *participant_ids, max_threads),
    )
    threads: list[dict[str, Any]] = []
    for conversation_id, _at, subject in conversations:
        recipients = con.execute(
            """SELECT DISTINCT LOWER(p.email_address) AS email,
                      COALESCE(NULLIF(p.display_name, ''), NULLIF(mr.display_name, ''), '') AS name
               FROM messages m
               JOIN message_recipients mr ON mr.message_id = m.id
               JOIN participants p ON p.id = mr.participant_id
               WHERE m.conversation_id = ?""",
            (conversation_id,),
        )
        roster, seen = [], set()
        for email, name in recipients:
            if email and email not in seen:
                seen.add(email)
                roster.append(f"{name} <{email}>" if name else email)
        if roster:
            threads.append({
                "subject": (subject or "(no subject)")[:120],
                "participants": roster,
            })
    return threads
