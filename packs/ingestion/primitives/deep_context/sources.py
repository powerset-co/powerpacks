"""Bounded message readers over the shared msgvault, chat.db, and wacli stores."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.common import Person

from packs.ingestion.primitives.deep_context import build_email_context as bec
from packs.ingestion.primitives.discover.gmail.msgvault import (  # noqa: F401 - re-exported for collector defaults
    store as gni,
)
from packs.ingestion.primitives.discover.messages import chatdb
from packs.ingestion.primitives.discover.messages.wacli import message_db as wacli_messages
from packs.ingestion.primitives.discover.messages.wacli import store_db as wacli_store

# Each channel keeps up to the same deep cap. Counts remain uncapped.
CHAT_MESSAGE_CAP = 1600
SAFETY_CHAR_CAP = 1_800_000
DEFAULT_WACLI_DB = Path(".powerpacks/messages/wacli/wacli.db")


def _message(
    channel: str, at: str, from_me: bool, text: str, subject: str = "",
) -> dict[str, Any]:
    return {
        "channel": channel, "at": at,
        "direction": "from_me" if from_me else "from_them",
        "subject": subject, "text": text,
    }


# --- Gmail (msgvault) -------------------------------------------------------

def read_gmail(person: Person, store: "gni.MsgvaultStore", accounts: set[str],
               cap: int = CHAT_MESSAGE_CAP) -> list[dict[str, Any]]:
    """Return recent signature-aware email bodies, preserving thread exchanges."""
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
            out.append(_message(
                "gmail", entry.get("at") or "", entry.get("from_role") == "me",
                text, entry.get("subject") or "",
            ))
    return out


def count_gmail(person: Person, store: "gni.MsgvaultStore", accounts: set[str]) -> int:
    """Count the same uncapped Gmail universe used by ``read_gmail``."""
    total = 0
    for email in person.emails:
        try:
            total += store.count_messages_for(email, accounts)
        except gni.DatabaseError:
            continue
    return total


# --- iMessage (chat.db), DM-only -------------------------------------------


def probe_chat_db(chat_db: Path) -> dict[str, Any]:
    return chatdb.probe_message_counts(chat_db)


def _chat_query(person: Person, chat_db: Path, query: Any, empty: Any) -> Any:
    """Resolve this person's handles once around one shared chat.db query."""
    if not person.phones or not chat_db.exists():
        return empty
    try:
        connection = chatdb.open_sqlite_readonly(chat_db, immutable=True)
    except chatdb.DatabaseError:
        return empty
    try:
        handles = chatdb.resolve_handle_ids(connection, person.phones, cache_key=chat_db)
        return query(connection, handles) if handles else empty
    except chatdb.DatabaseError:
        return empty
    finally:
        connection.close()


def read_imessage(person: Person, chat_db: Path, cap: int = CHAT_MESSAGE_CAP) -> list[dict[str, Any]]:
    """Recent DM bodies for the person from chat.db (group chats never read)."""
    rows = _chat_query(
        person, chat_db,
        lambda connection, handles: list(chatdb.query_direct_messages(
            connection, handles, limit=cap, newest_first=True,
        )),
        [],
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        text = chatdb.message_text(row)
        if not text:
            continue
        out.append(_message(
            "imessage", bec_apple_iso(row["date"]), bool(row["is_from_me"]), text.strip(),
        ))
    return out


def count_imessage_dms(person: Person, chat_db: Path) -> int:
    """True total of the person's iMessage DMs (so capping is honest)."""
    return int(_chat_query(person, chat_db, chatdb.count_direct_messages, 0))


def read_imessage_groups(person: Person, chat_db: Path, cap: int = 25) -> list[str]:
    """Named iMessage group chats this contact belongs to (names only, no bodies)."""
    rows = _chat_query(
        person, chat_db,
        lambda connection, handles: list(chatdb.query_group_chats_for_handles(
            connection, handles,
        )),
        [],
    )
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
    rows = _chat_query(
        person, chat_db,
        lambda connection, handles: list(chatdb.query_small_group_messages(
            connection, handles, max_group_size=max_group_size, limit=cap,
        )),
        [],
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        text = chatdb.message_text(row)
        if not text:
            continue
        group = (row["dn"] or row["rn"] or "group").strip()
        out.append(_message(
            "imessage_group", bec_apple_iso(row["date"]), bool(row["is_from_me"]),
            text.strip(), group,
        ))
    return out


def bec_apple_iso(value: Any) -> str:
    """Apple-epoch timestamp rendered as ISO-8601."""
    return chatdb.apple_timestamp_to_iso(value) or ""


# --- WhatsApp (wacli store), DM-only ---------------------------------------

def read_whatsapp(person: Person, wacli_db: Path = DEFAULT_WACLI_DB, cap: int = CHAT_MESSAGE_CAP) -> list[dict[str, Any]]:
    """Recent DM bodies from the schema-tolerant shared wacli reader."""
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
        out.append(_message(
            "whatsapp", wacli_store.whatsapp_epoch_to_iso(row["ts"]) or "",
            bool(row["from_me"]), text,
        ))
    return out


def collect_person(
    person: Person,
    *,
    store: "gni.MsgvaultStore | None",
    accounts: set[str],
    chat_db: Path,
    wacli_db: Path,
    deep_cap: int,
    include_groups: bool = False,
    max_group_size: int = 25,
) -> tuple[list[dict[str, Any]], int]:
    """Return the bounded cross-source pool and its uncapped available count."""
    has_gmail = store is not None and bool(person.emails)
    gmail = read_gmail(person, store, accounts, cap=deep_cap) if has_gmail else []
    gmail_total = count_gmail(person, store, accounts) if has_gmail else 0
    whatsapp = read_whatsapp(person, wacli_db, cap=deep_cap) if person.phones else []
    direct = (
        read_imessage(person, chat_db, cap=deep_cap) + whatsapp if person.phones else []
    )
    chat_total = count_imessage_dms(person, chat_db) + len(whatsapp) if person.phones else 0
    group = (
        read_imessage_group_messages(
            person, chat_db, max_group_size=max_group_size, cap=deep_cap,
        )
        if person.phones and include_groups else []
    )

    ordered = (
        gmail
        + sorted(direct, key=lambda message: message.get("at") or "", reverse=True)
        + sorted(group, key=lambda message: message.get("at") or "", reverse=True)
    )
    pool: list[dict[str, Any]] = []
    used = 0
    for message in ordered:
        text = message.get("text") or ""
        if not text:
            continue
        if pool and used + len(text) > SAFETY_CHAR_CAP:
            break
        pool.append(message)
        used += len(text)
    pool.sort(key=lambda message: message.get("at") or "")
    return pool, gmail_total + chat_total + len(group)
