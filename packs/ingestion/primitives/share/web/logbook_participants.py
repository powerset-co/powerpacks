"""Read a saved Gmail thread's senders and recipients from the local mail store."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from packs.ingestion.primitives.discover.gmail.msgvault.util import msgvault_db_uri


@dataclass(frozen=True)
class Participant:
    name: str
    email: str
    roles: tuple[str, ...]


def email_participants(store: Path, thread_id: str) -> tuple[Participant, ...] | None:
    """None means local metadata is unavailable; the saved conversation remains readable."""
    if not store.is_file():
        return None
    try:
        with closing(sqlite3.connect(msgvault_db_uri(store), uri=True)) as con:
            rows = con.execute("""
                WITH thread AS (
                    SELECT id FROM conversations WHERE source_conversation_id = ?
                    UNION SELECT id FROM conversations WHERE 'conv-' || id = ?
                ), members AS (
                    SELECT m.sender_id AS participant_id, 'from' AS role
                    FROM thread JOIN messages m ON m.conversation_id = thread.id
                    UNION
                    SELECT r.participant_id, r.recipient_type
                    FROM thread JOIN messages m ON m.conversation_id = thread.id
                    JOIN message_recipients r ON r.message_id = m.id
                )
                SELECT p.display_name, lower(p.email_address), members.role
                FROM members JOIN participants p ON p.id = members.participant_id
                ORDER BY lower(p.email_address), members.role
            """, (thread_id, thread_id)).fetchall()
    except sqlite3.Error:
        return None
    people: dict[str, tuple[str, set[str]]] = {}
    for name, email, role in rows:
        if not email:
            continue
        person = people.setdefault(email, (name or "", set()))
        person[1].add(role)
    return tuple(Participant(name, email, tuple(sorted(roles))) for email, (name, roles) in people.items())
