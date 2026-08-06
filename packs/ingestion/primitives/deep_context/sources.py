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

Changelog:
  2026-08-05: Apple and WhatsApp store mechanics use their shared discovery
    readers; this module owns only Deep Context's normalized output policy.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.common import Person

# Reuse the Gmail email-context machinery (msgvault connect/schema + the
# signature-aware body selection) exactly as the marker flow does.
from packs.ingestion.primitives.deep_context import build_email_context as bec
from packs.ingestion.primitives.discover.gmail.msgvault import (  # noqa: F401 - re-exported for collector defaults
    store as gni,
)
from packs.ingestion.primitives.discover.messages import chatdb
from packs.ingestion.primitives.discover.messages.chatdb import (
    decode_attributed_body,  # noqa: F401 - preserve this module's public helper
)
from packs.ingestion.primitives.discover.messages.wacli import message_db as wacli_messages
from packs.ingestion.primitives.discover.messages.wacli import store_db as wacli_store

# Every channel is its own vertical with the same deep cap: Gmail, iMessage, and
# WhatsApp each pool up to CHAT_MESSAGE_CAP recent messages, and the incremental
# synthesizer decides how far back to actually grok. Gmail used to collapse to one
# message per thread (~20 threads), which starved thin contacts with a single rich
# thread — now it keeps the back-and-forth like the chat channels. `count_*` report
# the TRUE total so capping is honest (not hidden behind the LIMIT).
CHAT_MESSAGE_CAP = 1600
DEFAULT_WACLI_DB = Path(".powerpacks/messages/wacli/wacli.db")


# --- Gmail (msgvault) -------------------------------------------------------

def read_gmail(person: Person, store: "gni.MsgvaultStore", accounts: set[str],
               cap: int = CHAT_MESSAGE_CAP) -> list[dict[str, Any]]:
    """Recent, signature-aware email bodies for the person — the whole back-and-forth.

    Queries each of the person's emails through ``build_email_context.recent_emails_for``
    (backed by ``store``) and merges the selected entries (the contact's own +
    owner-directed messages). ``max_per_thread=None`` keeps every message in a thread
    (not just the signal-densest one), so a single rich thread is no longer reduced to
    one line; the per-person ``cap`` bounds the total."""
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for email in person.emails:
        try:
            entries, _ = bec.recent_emails_for(
                store, email, cap, bec.DEFAULT_SNIPPET_CHARS, accounts,
                source="body", max_per_thread=None,
            )
        except gni.DatabaseError:
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


def count_gmail(person: Person, store: "gni.MsgvaultStore", accounts: set[str]) -> int:
    """True total of the person's poolable Gmail messages (so capping is honest), mirroring
    ``count_imessage_dms``. Counts the same universe ``read_gmail`` draws from — the contact's
    own + owner-directed messages — across all of the person's email addresses."""
    total = 0
    for email in person.emails:
        try:
            total += store.count_messages_for(email, accounts)
        except gni.DatabaseError:
            continue
    return total


def gmail_thread_participants(person: Person, store: "gni.MsgvaultStore", max_threads: int = 25) -> list[dict[str, Any]]:
    """Per-thread participant rosters (full from/to/cc as ``Name <email>``) for the person's email
    threads. Surfaces co-recipients we'd otherwise drop — shared colleagues, the team, and the
    OWNER's own address CC'd next to a same-named contact (the owner-alias signal)."""
    return store.thread_participant_rosters(person.emails, max_threads)


# --- iMessage (chat.db), DM-only -------------------------------------------


def probe_chat_db(chat_db: Path) -> dict[str, Any]:
    """Return the established Deep Context readability/count probe."""
    return chatdb.probe_message_counts(chat_db)


def read_imessage(person: Person, chat_db: Path, cap: int = CHAT_MESSAGE_CAP) -> list[dict[str, Any]]:
    """Recent DM bodies for the person from chat.db (group chats never read)."""
    if not person.phones or not chat_db.exists():
        return []
    try:
        con = chatdb.open_sqlite_readonly(chat_db, immutable=True)
    except chatdb.DatabaseError:
        return []
    try:
        handle_ids = chatdb.resolve_handle_ids(con, person.phones, cache_key=chat_db)
        if not handle_ids:
            return []
        rows = list(chatdb.query_direct_messages(
            con, handle_ids, limit=cap, newest_first=True,
        ))
    except chatdb.DatabaseError:
        return []
    finally:
        con.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        text = chatdb.message_text(row)
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


def count_imessage_dms(person: Person, chat_db: Path) -> int:
    """True total of the person's iMessage DMs (so capping is honest)."""
    if not person.phones or not chat_db.exists():
        return 0
    try:
        con = chatdb.open_sqlite_readonly(chat_db, immutable=True)
    except chatdb.DatabaseError:
        return 0
    try:
        handle_ids = chatdb.resolve_handle_ids(con, person.phones, cache_key=chat_db)
        return chatdb.count_direct_messages(con, handle_ids)
    except chatdb.DatabaseError:
        return 0
    finally:
        con.close()


def read_imessage_groups(person: Person, chat_db: Path, cap: int = 25) -> list[str]:
    """Named iMessage group chats this contact belongs to (names only, no bodies)."""
    if not person.phones or not chat_db.exists():
        return []
    try:
        con = chatdb.open_sqlite_readonly(chat_db, immutable=True)
    except chatdb.DatabaseError:
        return []
    try:
        handle_ids = chatdb.resolve_handle_ids(con, person.phones, cache_key=chat_db)
        if not handle_ids:
            return []
        rows = list(chatdb.query_group_chats_for_handles(con, handle_ids))
    except chatdb.DatabaseError:
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


def read_imessage_group_messages(person: Person, chat_db: Path, *, max_group_size: int = 25,
                                 cap: int = CHAT_MESSAGE_CAP) -> list[dict[str, Any]]:
    """Opt-in: message bodies from the person's SMALL shared groups (size-capped)."""
    if not person.phones or not chat_db.exists():
        return []
    try:
        con = chatdb.open_sqlite_readonly(chat_db, immutable=True)
    except wacli_store.DatabaseError:
        return []
    try:
        handle_ids = chatdb.resolve_handle_ids(con, person.phones, cache_key=chat_db)
        if not handle_ids:
            return []
        rows = list(chatdb.query_small_group_messages(
            con, handle_ids, max_group_size=max_group_size, limit=cap,
        ))
    except wacli_store.DatabaseError:
        return []
    finally:
        con.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        text = chatdb.message_text(row)
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
    """Apple-epoch timestamp rendered as ISO-8601."""
    return chatdb.apple_timestamp_to_iso(value) or ""


# --- WhatsApp (wacli store), DM-only ---------------------------------------

def read_whatsapp(person: Person, wacli_db: Path = DEFAULT_WACLI_DB, cap: int = CHAT_MESSAGE_CAP) -> list[dict[str, Any]]:
    """Recent DM bodies for the person from the wacli store (groups never read).

    Defensive about schema: the wacli store may be absent or shaped slightly
    differently across versions, so column presence is checked before querying."""
    if not person.phones or not wacli_db.exists():
        return []
    try:
        con = wacli_store.open_readonly_db(wacli_db)
    except wacli_store.DatabaseError:
        return []
    try:
        rows = list(wacli_messages.query_whatsapp_messages(
            con, phones=person.phones, limit=cap, newest_first=True,
        ))
    except wacli_store.DatabaseError:
        return []
    finally:
        con.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        text = wacli_messages.whatsapp_message_text(row, include_media=False)
        if not text:
            continue
        out.append({
            "channel": "whatsapp",
            "at": wacli_store.whatsapp_epoch_to_iso(row["ts"]) or "",
            "direction": "from_me" if row["from_me"] else "from_them",
            "subject": "",
            "text": text,
        })
    return out


# --- shared signal ranking for adaptive sampling ---------------------------

def signal_rank(message: dict[str, Any]) -> tuple[int, str]:
    """Rank a chat/email message for adaptive keep: identity signal, then recency."""
    return (bec.signal_score(message.get("text", "")), message.get("at") or "")
