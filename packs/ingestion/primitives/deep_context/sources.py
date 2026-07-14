"""Per-source message-body readers for the deep-context collector.

Each reader takes a ``Person`` and returns a list of normalized message dicts:

    {"channel": "gmail"|"imessage"|"whatsapp",
     "at": "<iso8601>",
     "direction": "from_them"|"from_me",
     "subject": "<str, gmail only>",
     "text": "<cleaned body>"}

Design goals: **stream + bound**. Every query uses a per-person ``LIMIT`` so only
one person's recent window is ever materialized — RSS stays flat regardless of
archive size. Gmail reuses ``build_email_context`` wholesale (thread dedup,
signature-aware body cleaning, signal ranking). iMessage and WhatsApp read DM
bodies by default. A separate opt-in reader can include small iMessage groups;
WhatsApp groups remain excluded. The iMessage readers decode Apple's
``attributedBody`` blob when the plain ``text`` column is empty (newer macOS).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.common import Person, phone_digits

# Reuse the Gmail email-context machinery (msgvault connect/schema + the
# signature-aware body selection) exactly as the marker flow does.
_PRIMITIVES_DIR = Path(__file__).resolve().parent.parent
for _sub in ("build_email_context", "gmail_network_import"):
    _p = str(_PRIMITIVES_DIR / _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import build_email_context as bec  # noqa: E402
import gmail_network_import as gni  # noqa: E402, F401 - re-exported for collector defaults

# Every channel is its own vertical with the same deep cap: Gmail, iMessage, and
# WhatsApp each pool up to CHAT_MESSAGE_CAP recent messages, and the incremental
# synthesizer decides how far back to actually grok. Gmail used to collapse to one
# message per thread (~20 threads), which starved thin contacts with a single rich
# thread — now it keeps the back-and-forth like the chat channels. `count_*` report
# the TRUE total so capping is honest (not hidden behind the LIMIT).
CHAT_MESSAGE_CAP = 1600
DEFAULT_WACLI_DB = Path(".powerpacks/messages/wacli/wacli.db")


# --- Gmail (msgvault) -------------------------------------------------------

def read_gmail(person: Person, con: sqlite3.Connection, accounts: set[str],
               cap: int = CHAT_MESSAGE_CAP) -> list[dict[str, Any]]:
    """Recent, signature-aware email bodies for the person — the whole back-and-forth.

    Queries each of the person's emails through ``build_email_context`` and merges the
    selected entries (the contact's own + owner-directed messages). ``max_per_thread=None``
    keeps every message in a thread (not just the signal-densest one), so a single rich
    thread is no longer reduced to one line; the per-person ``cap`` bounds the total."""
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for email in person.emails:
        try:
            entries, _ = bec.recent_emails_for(
                con, email, cap, bec.DEFAULT_SNIPPET_CHARS, accounts,
                source="body", max_per_thread=None,
            )
        except sqlite3.Error:
            continue
        for entry in entries:
            text = (entry.get("snippet") or "").strip()
            if not text:
                continue
            key = ((entry.get("subject") or "").lower(), text[:80].lower())
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "channel": "gmail",
                "at": entry.get("at") or "",
                "direction": "from_me" if entry.get("from_role") == "me" else "from_them",
                "subject": entry.get("subject") or "",
                "text": text,
            })
    return out


def count_gmail(person: Person, con: sqlite3.Connection, accounts: set[str]) -> int:
    """True total of the person's poolable Gmail messages (so capping is honest), mirroring
    ``count_imessage_dms``. Counts the same universe ``read_gmail`` draws from — the contact's
    own + owner-directed messages — across all of the person's email addresses."""
    total = 0
    for email in person.emails:
        try:
            total += bec.count_messages_for(con, email, accounts)
        except sqlite3.Error:
            continue
    return total


def gmail_thread_participants(person: Person, con: sqlite3.Connection, max_threads: int = 25) -> list[dict[str, Any]]:
    """Per-thread participant rosters (full from/to/cc as ``Name <email>``) for the person's email
    threads. Surfaces co-recipients we'd otherwise drop — shared colleagues, the team, and the
    OWNER's own address CC'd next to a same-named contact (the owner-alias signal)."""
    emails = [e.lower() for e in person.emails if e]
    if not emails:
        return []
    pids = [r[0] for r in con.execute(
        f"SELECT id FROM participants WHERE LOWER(email_address) IN ({','.join('?' * len(emails))})", emails)]
    if not pids:
        return []
    pq = ",".join("?" * len(pids))
    # Two index-driven arms UNION'd, not one OR-filter: a single `sender_id IN (..)
    # OR id IN (subquery)` defeats both idx_messages_sender and
    # idx_message_recipients_participant (SQLite can't use two indexes across an OR),
    # forcing a full per-person scan of every email row — O(emails x people),
    # quadratic in archive size. The UNION lets each arm use its index; the outer
    # GROUP BY dedupes conversations a person both sent and received in. Same rows out.
    convs = con.execute(
        f"""SELECT conversation_id, MAX(at) AS at, MAX(subject) AS subject FROM (
                SELECT m.conversation_id AS conversation_id,
                       COALESCE(m.sent_at, m.received_at, m.internal_date) AS at,
                       m.subject AS subject
                FROM messages m
                WHERE m.message_type='email' AND m.conversation_id IS NOT NULL
                  AND m.sender_id IN ({pq})
                UNION ALL
                SELECT m.conversation_id AS conversation_id,
                       COALESCE(m.sent_at, m.received_at, m.internal_date) AS at,
                       m.subject AS subject
                FROM message_recipients mr
                JOIN messages m ON m.id = mr.message_id
                WHERE m.message_type='email' AND m.conversation_id IS NOT NULL
                  AND mr.participant_id IN ({pq})
            ) GROUP BY conversation_id ORDER BY at DESC LIMIT ?""",
        (*pids, *pids, max_threads)).fetchall()
    threads: list[dict[str, Any]] = []
    for conv_id, _at, subject in convs:
        rows = con.execute(
            """SELECT DISTINCT LOWER(p.email_address) AS email,
                      COALESCE(NULLIF(p.display_name, ''), NULLIF(mr.display_name, ''), '') AS name
               FROM messages m
               JOIN message_recipients mr ON mr.message_id = m.id
               JOIN participants p ON p.id = mr.participant_id
               WHERE m.conversation_id = ?""", (conv_id,)).fetchall()
        roster, seen = [], set()
        for email, name in rows:
            if email and email not in seen:
                seen.add(email)
                roster.append(f"{name} <{email}>" if name else email)
        if roster:
            threads.append({"subject": (subject or "(no subject)")[:120], "participants": roster})
    return threads


# --- iMessage (chat.db), DM-only -------------------------------------------

def probe_chat_db(chat_db: Path) -> dict[str, Any]:
    """Can we actually open + read chat.db? Distinguishes a TCC/Full-Disk-Access
    denial from 'opened fine but no DM matches'. Returns readability + counts."""
    result: dict[str, Any] = {"exists": chat_db.exists(), "readable": False, "messages": 0, "handles": 0, "error": None}
    if not chat_db.exists():
        result["error"] = "chat.db does not exist"
        return result
    try:
        con = sqlite3.connect(f"file:{chat_db}?mode=ro&immutable=1", uri=True)
        try:
            result["messages"] = con.execute("SELECT COUNT(*) FROM message").fetchone()[0]
            result["handles"] = con.execute("SELECT COUNT(*) FROM handle").fetchone()[0]
            result["readable"] = True
        finally:
            con.close()
    except sqlite3.Error as exc:
        # "unable to open database file" / "authorization denied" => Full Disk Access.
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def decode_attributed_body(blob: Any) -> str:
    """Extract readable text from Apple's ``attributedBody`` typedstream blob.

    Newer macOS leaves ``message.text`` NULL and stores the body in this archived
    NSAttributedString. We pull the NSString payload heuristically (length-prefixed
    after the ``NSString`` marker) — good enough for plain DM text; returns "" if
    the layout doesn't match."""
    if not blob:
        return ""
    data = bytes(blob) if not isinstance(blob, bytes) else blob
    try:
        segment = data.split(b"NSString", 1)[1][5:]
        if not segment:
            return ""
        if segment[0] == 0x81:  # 2-byte little-endian length prefix
            length = int.from_bytes(segment[1:3], "little")
            start = 3
        else:
            length = segment[0]
            start = 1
        return segment[start:start + length].decode("utf-8", "replace").strip()
    except (IndexError, UnicodeDecodeError):
        return ""


# DM-only: join through chat_message_join -> chat and exclude group chats
# (chat_identifier LIKE 'chat%'). Restrict to the person's handle rowids.
_IMESSAGE_DM_SQL = """
SELECT m.text AS text, m.attributedBody AS attributed_body,
       m.date AS date, m.is_from_me AS is_from_me
FROM message m
JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
JOIN chat c ON c.ROWID = cmj.chat_id
WHERE m.handle_id IN ({handles})
  AND c.chat_identifier NOT LIKE 'chat%'
  AND (m.associated_message_type IS NULL
       OR m.associated_message_type < 2000
       OR m.associated_message_type > 3006)
ORDER BY m.date DESC
LIMIT ?
"""


# The handle table (rowid -> identifier) is fixed for the duration of a collection
# run, but every iMessage reader needs it. Scan + normalize it ONCE per chat.db
# path and reuse, instead of re-running `SELECT ROWID,id FROM handle` (a full scan)
# three times per person (read_imessage + count_imessage_dms + read_imessage_groups).
_HANDLE_ROWS_CACHE: dict[str, list[tuple[int, str]]] = {}


def _handle_rows(con: sqlite3.Connection, chat_db: Path) -> list[tuple[int, str]]:
    key = str(chat_db)
    cached = _HANDLE_ROWS_CACHE.get(key)
    if cached is None:
        cached = [(int(row["rid"]), phone_digits(str(row["ident"] or "")))
                  for row in con.execute("SELECT ROWID AS rid, id AS ident FROM handle")]
        _HANDLE_ROWS_CACHE[key] = cached
    return cached


def _imessage_handle_ids(con: sqlite3.Connection, person: Person, chat_db: Path) -> list[int]:
    """All handle ROWIDs whose identifier matches one of the person's phones."""
    wanted = {phone_digits(p) for p in person.phones if phone_digits(p)}
    if not wanted:
        return []
    return [rid for rid, digits in _handle_rows(con, chat_db) if digits in wanted]


def read_imessage(person: Person, chat_db: Path, cap: int = CHAT_MESSAGE_CAP) -> list[dict[str, Any]]:
    """Recent DM bodies for the person from chat.db (group chats never read)."""
    if not person.phones or not chat_db.exists():
        return []
    try:
        # immutable=1 lets us read past a live Messages.app WAL lock (read-only).
        con = sqlite3.connect(f"file:{chat_db}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error:
        return []
    con.row_factory = sqlite3.Row
    try:
        handle_ids = _imessage_handle_ids(con, person, chat_db)
        if not handle_ids:
            return []
        sql = _IMESSAGE_DM_SQL.format(handles=",".join("?" for _ in handle_ids))
        rows = con.execute(sql, (*handle_ids, cap)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        text = (row["text"] or "").strip() or decode_attributed_body(row["attributed_body"])
        if not text:
            continue
        out.append({
            "channel": "imessage",
            "at": bec_apple_iso(row["date"]),
            "direction": "from_me" if row["is_from_me"] else "from_them",
            "subject": "",
            "text": text.strip(),
        })
    return out


_IMESSAGE_DM_COUNT_SQL = """
SELECT COUNT(*) FROM message m
JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
JOIN chat c ON c.ROWID = cmj.chat_id
WHERE m.handle_id IN ({handles})
  AND c.chat_identifier NOT LIKE 'chat%'
  AND (m.associated_message_type IS NULL
       OR m.associated_message_type < 2000
       OR m.associated_message_type > 3006)
"""


def count_imessage_dms(person: Person, chat_db: Path) -> int:
    """True total of the person's iMessage DMs (so capping is honest)."""
    if not person.phones or not chat_db.exists():
        return 0
    try:
        con = sqlite3.connect(f"file:{chat_db}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error:
        return 0
    con.row_factory = sqlite3.Row
    try:
        handle_ids = _imessage_handle_ids(con, person, chat_db)
        if not handle_ids:
            return 0
        sql = _IMESSAGE_DM_COUNT_SQL.format(handles=",".join("?" for _ in handle_ids))
        return int(con.execute(sql, tuple(handle_ids)).fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        con.close()


# Group NAMES only (metadata, never bodies): which named group chats a contact is
# in is a relationship signal ("Family", "College Crew"). DMs supply bodies; groups
# supply only names + membership.
_IMESSAGE_GROUPS_SQL = """
SELECT DISTINCT c.display_name AS dn, c.room_name AS rn, c.chat_identifier AS ci
FROM chat c
JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
WHERE chj.handle_id IN ({handles}) AND c.chat_identifier LIKE 'chat%'
"""


def read_imessage_groups(person: Person, chat_db: Path, cap: int = 25) -> list[str]:
    """Named iMessage group chats this contact belongs to (names only, no bodies)."""
    if not person.phones or not chat_db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{chat_db}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error:
        return []
    con.row_factory = sqlite3.Row
    try:
        handle_ids = _imessage_handle_ids(con, person, chat_db)
        if not handle_ids:
            return []
        sql = _IMESSAGE_GROUPS_SQL.format(handles=",".join("?" for _ in handle_ids))
        rows = con.execute(sql, tuple(handle_ids)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    names: list[str] = []
    for row in rows:
        for candidate in (row["dn"], row["rn"]):
            name = (candidate or "").strip()
            if name and name != (row["ci"] or "") and name not in names:
                names.append(name)
    return names[:cap]


# Opt-in only: full group-chat BODIES from small shared groups (off by default).
# Skips groups larger than max_group_size (low signal-per-person + more third parties).
_IMESSAGE_GROUP_MSGS_SQL = """
WITH person_groups AS (
    SELECT DISTINCT c.ROWID AS cid, c.display_name AS dn, c.room_name AS rn
    FROM chat c
    JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
    WHERE chj.handle_id IN ({handles}) AND c.chat_identifier LIKE 'chat%'
),
sized AS (
    SELECT pg.cid, pg.dn, pg.rn,
           (SELECT COUNT(*) FROM chat_handle_join x WHERE x.chat_id = pg.cid) AS n
    FROM person_groups pg
)
SELECT m.text AS text, m.attributedBody AS attributed_body, m.date AS date,
       m.is_from_me AS is_from_me, s.dn AS dn, s.rn AS rn
FROM sized s
JOIN chat_message_join cmj ON cmj.chat_id = s.cid
JOIN message m ON m.ROWID = cmj.message_id
WHERE s.n <= ?
  AND (m.associated_message_type IS NULL
       OR m.associated_message_type < 2000
       OR m.associated_message_type > 3006)
ORDER BY m.date DESC
LIMIT ?
"""


def read_imessage_group_messages(person: Person, chat_db: Path, *, max_group_size: int = 25,
                                 cap: int = CHAT_MESSAGE_CAP) -> list[dict[str, Any]]:
    """Opt-in: message bodies from the person's SMALL shared groups (size-capped)."""
    if not person.phones or not chat_db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{chat_db}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error:
        return []
    con.row_factory = sqlite3.Row
    try:
        handle_ids = _imessage_handle_ids(con, person, chat_db)
        if not handle_ids:
            return []
        sql = _IMESSAGE_GROUP_MSGS_SQL.format(handles=",".join("?" for _ in handle_ids))
        rows = con.execute(sql, (*handle_ids, max_group_size, cap)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        text = (row["text"] or "").strip() or decode_attributed_body(row["attributed_body"])
        if not text:
            continue
        group = (row["dn"] or row["rn"] or "group").strip()
        out.append({
            "channel": "imessage_group",
            "at": bec_apple_iso(row["date"]),
            "direction": "from_me" if row["is_from_me"] else "from_them",
            "subject": group,
            "text": text.strip(),
        })
    return out


def bec_apple_iso(value: Any) -> str:
    """Apple-epoch -> ISO (reuse the messages-pack converter via lazy import)."""
    return _apple_to_iso(value) or ""


# --- WhatsApp (wacli store), DM-only ---------------------------------------

def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r["name"]) for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def read_whatsapp(person: Person, wacli_db: Path = DEFAULT_WACLI_DB, cap: int = CHAT_MESSAGE_CAP) -> list[dict[str, Any]]:
    """Recent DM bodies for the person from the wacli store (groups never read).

    Defensive about schema: the wacli store may be absent or shaped slightly
    differently across versions, so column presence is checked before querying."""
    if not person.phones or not wacli_db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{wacli_db}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    con.row_factory = sqlite3.Row
    try:
        cols = _table_columns(con, "messages")
        if not cols or "chat_jid" not in cols:
            return []
        text_col = "text" if "text" in cols else ("display_text" if "display_text" in cols else None)
        if not text_col:
            return []
        ts_col = "ts" if "ts" in cols else ("timestamp" if "timestamp" in cols else None)
        from_me_col = "from_me" if "from_me" in cols else ("is_from_me" if "is_from_me" in cols else None)
        # WhatsApp JIDs keep the country code (US: "1XXXXXXXXXX@s.whatsapp.net"),
        # but phone_digits() strips a leading US 1 for comparison — so a US number's
        # JID would never match the stripped key. Match BOTH forms: the stripped key
        # and, for 10-digit (US-shaped) keys, the "1"-prefixed E.164 form.
        wanted: set[str] = set()
        for p in person.phones:
            key = phone_digits(p)
            if not key:
                continue
            wanted.add(key)
            if len(key) == 10:
                wanted.add(f"1{key}")
        # DM chat jids look like "<digits>@s.whatsapp.net"; groups end in @g.us.
        jids = [f"{d}@s.whatsapp.net" for d in wanted]
        placeholders = ",".join("?" for _ in jids)
        select = [f"{text_col} AS text"]
        select.append((f"{ts_col} AS ts") if ts_col else "NULL AS ts")
        select.append((f"{from_me_col} AS from_me") if from_me_col else "0 AS from_me")
        order = f"ORDER BY {ts_col} DESC" if ts_col else ""
        sql = (
            f"SELECT {', '.join(select)} FROM messages "
            f"WHERE chat_jid IN ({placeholders}) AND chat_jid NOT LIKE '%@g.us' "
            f"{order} LIMIT ?"
        )
        rows = con.execute(sql, (*jids, cap)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        text = (row["text"] or "").strip()
        if not text:
            continue
        out.append({
            "channel": "whatsapp",
            "at": _whatsapp_iso(row["ts"]),
            "direction": "from_me" if row["from_me"] else "from_them",
            "subject": "",
            "text": text,
        })
    return out


# --- timestamp helpers (lazy import keeps the messages pack optional) -------

def _apple_to_iso(value: Any) -> str | None:
    from datetime import datetime, timezone

    if value is None:
        return None
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return None
    if raw > 10_000_000_000:
        unix_ts = (raw / 1_000_000_000) + 978_307_200
    elif raw < 2_000_000_000:
        unix_ts = raw + 978_307_200
    else:
        unix_ts = raw
    try:
        return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _whatsapp_iso(value: Any) -> str:
    from datetime import datetime, timezone

    if value in (None, "", 0):
        return ""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    if ts > 1e12:
        ts /= 1000
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return ""


# --- shared signal ranking for adaptive sampling ---------------------------

def signal_rank(message: dict[str, Any]) -> tuple[int, str]:
    """Rank a chat/email message for adaptive keep: identity signal, then recency."""
    return (bec.signal_score(message.get("text", "")), message.get("at") or "")
