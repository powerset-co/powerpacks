"""Bounded message readers over the shared msgvault, chat.db, and wacli stores.

Gmail deliberately selects for signal, deduplicates, and preserves thread
breadth before depth. Chat sources deliberately apply only a recency cap; they
do not borrow email's scoring policy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol, TypeVar

from packs.ingestion.primitives.deep_context.collection.email_context import EmailContext
from packs.ingestion.primitives.deep_context.collection.models import (
    ChatDbProbe,
    ContextSourcesReadiness,
    MessageChannel,
    MessageEntry,
    ThreadParticipants,
)
from packs.ingestion.primitives.deep_context.shared.common import Person
from packs.ingestion.primitives.deep_context.db.models import IsoTimestamp
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
QueryResult = TypeVar("QueryResult")


class ChatConnection(Protocol):
    def close(self) -> None: ...


# --- Gmail (msgvault) -------------------------------------------------------


def _read_gmail(
    person: Person,
    email_context: EmailContext,
    accounts: set[str],
    cap: int,
) -> list[MessageEntry]:
    """Return recent signature-aware email bodies, preserving thread exchanges."""
    seen: set[tuple[str, str]] = set()
    out: list[MessageEntry] = []
    for email in person.emails:
        try:
            entries, _ = email_context.recent_emails_for(
                email,
                cap,
                accounts,
                source="body",
                max_per_thread=None,
            )
        except gni.DatabaseError:
            continue
        for entry in entries:
            text = entry.snippet.strip()
            if not text:
                continue
            key = (entry.subject.lower(), text[:80].lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(
                MessageEntry.of(
                    MessageChannel.GMAIL,
                    entry.at,
                    from_me=entry.from_role == "me",
                    text=text,
                    subject=entry.subject,
                )
            )
    return out


def _count_gmail(person: Person, store: "gni.MsgvaultStore", accounts: set[str]) -> int:
    """Count the same uncapped Gmail universe used by ``read_gmail``."""
    total = 0
    for email in person.emails:
        try:
            total += store.count_messages_for(email, accounts)
        except gni.DatabaseError:
            continue
    return total


# --- iMessage (chat.db), DM-only -------------------------------------------


def probe_chat_db(chat_db: Path) -> ChatDbProbe:
    """Parse the shared chat.db probe at the external-store boundary."""
    return ChatDbProbe.from_payload(chatdb.probe_message_counts(chat_db))


def _chat_query(
    person: Person,
    chat_db: Path,
    query: Callable[[ChatConnection, list[int]], QueryResult],
    empty: QueryResult,
) -> QueryResult:
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


def _read_imessage(person: Person, chat_db: Path, cap: int) -> list[MessageEntry]:
    """Recent DM bodies for the person from chat.db (group chats never read)."""
    rows = _chat_query(
        person,
        chat_db,
        lambda connection, handles: list(
            chatdb.query_direct_messages(
                connection,
                handles,
                limit=cap,
                newest_first=True,
            )
        ),
        [],
    )
    out: list[MessageEntry] = []
    for row in rows:
        text = chatdb.message_text(row)
        if not text:
            continue
        out.append(
            MessageEntry.of(
                MessageChannel.IMESSAGE,
                apple_epoch_iso(row["date"]),
                from_me=bool(row["is_from_me"]),
                text=text.strip(),
            )
        )
    return out


def _count_imessage_dms(person: Person, chat_db: Path) -> int:
    """True total of the person's iMessage DMs (so capping is honest)."""
    return int(_chat_query(person, chat_db, chatdb.count_direct_messages, 0))


def _read_imessage_groups(person: Person, chat_db: Path, cap: int = 25) -> list[str]:
    """Named iMessage group chats this contact belongs to (names only, no bodies)."""
    rows = _chat_query(
        person,
        chat_db,
        lambda connection, handles: list(
            chatdb.query_group_chats_for_handles(
                connection,
                handles,
            )
        ),
        [],
    )
    names: list[str] = []
    for row in rows:
        for candidate in (row["dn"], row["rn"]):
            name = (candidate or "").strip()
            if name and name != (row["ci"] or "") and name not in names:
                names.append(name)
    return names[:cap]


def _read_imessage_group_messages(
    person: Person,
    chat_db: Path,
    *,
    max_group_size: int,
    cap: int,
) -> list[MessageEntry]:
    """Opt-in: message bodies from the person's SMALL shared groups (size-capped)."""
    rows = _chat_query(
        person,
        chat_db,
        lambda connection, handles: list(
            chatdb.query_small_group_messages(
                connection,
                handles,
                max_group_size=max_group_size,
                limit=cap,
            )
        ),
        [],
    )
    out: list[MessageEntry] = []
    for row in rows:
        text = chatdb.message_text(row)
        if not text:
            continue
        group = (row["dn"] or row["rn"] or "group").strip()
        out.append(
            MessageEntry.of(
                MessageChannel.IMESSAGE_GROUP,
                apple_epoch_iso(row["date"]),
                from_me=bool(row["is_from_me"]),
                text=text.strip(),
                subject=group,
            )
        )
    return out


def apple_epoch_iso(value: object) -> IsoTimestamp:
    """Apple-epoch timestamp rendered as ISO-8601."""
    return chatdb.apple_timestamp_to_iso(value) or ""


# --- WhatsApp (wacli store), DM-only ---------------------------------------


def _read_whatsapp(person: Person, wacli_db: Path, cap: int) -> list[MessageEntry]:
    """Recent DM bodies from the schema-tolerant shared wacli reader."""
    if not person.phones or not wacli_db.exists():
        return []
    try:
        con = wacli_store.open_readonly_db(wacli_db)
    except wacli_store.DatabaseError:
        return []
    try:
        rows = list(
            wacli_messages.query_whatsapp_messages(
                con,
                phones=person.phones,
                limit=cap,
                newest_first=True,
            )
        )
    except wacli_store.DatabaseError:
        return []
    finally:
        con.close()
    out: list[MessageEntry] = []
    for row in rows:
        text = wacli_messages.whatsapp_message_text(row, include_media=False)
        if not text:
            continue
        out.append(
            MessageEntry.of(
                MessageChannel.WHATSAPP,
                wacli_store.whatsapp_epoch_to_iso(row["ts"]) or "",
                from_me=bool(row["from_me"]),
                text=text,
            )
        )
    return out


class ContextSources:
    """Collect one person's bounded local context with fixed source tuning."""

    def __init__(
        self,
        *,
        store: "gni.MsgvaultStore",
        chat_db: Path,
        wacli_db: Path,
        deep_cap: int,
        include_groups: bool = False,
        max_group_size: int = 25,
    ) -> None:
        self._store = store
        self._accounts: set[str] = set()
        self.chat_db = Path(chat_db)
        self.wacli_db = Path(wacli_db)
        self.deep_cap = deep_cap
        self.include_groups = include_groups
        self.max_group_size = max_group_size
        self._readiness: ContextSourcesReadiness | None = None
        self.email_context = EmailContext(store)

    def readiness(self) -> ContextSourcesReadiness:
        """Open and validate local stores once before any person is collected."""
        if self._readiness is not None:
            return self._readiness
        gmail_available = False
        accounts: set[str] = set()
        if self._store.db_path.expanduser().exists():
            try:
                self._store.connect()
                self._store.require_schema()
                accounts.update(self._store.account_emails())
                gmail_available = True
            except Exception:
                self._store.close()
        self._accounts = accounts
        self._readiness = ContextSourcesReadiness(
            gmail_available=gmail_available,
            gmail_accounts=tuple(sorted(accounts)),
            chat_db=probe_chat_db(self.chat_db),
        )
        return self._readiness

    def close(self) -> None:
        """Close the message store owned by this source collection."""
        self._store.close()

    def thread_participants(
        self,
        person: Person,
        *,
        max_threads: int = 25,
    ) -> tuple[ThreadParticipants, ...]:
        """Return parsed Gmail thread rosters when the prepared store is available."""
        readiness = self._require_readiness()
        if not readiness.gmail_available or not person.emails:
            return ()
        return tuple(
            thread
            for payload in self._store.thread_participant_rosters(
                person.emails,
                max_threads,
            )
            if (thread := ThreadParticipants.from_payload(payload)) is not None
        )

    def _require_readiness(self) -> ContextSourcesReadiness:
        if self._readiness is None:
            raise RuntimeError("ContextSources.readiness() must run before collection")
        return self._readiness

    def collect_person(self, person: Person) -> tuple[list[MessageEntry], int]:
        """Return the bounded cross-source pool and its uncapped available count."""
        readiness = self._require_readiness()
        has_gmail = readiness.gmail_available and bool(person.emails)
        gmail = _read_gmail(person, self.email_context, self._accounts, self.deep_cap) if has_gmail else []
        gmail_total = _count_gmail(person, self._store, self._accounts) if has_gmail else 0
        whatsapp = _read_whatsapp(person, self.wacli_db, self.deep_cap) if person.phones else []
        direct = _read_imessage(person, self.chat_db, self.deep_cap) + whatsapp if person.phones else []
        chat_total = _count_imessage_dms(person, self.chat_db) + len(whatsapp) if person.phones else 0
        group = (
            _read_imessage_group_messages(
                person,
                self.chat_db,
                max_group_size=self.max_group_size,
                cap=self.deep_cap,
            )
            if person.phones and self.include_groups
            else []
        )

        ordered = (
            gmail
            + sorted(direct, key=lambda message: message.at or "", reverse=True)
            + sorted(group, key=lambda message: message.at or "", reverse=True)
        )
        pool: list[MessageEntry] = []
        used = 0
        for message in ordered:
            text = message.text or ""
            if not text:
                continue
            if pool and used + len(text) > SAFETY_CHAR_CAP:
                break
            pool.append(message)
            used += len(text)
        pool.sort(key=lambda message: message.at or "")
        return pool, gmail_total + chat_total + len(group)

    def imessage_groups(self, person: Person) -> list[str]:
        """Return named iMessage groups without reading their bodies."""
        return _read_imessage_groups(person, self.chat_db)
