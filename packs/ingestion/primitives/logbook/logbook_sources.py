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

Reuses the message-discovery ``chatdb`` reader for Apple handle resolution,
reaction filtering, attributed-body decoding, immutable reads, and timestamps.
The wacli store reader likewise owns WhatsApp schema capabilities, JID matching,
group resolution, body queries, and timestamps.
The candidate-pid temp table is built through ``MsgvaultStore`` (``dcs.gni``)
on our own read-only msgvault connection.
"""

from __future__ import annotations

import email
import re
import zlib
from email.message import Message
from pathlib import Path
from typing import Any, Iterator

from packs.ingestion.primitives.deep_context.collection import context_sources as dcs
from packs.ingestion.primitives.deep_context.shared.common import Person, phone_digits
from packs.ingestion.primitives.discover.messages import chatdb
from packs.ingestion.primitives.discover.messages.wacli import message_db as wacli_messages
from packs.ingestion.primitives.discover.messages.wacli import store_db as wacli_store

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
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
    )
    text = _WS_RE.sub(" ", text)
    return _BLANKS_RE.sub("\n\n", text).strip()


# --- Gmail (msgvault), full thread bodies ----------------------------------

# Read only the first ~2MB of the COMPRESSED blob (substr on a blob reads a prefix,
# not the whole value) — the message head holds all text + forwarded parts +
# attachment headers, so we never pull a multi-hundred-MB attachment into memory.
_RAW_HEAD_COMPRESSED = 2 * 1024 * 1024


def _gmail_body(store: "dcs.gni.MsgvaultStore", mid: int) -> str:
    """Full verbatim body for one message: raw MIME head (incl. forwards + attachment
    names) first, then the extracted text/html fallback. Reads only the head by PK so
    the streaming sort stays blob-free and a fat attachment can't blow up memory."""
    parts = store.logbook_body_parts(mid, _RAW_HEAD_COMPRESSED)
    if parts["head"]:
        body = _mime_full_text(parts["head"], parts["compression"])
        if body:
            return body
    return str(parts["body_text"] or "").strip() or _html_to_text(parts["body_html"] or "")


def open_msgvault(msgvault_db: Path) -> Any:
    return dcs.gni.MsgvaultStore(msgvault_db).connect()


def _build_gmail_convs(con: Any, person: Person) -> int:
    """Build cand_pid + the materialized lb_convs temp table. Returns thread count."""
    if not person.emails:
        return 0
    return dcs.gni.MsgvaultStore(connection=con).prepare_logbook_conversations(person.emails)


def stream_gmail(person: Person, con: Any, *, since_id: int = 0) -> Iterator[dict[str, Any]]:
    """Yield every message in the person's email threads, oldest-first per thread."""
    if not _build_gmail_convs(con, person):
        return
    store = dcs.gni.MsgvaultStore(connection=con)
    for row in store.stream_logbook_thread_rows(since_id):
        # Full raw MIME (keeps forwarded/nested messages), fetched by PK on a separate
        # cursor so the sorted scan above never holds a blob.
        body = _gmail_body(store, int(row["mid"]))
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


def count_gmail(person: Person, con: Any) -> tuple[int, int]:
    """(messages, threads) for the person — COUNT only, no body reads."""
    threads = _build_gmail_convs(con, person)
    if not threads:
        return 0, 0
    return dcs.gni.MsgvaultStore(connection=con).count_logbook_messages(), threads


# --- iMessage (chat.db) -----------------------------------------------------


def stream_imessage_dm(person: Person, chat_db: Path, *, since_rowid: int = 0) -> Iterator[dict[str, Any]]:
    if not chat_db.exists():
        return
    try:
        con = chatdb.open_sqlite_readonly(chat_db, immutable=True)
    except chatdb.DatabaseError:
        return
    try:
        handle_ids = chatdb.resolve_handle_ids(
            con,
            (*person.phones, *person.emails),
            cache_key=chat_db,
        )
        if not handle_ids:
            return
        for row in chatdb.query_direct_messages(con, handle_ids, since_rowid=since_rowid):
            text = chatdb.message_text(row)
            if not text:
                continue
            at = chatdb.apple_timestamp_to_iso(row["date"]) or ""
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
    if not chat_db.exists():
        return 0, 0
    try:
        con = chatdb.open_sqlite_readonly(chat_db, immutable=True)
    except chatdb.DatabaseError:
        return 0, 0
    try:
        handle_ids = chatdb.resolve_handle_ids(
            con,
            (*person.phones, *person.emails),
            cache_key=chat_db,
        )
        n = chatdb.count_direct_messages(con, handle_ids)
        return n, (1 if n else 0)
    except chatdb.DatabaseError:
        return 0, 0
    finally:
        con.close()


def build_imessage_name_map(wacli_db: Path, msgvault_db: Path, people: list[Person]) -> dict[str, str]:
    """phone-digits -> display name, merged from wacli contacts + msgvault participants +
    the CSV people (CSV wins). Lets us name an UNNAMED iMessage group by its members."""
    name_map: dict[str, str] = {}
    if msgvault_db.exists():
        store = dcs.gni.MsgvaultStore(msgvault_db)
        try:
            store.connect()
            for row in store.participant_phone_names():
                key = phone_digits(row["phone_number"])
                if key and key not in name_map:
                    name_map[key] = row["display_name"].strip()
        except SystemExit:
            pass
        finally:
            store.close()
    con = None
    if wacli_db.exists():
        try:
            con = wacli_store.open_readonly_db(wacli_db)
        except wacli_store.DatabaseError:
            pass
    if con is not None:
        try:
            for row in wacli_store.contact_rows(con):
                key = phone_digits(str(row.get("phone") or ""))
                name = str(row.get("full_name") or row.get("push_name") or "").strip()
                if key and name:
                    name_map[key] = name  # wacli names are well-curated → override
        except wacli_store.DatabaseError:
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


def resolve_imessage_groups(
    person: Person, chat_db: Path, name_map: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Group chats (guid + title) the person belongs to. Empty without --include-groups.

    Unnamed groups (no display/room name) are titled by their members joined with
    ' - ' (e.g. 'Jordan Bravo - Casey Delta'), resolved via ``name_map``."""
    name_map = name_map or {}
    if not chat_db.exists():
        return []
    try:
        con = chatdb.open_sqlite_readonly(chat_db, immutable=True)
    except chatdb.DatabaseError:
        return []
    try:
        handle_ids = chatdb.resolve_handle_ids(
            con,
            (*person.phones, *person.emails),
            cache_key=chat_db,
        )
        if not handle_ids:
            return []
        groups = list(chatdb.query_group_chats_for_handles(con, handle_ids))

        def _named_title(row: Any) -> str:
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
            for row in chatdb.query_group_members(con, need_members):
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
    except chatdb.DatabaseError:
        return []
    finally:
        con.close()


def stream_imessage_group(
    chat_db: Path, chat_rowid: int, title: str, guid: str, *, since_rowid: int = 0
) -> Iterator[dict[str, Any]]:
    if not chat_db.exists():
        return
    try:
        con = chatdb.open_sqlite_readonly(chat_db, immutable=True)
    except chatdb.DatabaseError:
        return
    try:
        for row in chatdb.query_group_messages(
            con,
            chat_rowid,
            since_rowid=since_rowid,
        ):
            text = chatdb.message_text(row)
            if not text:
                continue
            at = chatdb.apple_timestamp_to_iso(row["date"]) or ""
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


def stream_whatsapp_dm(person: Person, wacli_db: Path, *, since_rowid: int = 0) -> Iterator[dict[str, Any]]:
    if not wacli_db.exists():
        return
    try:
        con = wacli_store.open_readonly_db(wacli_db)
    except wacli_store.DatabaseError:
        return
    try:
        for row in wacli_messages.query_whatsapp_messages(
            con,
            phones=person.phones,
            since_rowid=since_rowid,
        ):
            text = wacli_messages.whatsapp_message_text(row)
            if not text:
                continue
            at = wacli_store.whatsapp_epoch_to_iso(row["ts"]) or ""
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
    if not wacli_db.exists():
        return 0, 0
    try:
        con = wacli_store.open_readonly_db(wacli_db)
    except wacli_store.DatabaseError:
        return 0, 0
    try:
        n = wacli_messages.count_whatsapp_direct_messages(con, person.phones)
        return n, (1 if n else 0)
    except wacli_store.DatabaseError:
        return 0, 0
    finally:
        con.close()


def resolve_whatsapp_groups(wacli_db: Path, names: list[str], person: Person | None = None) -> list[dict[str, Any]]:
    """Group chats (jid + title) matching CSV ``names`` and/or the person's membership.

    Membership can't use ``group_participants`` (those are privacy ``@lid`` ids with
    no phone mapping in the store). Instead we use the phone-based ``messages.sender_jid``
    — groups the person has actually spoken in. Name matching covers CSV-listed groups."""
    if not wacli_db.exists():
        return []
    try:
        con = wacli_store.open_readonly_db(wacli_db)
    except wacli_store.DatabaseError:
        return []
    try:
        phones = person.phones if person is not None else []
        return wacli_messages.resolve_whatsapp_groups(con, names, phones)
    except wacli_store.DatabaseError:
        return []
    finally:
        con.close()


def whatsapp_target_jids(wacli_db: Path, person: Person, group_names: list[str]) -> list[str]:
    """Existing WhatsApp chat_jids (DMs + groups) relevant to a person, for SCOPED
    `wacli history backfill --chat <jid>` backfill — so we deepen only the conversations
    that matter, not the user's entire WhatsApp."""
    jids: list[str] = [g["jid"] for g in resolve_whatsapp_groups(wacli_db, group_names, person=person)]
    con = None
    if wacli_db.exists():
        try:
            con = wacli_store.open_readonly_db(wacli_db)
        except wacli_store.DatabaseError:
            pass
    if con is not None:
        try:
            jids.extend(wacli_messages.existing_whatsapp_direct_jids(con, person.phones))
        finally:
            con.close()
    return list(dict.fromkeys(jids))


def stream_whatsapp_group(wacli_db: Path, jid: str, title: str, *, since_rowid: int = 0) -> Iterator[dict[str, Any]]:
    if not wacli_db.exists():
        return
    try:
        con = wacli_store.open_readonly_db(wacli_db)
    except wacli_store.DatabaseError:
        return
    try:
        for row in wacli_messages.query_whatsapp_messages(
            con,
            chat_jid=jid,
            since_rowid=since_rowid,
        ):
            text = wacli_messages.whatsapp_message_text(row)
            if not text:
                continue
            at = wacli_store.whatsapp_epoch_to_iso(row["ts"]) or ""
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
