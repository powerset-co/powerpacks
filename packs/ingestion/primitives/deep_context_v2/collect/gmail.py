"""Gmail messages for one candidate from the msgvault archive, opened read-only.

Rows are fetched by participation (sender or recipient) for each of the candidate's emails;
only the candidate's own words and the owner's own words are kept. Bodies are cut at quoted
history and windowed to head and tail; messages are ranked by signal, breadth across threads
first, and near-duplicates dropped.

Created: 2026-10-06
"""
from __future__ import annotations

import html
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from packs.ingestion.primitives.deep_context_v2.text_similarity import jaccard, shingles

SNIPPET_CHARS = 200
HEAD_CHARS = 300
TAIL_CHARS = 300
# Three fetched rows per kept message leaves room for per-thread ranking and dedup.
CANDIDATE_ROWS_PER_OUTPUT = 3
NEARDUP_THRESHOLD = 0.6
QUOTE_CUT = re.compile(
    r"(?im)^\s*(on .{0,120}wrote:|-+\s*original message\s*-+|-+\s*forwarded message\s*-+|"
    r"from:\s.+|sent from my .+|get outlook for .+)\s*$"
)
SIGNAL_FEATURES = (
    (re.compile(r"\+?\d[\d().\-  ]{7,}\d"), 3),
    (
        re.compile(
            r"https?://|www\.|linkedin\.com/in/|github\.com/|"
            r"[a-z0-9-]+\.(?:com|io|co|org|net)\b",
            re.I,
        ),
        2,
    ),
    (re.compile(r"(?<![\w.])@[A-Za-z0-9_]{2,}"), 1),
    (
        re.compile(
            r"\b(?:co-?founder|founder|ceo|cto|coo|cfo|vp|head of|director|"
            r"principal|engineer|developer|manager|realtor|broker|partner|associate|"
            r"analyst|consultant|professor|lecturer|recruiter|designer|attorney|"
            r"architect|scientist)\b",
            re.I,
        ),
        2,
    ),
    (re.compile(r"\b(?:DRE|CalBRE|NMLS|License|Lic\.?)\s*#?\s*\d", re.I), 3),
    (
        re.compile(
            r"\b(?:at|@)\s+[A-Z][A-Za-z0-9&.\-]+"
            r"(?:\s+[A-Z][A-Za-z0-9&.\-]+)*"
        ),
        1,
    ),
)

PARTICIPANT_IDS_SQL = "SELECT id FROM participants WHERE LOWER(email_address) = ?"
_RECENT_SELECT = """
SELECT COALESCE(m.sent_at, m.received_at, m.internal_date) AS at,
       m.conversation_id, LOWER(sp.email_address) AS sender_email,
       m.subject, m.snippet, mb.body_text
"""
_RECENT_SQL = _RECENT_SELECT + """
FROM messages m
LEFT JOIN participants sp ON sp.id = m.sender_id
LEFT JOIN message_bodies mb ON mb.message_id = m.id
WHERE m.message_type = 'email'
  AND (m.deleted_at IS NULL OR m.deleted_at = '')
  AND (m.deleted_from_source_at IS NULL OR m.deleted_from_source_at = '')
  -- Each email once: msgvault also files the sender as a 'from' recipient, and one person can be
  -- both To and Cc.
  AND m.id IN (
      SELECT id FROM messages WHERE sender_id IN (SELECT id FROM participants WHERE LOWER(email_address) = ?)
      UNION
      SELECT message_id FROM message_recipients
      WHERE participant_id IN (SELECT id FROM participants WHERE LOWER(email_address) = ?)
  )
-- Content survives store rebuilds; physical row ids do not.
ORDER BY at DESC,
         LOWER(COALESCE(sp.email_address, '')) DESC,
         COALESCE(m.subject, '') DESC,
         COALESCE(m.snippet, '') DESC,
         COALESCE(mb.body_text, '') DESC
LIMIT ?
"""


@dataclass(frozen=True)
class EmailMessage:
    at: str  # "" when the source row had no timestamp
    sender: str
    from_role: str  # "contact" | "me"
    subject: str
    snippet: str  # the cleaned body; Gmail's preview line only when there is no body


@dataclass(frozen=True)
class EmailRankedMessage:
    rank: tuple[int, int, str]  # signal score, 1 if the candidate wrote it, timestamp
    message: EmailMessage


def open_msgvault(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.expanduser().resolve()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def account_emails(con: sqlite3.Connection) -> set[str]:
    """The owner's own addresses: every synced msgvault source."""
    accounts = set()
    for row in con.execute("SELECT LOWER(identifier) AS ident FROM sources").fetchall():
        if str(row["ident"] or "").strip():
            accounts.add(str(row["ident"]).strip())
    return accounts


def _participant_ids(con: sqlite3.Connection, email: str) -> list[int]:
    ids = []
    for row in con.execute(PARTICIPANT_IDS_SQL, (email.lower(),)).fetchall():
        ids.append(row["id"])
    return ids



def fetch_recent_rows(con: sqlite3.Connection, email: str, fetch_limit: int) -> list[sqlite3.Row]:
    """The newest `fetch_limit` emails the candidate sent or received, each once."""
    return con.execute(_RECENT_SQL, (email.lower(), email.lower(), fetch_limit)).fetchall()


def count_messages_for(con: sqlite3.Connection, email: str, accounts: set[str]) -> int:
    """Uncapped count: mail the candidate sent, plus mail the owner sent to the candidate."""
    contact_ids = _participant_ids(con, email)
    if not contact_ids:
        return 0
    owner_ids: list[Any] = []
    if accounts:
        lowered = []
        for account in accounts:
            lowered.append(account.lower())
        lowered.sort()
        placeholders = ",".join("?" for _ in lowered)
        for row in con.execute(
            f"SELECT id FROM participants WHERE LOWER(email_address) IN ({placeholders})", lowered
        ).fetchall():
            owner_ids.append(row["id"])
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
        params.extend(contact_ids)
        params.extend(owner_ids)
    sql = f"SELECT COUNT(*) AS n FROM ({' UNION '.join(arms)})"
    return int(con.execute(sql, params).fetchone()["n"])


def thread_participant_rosters(con: sqlite3.Connection, emails: Sequence[str], max_threads: int) -> list[dict[str, Any]]:
    """Who was on the candidate's most recent threads: subject plus `name <email>` per recipient."""
    normalized = []
    for email in emails:
        normalized.append(email.lower())
    placeholders = ",".join("?" for _ in normalized)
    participant_ids = []
    for row in con.execute(f"SELECT id FROM participants WHERE LOWER(email_address) IN ({placeholders})", normalized):
        participant_ids.append(row[0])
    if not participant_ids:
        return []
    pid_slots = ",".join("?" for _ in participant_ids)
    params = list(participant_ids)
    params.extend(participant_ids)
    params.append(max_threads)
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
        params,
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
        roster = []
        seen = set()
        for email, name in recipients:
            if email and email not in seen:
                seen.add(email)
                if name:
                    roster.append(f"{name} <{email}>")
                else:
                    roster.append(email)
        if roster:
            threads.append({"subject": (subject or "(no subject)")[:120], "participants": roster})
    return threads


def clean_text(value: object, limit: int | None = None) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if limit is not None and 0 < limit < len(text):
        text = text[:limit]
    return text


def clean_body(value: object) -> str:
    """Keep the new message and its signature; drop quoted history; window to head and tail."""
    text = html.unescape(str(value or ""))
    cut = QUOTE_CUT.search(text)
    if cut:
        text = text[: cut.start()]
    lines = []
    for line in text.splitlines():
        if not line.lstrip().startswith(">"):
            lines.append(line)
    text = re.sub(r"\s+", " ", " ".join(lines)).strip()
    if not text:
        return ""
    if len(text) <= HEAD_CHARS + TAIL_CHARS:
        return text
    return f"{text[:HEAD_CHARS].strip()} … {text[-TAIL_CHARS:].strip()}"


def signal_score(text: str) -> int:
    """Signature and bio features, plus a small length bonus."""
    text = text or ""
    score = 0
    for pattern, weight in SIGNAL_FEATURES:
        if pattern.search(text):
            score += weight
    return score + min(len(text) // 200, 3)


def _ranked_order_key(ranked: EmailRankedMessage) -> tuple[int, int, str, str, str, str]:
    """Signal, role, time, then content to break ties."""
    message = ranked.message
    signal, role, at = ranked.rank
    return (signal, role, at, message.sender, message.subject, message.snippet)


def select_emails(rows: list[sqlite3.Row], email: str, per_person: int, accounts: set[str]) -> list[EmailMessage]:
    """The candidate's and the owner's own mail, thread leaders first, then the rest, near-duplicates dropped."""
    by_thread: dict[tuple[str, object], list[EmailRankedMessage]] = {}
    for index, row in enumerate(rows):
        sender = str(row["sender_email"] or "").strip()
        if sender == email:
            from_role = "contact"
        elif sender and sender in accounts:
            from_role = "me"
        else:
            continue  # a third party's message on a shared thread
        if row["body_text"]:
            text = clean_body(row["body_text"])
        else:
            text = clean_text(row["snippet"], SNIPPET_CHARS)
        if not text:
            continue
        at = str(row["at"] or "").strip()
        message = EmailMessage(at=at, sender=sender, from_role=from_role, subject=clean_text(row["subject"]), snippet=text)
        if from_role == "contact":
            role_rank = 1
        else:
            role_rank = 0
        conversation_id = row["conversation_id"]
        # A message without a thread is its own bucket; `index` only makes the key unique.
        if conversation_id in (None, "", "None"):
            key = ("msg", index)
        else:
            key = ("thread", conversation_id)
        by_thread.setdefault(key, []).append(EmailRankedMessage((signal_score(text), role_rank, at), message))
    leaders = []
    rest = []
    for messages in by_thread.values():
        messages.sort(key=_ranked_order_key, reverse=True)
        leaders.append(messages[0])
        rest.extend(messages[1:])
    leaders.sort(key=_ranked_order_key, reverse=True)
    rest.sort(key=_ranked_order_key, reverse=True)
    kept: list[EmailMessage] = []
    kept_shingles: list[frozenset[str]] = []
    for ranked in leaders + rest:
        if len(kept) >= per_person:
            break
        message_shingles = shingles(ranked.message.snippet)
        duplicate = False
        for prior in kept_shingles:
            if jaccard(message_shingles, prior) >= NEARDUP_THRESHOLD:
                duplicate = True
                break
        if duplicate:
            continue
        kept.append(ranked.message)
        kept_shingles.append(message_shingles)
    return kept


def recent_emails_for(con: sqlite3.Connection, email: str, per_person: int, accounts: set[str]) -> list[EmailMessage]:
    rows = fetch_recent_rows(con, email, per_person * CANDIDATE_ROWS_PER_OUTPUT)
    return select_emails(rows, email, per_person, accounts)
