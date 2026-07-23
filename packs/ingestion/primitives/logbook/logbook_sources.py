"""Streaming, uncapped, group-aware message-body readers for $logbook.

Every reader is a GENERATOR that iterates a single ordered cursor row-by-row
(never ``fetchall``) so resident memory stays flat regardless of archive size —
one message in flight at a time. Rows come out grouped by container
(thread / DM / group) and ordered by time within a container, so the renderer can
open one output file per container and emit ``## YYYY`` on year change.

Each yielded row is a normalized dict:

    {"channel": "gmail"|"imessage"|"whatsapp",
     "kind": "thread"|"dm"|"group",
     "container_id": <gmail thread id | "dm" | chat guid/jid>,
     "container_title": <subject | group name | "">,
     "msg_id": <stable per-message id>,            # for dedupe
     "watermark": <monotonic int>,                 # gmail messages.id / chat ROWID / wacli rowid
     "at": <iso8601>, "year": <int|None>,
     "sender": <display name | "me">,
     "direction": "from_me"|"from_them",
     "subject": <gmail thread subject | "">,
     "text": <full verbatim body>}

The ``watermark`` is the incremental cursor: ``sync`` re-reads only rows whose
watermark exceeds the per-channel max recorded last run (filtered in SQL).

Reuses ``deep_context.sources`` wholesale for Apple ``attributedBody`` decoding,
the immutable read-only chat.db open, and the apple-epoch converter — so
identity/timestamp logic never drifts. The candidate-pid temp table is built
through ``MsgvaultStore`` (``dcs.gni``) on our own read-only msgvault connection.
"""
from __future__ import annotations

import email
import re
import sqlite3
import zlib
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from typing import Any, Iterator

from packs.ingestion.primitives.deep_context import sources as dcs
from packs.ingestion.primitives.deep_context.common import Person, phone_digits

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_BLANKS_RE = re.compile(r"\n{3,}")


def _year_of(at: str) -> int | None:
    head = (at or "")[:4]
    return int(head) if head.isdigit() else None


# Decompress at most this many output bytes from the raw message head. MIME orders
# text + forwarded parts (and every part's headers, hence attachment NAMES) FIRST and
# the big binary attachment bodies LAST — so this cap keeps all the readable content
# while the heavy attachment payload is never materialized. Bounds peak RSS flat.
_RAW_DECOMP_CAP = 6 * 1024 * 1024


def _decompress_head(blob: bytes, compression: str | None, cap: int) -> bytes:
    """Decompress up to ``cap`` output bytes (tolerant of a truncated input stream)."""
    if (compression or "").lower() != "zlib":
        return blob[:cap]
    try:
        return zlib.decompressobj().decompress(blob, cap)
    except zlib.error:
        return b""


def _mime_full_text(raw_data: Any, compression: str | None) -> str:
    """Readable text from the stored raw MIME head — INCLUDING forwarded/nested
    messages, with attachments named but NOT inlined.

    msgvault's ``body_text`` keeps only the top text part (drops the forwarded essay).
    We decompress the message HEAD and walk its parts: text/plain + forwarded text are
    kept verbatim; each attachment becomes a ``[attachment: name (type)]`` marker (the
    binary is never read). Only the head is decompressed, so a fat attachment can't
    blow up memory — its body sits past the cap and is simply not loaded."""
    if not raw_data:
        return ""
    blob = bytes(raw_data) if not isinstance(raw_data, (bytes, bytearray)) else raw_data
    raw = _decompress_head(blob, compression, _RAW_DECOMP_CAP)
    if not raw:
        return ""
    try:
        msg = email.message_from_bytes(raw)
    except (ValueError, TypeError):
        return ""
    chunks: list[str] = []
    html_only: list[str] = []
    has_plain = False
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "message/rfc822":  # forwarded message — mark the boundary, walk() recurses in
            payload = part.get_payload()
            sub = payload[0] if isinstance(payload, list) and payload else None
            if isinstance(sub, Message):
                chunks.append(
                    "\n---------- Forwarded message ----------\n"
                    f"From: {sub.get('From', '')}\nDate: {sub.get('Date', '')}\n"
                    f"Subject: {sub.get('Subject', '')}\nTo: {sub.get('To', '')}\n"
                )
            continue
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = str(part.get("Content-Disposition") or "").lower()
        if filename or "attachment" in disposition:
            # Keep a reference, NOT the bytes (the whole point — content but no attachment).
            chunks.append(f"\n[attachment: {filename or 'unnamed'} ({ctype})]\n")
            continue
        try:
            payload = part.get_payload(decode=True)
        except (LookupError, ValueError):
            payload = None
        if not payload:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        if ctype == "text/plain":
            chunks.append(text)
            has_plain = True
        elif ctype == "text/html":
            html_only.append(text)
    body = "".join(chunks).strip()
    if not has_plain and html_only:
        body = _html_to_text("\n".join(html_only))
    return body


def _html_to_text(html_body: str) -> str:
    """Cheap HTML -> text fallback when ``body_text`` is empty (raw fidelity, no deps)."""
    text = _TAG_RE.sub(" ", html_body or "")
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'").replace("&quot;", '"'))
    text = _WS_RE.sub(" ", text)
    return _BLANKS_RE.sub("\n\n", text).strip()


def _whatsapp_iso(value: Any) -> str:
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


# --- Gmail (msgvault), full thread bodies ----------------------------------

_GMAIL_NOT_DELETED = (
    "m.message_type = 'email' "
    "AND (m.deleted_at IS NULL OR m.deleted_at = '') "
    "AND (m.deleted_from_source_at IS NULL OR m.deleted_from_source_at = '')"
)

# Discover the conversation ids the contact participates in, materialized into a
# real indexed temp table. We MUST materialize (not leave as a CTE) — as a CTE the
# planner makes it a co-routine and drives the outer query from idx_messages_type
# (every email in the archive) doing a full SCAN of the conversation set per row:
# O(all_messages * threads). Materialized + indexed, the planner drives from the
# small conversation set into messages via idx_messages_conversation. Huge diff.
_GMAIL_CONVS_SQL = f"""
CREATE TEMP TABLE lb_convs AS
SELECT DISTINCT m.conversation_id AS cid
FROM cand_pid cp JOIN messages m ON m.sender_id = cp.pid
WHERE {_GMAIL_NOT_DELETED} AND m.conversation_id IS NOT NULL
UNION
SELECT DISTINCT m.conversation_id
FROM cand_pid cp
JOIN message_recipients mr ON mr.participant_id = cp.pid
JOIN messages m ON m.id = mr.message_id
WHERE {_GMAIL_NOT_DELETED} AND m.conversation_id IS NOT NULL
"""

# All messages in every thread the contact participates in (full thread fidelity,
# not just their own messages). The SORTED scan stays LIGHTWEIGHT — no body/raw
# blobs in the SELECT, or the ORDER BY materializes every blob in the sort buffer
# (1.7GB+ peak). Bodies are fetched per message by primary key in the loop instead,
# so only one raw message is decompressed/parsed in memory at a time.
_GMAIL_THREADS_SQL = f"""
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
WHERE {_GMAIL_NOT_DELETED} AND m.id > ?
ORDER BY m.conversation_id, at, m.id
"""

# Read only the first ~2MB of the COMPRESSED blob (substr on a blob reads a prefix,
# not the whole value) — the message head holds all text + forwarded parts +
# attachment headers, so we never pull a multi-hundred-MB attachment into memory.
_RAW_HEAD_COMPRESSED = 2 * 1024 * 1024
_GMAIL_RAW_SQL = "SELECT substr(raw_data, 1, ?) AS head, compression FROM message_raw WHERE message_id = ?"
_GMAIL_BODY_SQL = "SELECT body_text, body_html FROM message_bodies WHERE message_id = ?"


def _gmail_body(con: sqlite3.Connection, mid: int) -> str:
    """Full verbatim body for one message: raw MIME head (incl. forwards + attachment
    names) first, then the extracted text/html fallback. Reads only the head by PK so
    the streaming sort stays blob-free and a fat attachment can't blow up memory."""
    raw = con.execute(_GMAIL_RAW_SQL, (_RAW_HEAD_COMPRESSED, mid)).fetchone()
    if raw is not None and raw["head"]:
        body = _mime_full_text(raw["head"], raw["compression"])
        if body:
            return body
    fb = con.execute(_GMAIL_BODY_SQL, (mid,)).fetchone()
    if fb is not None:
        return (fb["body_text"] or "").strip() or _html_to_text(fb["body_html"] or "")
    return ""


def open_msgvault(msgvault_db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{msgvault_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _build_gmail_convs(con: sqlite3.Connection, person: Person) -> int:
    """Build cand_pid + the materialized lb_convs temp table. Returns thread count."""
    if not person.emails or not dcs.gni.MsgvaultStore(connection=con).create_candidate_pid_table(person.emails):
        return 0
    con.execute("DROP TABLE IF EXISTS lb_convs")
    con.execute(_GMAIL_CONVS_SQL)
    con.execute("CREATE INDEX lb_convs_cid ON lb_convs(cid)")
    return int(con.execute("SELECT COUNT(*) FROM lb_convs").fetchone()[0])


def stream_gmail(person: Person, con: sqlite3.Connection, *, since_id: int = 0) -> Iterator[dict[str, Any]]:
    """Yield every message in the person's email threads, oldest-first per thread."""
    if not _build_gmail_convs(con, person):
        return
    body_cur = con.cursor()
    for row in con.execute(_GMAIL_THREADS_SQL, (since_id,)):
        # Full raw MIME (keeps forwarded/nested messages), fetched by PK on a separate
        # cursor so the sorted scan above never holds a blob.
        body = _gmail_body(body_cur, int(row["mid"]))
        if not body:
            continue
        at = str(row["at"] or "")
        from_me = bool(row["is_from_me"])
        sender = "me" if from_me else (row["sender_name"] or row["sender_email"] or "unknown")
        yield {
            "channel": "gmail",
            "kind": "thread",
            "container_id": row["thread_id"] or f"conv-{row['cid']}",
            "container_title": (row["subject"] or row["title"] or "").strip(),
            "msg_id": row["src_id"] or f"mid-{row['mid']}",
            "watermark": int(row["mid"]),
            "at": at,
            "year": _year_of(at),
            "sender": sender,
            "direction": "from_me" if from_me else "from_them",
            "subject": (row["subject"] or row["title"] or "").strip(),
            "text": body,
        }


def count_gmail(person: Person, con: sqlite3.Connection) -> tuple[int, int]:
    """(messages, threads) for the person — COUNT only, no body reads."""
    threads = _build_gmail_convs(con, person)
    if not threads:
        return 0, 0
    sql = f"SELECT COUNT(*) FROM lb_convs JOIN messages m ON m.conversation_id = lb_convs.cid WHERE {_GMAIL_NOT_DELETED}"
    return int(con.execute(sql).fetchone()[0]), threads


# --- iMessage (chat.db) -----------------------------------------------------

def _open_chat_db(chat_db: Path) -> sqlite3.Connection | None:
    if not chat_db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{chat_db}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error:
        return None
    con.row_factory = sqlite3.Row
    return con


def _imessage_handle_ids(con: sqlite3.Connection, person: Person) -> list[int]:
    wanted = {phone_digits(p) for p in person.phones if phone_digits(p)}
    wanted |= {e for e in person.emails if e}  # iMessage handles can be emails too
    if not wanted:
        return []
    ids: list[int] = []
    for row in con.execute("SELECT ROWID AS rid, id AS ident FROM handle"):
        ident = str(row["ident"] or "")
        if ident.lower() in wanted or phone_digits(ident) in wanted:
            ids.append(int(row["rid"]))
    return ids


_IMSG_DM_NOT_REACTION = (
    "(m.associated_message_type IS NULL OR m.associated_message_type < 2000 OR m.associated_message_type > 3006)"
)

_IMSG_DM_SQL = f"""
SELECT m.ROWID AS rid, m.guid AS guid, m.text AS text, m.attributedBody AS attributed_body,
       m.date AS date, m.is_from_me AS is_from_me, h.id AS handle
FROM message m
JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
JOIN chat c ON c.ROWID = cmj.chat_id
LEFT JOIN handle h ON h.ROWID = m.handle_id
WHERE m.handle_id IN ({{handles}})
  AND c.chat_identifier NOT LIKE 'chat%'
  AND {_IMSG_DM_NOT_REACTION}
  AND m.ROWID > ?
ORDER BY m.date, m.ROWID
"""


def stream_imessage_dm(person: Person, chat_db: Path, *, since_rowid: int = 0) -> Iterator[dict[str, Any]]:
    con = _open_chat_db(chat_db)
    if con is None:
        return
    try:
        handle_ids = _imessage_handle_ids(con, person)
        if not handle_ids:
            return
        sql = _IMSG_DM_SQL.format(handles=",".join("?" for _ in handle_ids))
        for row in con.execute(sql, (*handle_ids, since_rowid)):
            text = (row["text"] or "").strip() or dcs.decode_attributed_body(row["attributed_body"])
            if not text:
                continue
            at = dcs.bec_apple_iso(row["date"])
            from_me = bool(row["is_from_me"])
            yield {
                "channel": "imessage",
                "kind": "dm",
                "container_id": "dm",
                "container_title": person.full_name,
                "msg_id": row["guid"] or f"rid-{row['rid']}",
                "watermark": int(row["rid"]),
                "at": at,
                "year": _year_of(at),
                "sender": "me" if from_me else (person.full_name or row["handle"] or "them"),
                "direction": "from_me" if from_me else "from_them",
                "subject": "",
                "text": text.strip(),
            }
    finally:
        con.close()


def count_imessage_dm(person: Person, chat_db: Path) -> tuple[int, int]:
    con = _open_chat_db(chat_db)
    if con is None:
        return 0, 0
    try:
        handle_ids = _imessage_handle_ids(con, person)
        if not handle_ids:
            return 0, 0
        sql = (
            "SELECT COUNT(*) FROM message m "
            "JOIN chat_message_join cmj ON cmj.message_id = m.ROWID "
            "JOIN chat c ON c.ROWID = cmj.chat_id "
            f"WHERE m.handle_id IN ({','.join('?' for _ in handle_ids)}) "
            f"AND c.chat_identifier NOT LIKE 'chat%' AND {_IMSG_DM_NOT_REACTION}"
        )
        n = int(con.execute(sql, tuple(handle_ids)).fetchone()[0])
        return n, (1 if n else 0)
    except sqlite3.Error:
        return 0, 0
    finally:
        con.close()


_IMSG_GROUPS_FOR_PERSON_SQL = """
SELECT DISTINCT c.ROWID AS cid, c.guid AS guid, c.display_name AS dn, c.room_name AS rn, c.chat_identifier AS ci
FROM chat c
JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
WHERE chj.handle_id IN ({handles}) AND c.chat_identifier LIKE 'chat%'
"""


def build_imessage_name_map(wacli_db: Path, msgvault_db: Path, people: list[Person]) -> dict[str, str]:
    """phone-digits -> display name, merged from wacli contacts + msgvault participants +
    the CSV people (CSV wins). Lets us name an UNNAMED iMessage group by its members."""
    name_map: dict[str, str] = {}
    if msgvault_db.exists():
        try:
            con = sqlite3.connect(f"file:{msgvault_db}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            try:
                for row in con.execute("SELECT phone_number, display_name FROM participants WHERE phone_number IS NOT NULL AND phone_number != '' AND display_name IS NOT NULL AND display_name != ''"):
                    key = phone_digits(str(row["phone_number"]))
                    if key and key not in name_map:
                        name_map[key] = str(row["display_name"]).strip()
            finally:
                con.close()
        except sqlite3.Error:
            pass
    con = _open_wacli(wacli_db)
    if con is not None:
        try:
            for row in con.execute("SELECT phone, full_name, push_name FROM contacts WHERE phone IS NOT NULL AND phone != ''"):
                key = phone_digits(str(row["phone"]))
                name = (row["full_name"] or row["push_name"] or "").strip()
                if key and name:
                    name_map[key] = name  # wacli names are well-curated → override
        except sqlite3.Error:
            pass
        finally:
            con.close()
    for person in people:  # CSV is most authoritative
        if person.full_name:
            for ph in person.phones:
                key = phone_digits(ph)
                if key:
                    name_map[key] = person.full_name
    return name_map


def _handle_display(handle: str, name_map: dict[str, str]) -> str:
    h = str(handle or "").strip()
    return name_map.get(phone_digits(h)) or name_map.get(h.lower()) or h


def resolve_imessage_groups(person: Person, chat_db: Path, name_map: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Group chats (guid + title) the person belongs to. Empty without --include-groups.

    Unnamed groups (no display/room name) are titled by their members joined with
    ' - ' (e.g. 'Amir Moazami - Jake Zeller'), resolved via ``name_map``."""
    name_map = name_map or {}
    con = _open_chat_db(chat_db)
    if con is None:
        return []
    try:
        handle_ids = _imessage_handle_ids(con, person)
        if not handle_ids:
            return []
        sql = _IMSG_GROUPS_FOR_PERSON_SQL.format(handles=",".join("?" for _ in handle_ids))
        groups = list(con.execute(sql, tuple(handle_ids)))

        def _named_title(row: sqlite3.Row) -> str:
            # display_name wins; room_name only if it's a real name (it's often just
            # the chat_identifier, which is NOT a friendly name).
            dn = (row["dn"] or "").strip()
            if dn:
                return dn
            rn = (row["rn"] or "").strip()
            return rn if rn and rn != (row["ci"] or "").strip() else ""

        # Fetch members for the UNNAMED groups so we can title them by participants.
        need_members = [int(r["cid"]) for r in groups if not _named_title(r)]
        members: dict[int, list[str]] = {}
        if need_members:
            ph = ",".join("?" for _ in need_members)
            for row in con.execute(
                f"SELECT chj.chat_id AS cid, h.id AS handle FROM chat_handle_join chj "
                f"JOIN handle h ON h.ROWID = chj.handle_id WHERE chj.chat_id IN ({ph})",
                tuple(need_members),
            ):
                members.setdefault(int(row["cid"]), []).append(_handle_display(row["handle"], name_map))
        out: list[dict[str, Any]] = []
        for row in groups:
            cid = int(row["cid"])
            title = _named_title(row)
            if not title:
                names = sorted(dict.fromkeys(n for n in members.get(cid, []) if n))
                title = " - ".join(names) if names else (row["ci"] or "group")
            out.append({"chat_rowid": cid, "guid": row["guid"] or row["ci"], "title": title})
        return out
    except sqlite3.Error:
        return []
    finally:
        con.close()


_IMSG_GROUP_MSGS_SQL = f"""
SELECT m.ROWID AS rid, m.guid AS guid, m.text AS text, m.attributedBody AS attributed_body,
       m.date AS date, m.is_from_me AS is_from_me, h.id AS handle
FROM chat_message_join cmj
JOIN message m ON m.ROWID = cmj.message_id
LEFT JOIN handle h ON h.ROWID = m.handle_id
WHERE cmj.chat_id = ? AND {_IMSG_DM_NOT_REACTION} AND m.ROWID > ?
ORDER BY m.date, m.ROWID
"""


def stream_imessage_group(chat_db: Path, chat_rowid: int, title: str, guid: str, *, since_rowid: int = 0) -> Iterator[dict[str, Any]]:
    con = _open_chat_db(chat_db)
    if con is None:
        return
    try:
        for row in con.execute(_IMSG_GROUP_MSGS_SQL, (chat_rowid, since_rowid)):
            text = (row["text"] or "").strip() or dcs.decode_attributed_body(row["attributed_body"])
            if not text:
                continue
            at = dcs.bec_apple_iso(row["date"])
            from_me = bool(row["is_from_me"])
            yield {
                "channel": "imessage",
                "kind": "group",
                "container_id": guid,
                "container_title": title,
                "msg_id": row["guid"] or f"rid-{row['rid']}",
                "watermark": int(row["rid"]),
                "at": at,
                "year": _year_of(at),
                "sender": "me" if from_me else (row["handle"] or "member"),
                "direction": "from_me" if from_me else "from_them",
                "subject": "",
                "text": text.strip(),
            }
    finally:
        con.close()


# --- WhatsApp (wacli store) -------------------------------------------------

def _open_wacli(wacli_db: Path) -> sqlite3.Connection | None:
    if not wacli_db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{wacli_db}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    con.row_factory = sqlite3.Row
    return con


def _wacli_text(row: sqlite3.Row) -> str:
    for col in ("text", "display_text", "media_caption"):
        try:
            value = (row[col] or "").strip()
        except (IndexError, KeyError):
            value = ""
        if value:
            return value
    try:
        media = (row["media_type"] or "").strip()
    except (IndexError, KeyError):
        media = ""
    return f"[{media}]" if media else ""


_WA_DM_SQL = (
    "SELECT rowid AS rid, msg_id, sender_name, ts, from_me, text, display_text, media_caption, media_type "
    "FROM messages WHERE chat_jid IN ({jids}) AND chat_jid NOT LIKE '%@g.us' AND rowid > ? "
    "ORDER BY ts, rowid"
)


def _wa_dm_jids(person: Person) -> list[str]:
    """WhatsApp DM jids. WhatsApp keeps the FULL international number (incl. country
    code) in the jid, but ``phone_digits`` strips a US ``1`` — so try both the full
    digits and the stripped form, else US numbers silently miss their own DMs."""
    forms: set[str] = set()
    for p in person.phones:
        full = re.sub(r"\D", "", p or "")
        if full:
            forms.add(full)
        stripped = phone_digits(p)
        if stripped:
            forms.add(stripped)
    return [f"{d}@s.whatsapp.net" for d in forms]


def stream_whatsapp_dm(person: Person, wacli_db: Path, *, since_rowid: int = 0) -> Iterator[dict[str, Any]]:
    con = _open_wacli(wacli_db)
    if con is None:
        return
    try:
        jids = _wa_dm_jids(person)
        if not jids:
            return
        sql = _WA_DM_SQL.format(jids=",".join("?" for _ in jids))
        for row in con.execute(sql, (*jids, since_rowid)):
            text = _wacli_text(row)
            if not text:
                continue
            at = _whatsapp_iso(row["ts"])
            from_me = bool(row["from_me"])
            yield {
                "channel": "whatsapp",
                "kind": "dm",
                "container_id": "dm",
                "container_title": person.full_name,
                "msg_id": row["msg_id"] or f"rid-{row['rid']}",
                "watermark": int(row["rid"]),
                "at": at,
                "year": _year_of(at),
                "sender": "me" if from_me else (person.full_name or row["sender_name"] or "them"),
                "direction": "from_me" if from_me else "from_them",
                "subject": "",
                "text": text,
            }
    finally:
        con.close()


def count_whatsapp_dm(person: Person, wacli_db: Path) -> tuple[int, int]:
    con = _open_wacli(wacli_db)
    if con is None:
        return 0, 0
    try:
        jids = _wa_dm_jids(person)
        if not jids:
            return 0, 0
        sql = f"SELECT COUNT(*) FROM messages WHERE chat_jid IN ({','.join('?' for _ in jids)}) AND chat_jid NOT LIKE '%@g.us'"
        n = int(con.execute(sql, tuple(jids)).fetchone()[0])
        return n, (1 if n else 0)
    except sqlite3.Error:
        return 0, 0
    finally:
        con.close()


def _wa_group_title(con: sqlite3.Connection, jid: str, fallback: str = "") -> str:
    for table in ("chats", "groups"):
        try:
            row = con.execute(f"SELECT name FROM {table} WHERE jid = ?", (jid,)).fetchone()
        except sqlite3.Error:
            row = None
        name = (row["name"] if row else "") or ""
        if name and name != jid:
            return str(name)
    return fallback or jid


def resolve_whatsapp_groups(wacli_db: Path, names: list[str], person: Person | None = None) -> list[dict[str, Any]]:
    """Group chats (jid + title) matching CSV ``names`` and/or the person's membership.

    Membership can't use ``group_participants`` (those are privacy ``@lid`` ids with
    no phone mapping in the store). Instead we use the phone-based ``messages.sender_jid``
    — groups the person has actually spoken in. Name matching covers CSV-listed groups."""
    con = _open_wacli(wacli_db)
    if con is None:
        return []
    try:
        found: dict[str, str] = {}
        wanted = {re.sub(r"\s+", " ", n.strip().lower()) for n in names if n.strip()}
        if wanted:
            # Friendly group names can live in chats/groups.name OR only in
            # messages.chat_name — check all three so CSV-named groups resolve.
            sources_sql = [
                "SELECT jid, name FROM chats WHERE jid LIKE '%@g.us'",
                "SELECT jid, name FROM groups",
                "SELECT DISTINCT chat_jid AS jid, chat_name AS name FROM messages WHERE chat_jid LIKE '%@g.us'",
            ]
            for q in sources_sql:
                try:
                    rows = con.execute(q)
                except sqlite3.Error:
                    continue
                for row in rows:
                    name = re.sub(r"\s+", " ", str(row["name"] or "").strip().lower())
                    if name and name in wanted and row["jid"] not in found:
                        found[row["jid"]] = str(row["name"] or row["jid"])
        if person is not None:
            digit_forms: set[str] = set()
            for p in person.phones:
                full = re.sub(r"\D", "", p or "")
                if full:
                    digit_forms.add(full)
                stripped = phone_digits(p)
                if stripped:
                    digit_forms.add(stripped)
            sender_jids = [f"{d}@s.whatsapp.net" for d in digit_forms]
            if sender_jids:
                ph = ",".join("?" for _ in sender_jids)
                sql = (f"SELECT DISTINCT chat_jid, chat_name FROM messages "
                       f"WHERE chat_jid LIKE '%@g.us' AND sender_jid IN ({ph})")
                try:
                    for row in con.execute(sql, tuple(sender_jids)):
                        jid = row["chat_jid"]
                        if jid not in found:
                            found[jid] = _wa_group_title(con, jid, str(row["chat_name"] or jid))
                except sqlite3.Error:
                    pass
        return [{"jid": jid, "title": title} for jid, title in found.items()]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def whatsapp_target_jids(wacli_db: Path, person: Person, group_names: list[str]) -> list[str]:
    """Existing WhatsApp chat_jids (DMs + groups) relevant to a person, for SCOPED
    `wacli history backfill --chat <jid>` backfill — so we deepen only the conversations
    that matter, not the user's entire WhatsApp."""
    jids: list[str] = [g["jid"] for g in resolve_whatsapp_groups(wacli_db, group_names, person=person)]
    con = _open_wacli(wacli_db)
    if con is not None:
        try:
            cand = _wa_dm_jids(person)
            if cand:
                ph = ",".join("?" for _ in cand)
                for row in con.execute(f"SELECT DISTINCT chat_jid FROM messages WHERE chat_jid IN ({ph})", tuple(cand)):
                    jids.append(row["chat_jid"])
        except sqlite3.Error:
            pass
        finally:
            con.close()
    return list(dict.fromkeys(jids))


_WA_GROUP_SQL = (
    "SELECT rowid AS rid, msg_id, sender_name, ts, from_me, text, display_text, media_caption, media_type "
    "FROM messages WHERE chat_jid = ? AND rowid > ? ORDER BY ts, rowid"
)


def stream_whatsapp_group(wacli_db: Path, jid: str, title: str, *, since_rowid: int = 0) -> Iterator[dict[str, Any]]:
    con = _open_wacli(wacli_db)
    if con is None:
        return
    try:
        for row in con.execute(_WA_GROUP_SQL, (jid, since_rowid)):
            text = _wacli_text(row)
            if not text:
                continue
            at = _whatsapp_iso(row["ts"])
            from_me = bool(row["from_me"])
            yield {
                "channel": "whatsapp",
                "kind": "group",
                "container_id": jid,
                "container_title": title,
                "msg_id": row["msg_id"] or f"rid-{row['rid']}",
                "watermark": int(row["rid"]),
                "at": at,
                "year": _year_of(at),
                "sender": "me" if from_me else (row["sender_name"] or "member"),
                "direction": "from_me" if from_me else "from_them",
                "subject": "",
                "text": text,
            }
    finally:
        con.close()
