"""Bounded message readers over the shared msgvault, chat.db, and wacli stores.

Gmail deliberately selects for signal, deduplicates, and preserves thread
breadth before depth. Chat sources deliberately apply only a recency cap; they
do not borrow email's scoring policy.

Changelog:
- 2026-08-08: _read_imessage_group_messages now classifies each group row
  against the person's own resolved handle ids (MessageDirection.of_group)
  instead of collapsing every non-owner sender onto FROM_THEM.
"""

from __future__ import annotations

import errno
import sys
import time
from pathlib import Path
from typing import Callable, TypeVar

from packs.ingestion.primitives.deep_context.collection.email_context import EmailContext
from packs.ingestion.primitives.deep_context.collection.models import (
    ChatDbProbe,
    ContextSourcesReadiness,
    MessageChannel,
    MessageDirection,
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
# Bounds characters, not messages — cost downstream is per-character, and this
# stops one heavy correspondent from making a single bundle unbounded.
SAFETY_CHAR_CAP = 1_800_000
DEFAULT_WACLI_DB = Path(".powerpacks/messages/wacli/wacli.db")
QueryResult = TypeVar("QueryResult")
_READ_RETRIES = 3
_RETRY_DELAY_SECONDS = 0.25
_SQLITE_PRIMARY_MASK = 0xFF
_TRANSIENT_SQLITE = {chatdb.sqlite3.SQLITE_BUSY, chatdb.sqlite3.SQLITE_LOCKED, chatdb.sqlite3.SQLITE_IOERR}
_TRANSIENT_IO = {errno.EAGAIN, errno.EBUSY, errno.EINTR, errno.EIO, errno.ETIMEDOUT}


def _read_source(path: Path, read: Callable[[], QueryResult]) -> QueryResult:
    """Retry transient reads; failed reads must never become empty evidence."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return read()
        except (chatdb.DatabaseError, OSError, SystemExit) as exc:
            # Msgvault wraps connection errors in SystemExit; retain its cause.
            cause = exc.__cause__ if isinstance(exc, SystemExit) and exc.__cause__ else exc
            code = getattr(cause, "sqlite_errorcode", 0)
            transient = (
                code & _SQLITE_PRIMARY_MASK in _TRANSIENT_SQLITE
                or isinstance(cause, OSError) and cause.errno in _TRANSIENT_IO
            )
            if not transient or attempt > _READ_RETRIES:
                raise RuntimeError(
                    f"Cannot read {path} after {attempt} attempts: {type(cause).__name__}: {cause}"
                ) from exc
            print(f"[collect] retry {attempt}/{_READ_RETRIES} reading {path}: {cause}", file=sys.stderr, flush=True)
            time.sleep(_RETRY_DELAY_SECONDS * attempt)


def probe_chat_db(chat_db: Path) -> ChatDbProbe:
    """Parse the shared chat.db probe at the external-store boundary."""
    return ChatDbProbe.from_payload(chatdb.probe_message_counts(chat_db))


def apple_epoch_iso(value: object) -> IsoTimestamp:
    """Apple-epoch timestamp rendered as ISO-8601."""
    return chatdb.apple_timestamp_to_iso(value) or ""


class ContextSources:
    """Collect one person's bounded local context with fixed source tuning."""

    def __init__(
        self,
        *,
        store: "gni.MsgvaultStore",
        chat_db: Path,
        wacli_db: Path,
        deep_cap: int,
        max_group_size: int = 25,
    ) -> None:
        self._store = store
        # Populated only by readiness(); stays empty until then (_require_readiness
        # turns a skipped call into a loud failure instead of silent empty accounts).
        self._accounts: set[str] = set()
        self.chat_db = Path(chat_db)
        self.wacli_db = Path(wacli_db)
        self.deep_cap = deep_cap
        self.max_group_size = max_group_size
        self._readiness: ContextSourcesReadiness | None = None
        self.email_context = EmailContext(store)

    def readiness(self, *, people: list[Person] | None = None) -> ContextSourcesReadiness:
        """Open and validate local stores once before any person is collected."""
        if self._readiness is not None:
            return self._readiness
        gmail_available = False
        accounts: set[str] = set()
        needs_gmail = people is None or any(person.emails for person in people)
        if needs_gmail and self._store.db_path.expanduser().exists():
            def read_accounts() -> set[str]:
                self._store.connect()
                self._store.require_schema()
                return self._store.account_emails()

            try:
                accounts.update(_read_source(self._store.db_path, read_accounts))
                gmail_available = True
            except RuntimeError:
                self._store.close()
                raise
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
        rows = _read_source(
            self._store.db_path,
            lambda: self._store.thread_participant_rosters(person.emails, max_threads),
        )
        return tuple(
            thread
            for payload in rows
            if (thread := ThreadParticipants.from_payload(payload)) is not None
        )

    def _require_readiness(self) -> ContextSourcesReadiness:
        """Fail loudly if collection runs before readiness() has run.

        Skipping it leaves ``_accounts`` empty, so every message from the
        owner's own addresses misclassifies as third-party.
        """
        if self._readiness is None:
            raise RuntimeError("ContextSources.readiness() must run before collection")
        return self._readiness

    def _read_gmail(self, person: Person, *, processed: frozenset[str] = frozenset()) -> list[MessageEntry]:
        """Return recent signature-aware email bodies, preserving thread exchanges."""
        seen: set[tuple[str, str]] = set()
        out: list[MessageEntry] = []
        for email in person.emails:
            if processed and len(out) >= self.deep_cap:
                break
            entries, _ = _read_source(
                self._store.db_path,
                lambda: self.email_context.recent_emails_for(
                    email,
                    self.deep_cap - len(out) if processed else self.deep_cap,
                    self._accounts,
                    processed=processed | frozenset(message.fingerprint() for message in out) if processed else processed,
                ),
            )
            for entry in entries:
                text = entry.snippet.strip()
                if not text:
                    continue
                # The outer loop runs once per address this contact owns, so the same
                # message can reach the pool once per address without this dedup.
                key = (entry.subject.lower(), text[:80].lower())
                if not processed and key in seen:
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

    def _count_gmail(self, person: Person) -> int:
        """Count the same uncapped Gmail universe used by ``_read_gmail``."""
        total = 0
        for email in person.emails:
            total += _read_source(
                self._store.db_path,
                lambda: self._store.count_messages_for(email, self._accounts),
            )
        return total

    def _chat_query(
        self,
        person: Person,
        query: Callable[[chatdb.sqlite3.Connection, list[int]], QueryResult],
        empty: QueryResult,
    ) -> QueryResult:
        """Resolve this person's handles once around one shared chat.db query.

        Opens a fresh connection per call (four times per person) — cheap because
        handle resolution is cached on ``cache_key=self.chat_db``, not reopened.
        """
        if not person.phones or not self.chat_db.exists():
            return empty

        def read() -> QueryResult:
            # immutable=True: no lock, no WAL side files against Apple's live store.
            connection = chatdb.open_sqlite_readonly(self.chat_db, immutable=True)
            try:
                handles = chatdb.resolve_handle_ids(connection, person.phones, cache_key=self.chat_db)
                return query(connection, handles) if handles else empty
            finally:
                connection.close()

        return _read_source(self.chat_db, read)

    def _read_imessage(self, person: Person, *, processed: frozenset[str] = frozenset()) -> list[MessageEntry]:
        """DM bodies only — group bodies come from _read_imessage_group_messages."""
        rows = self._chat_query(
            person,
            lambda connection, handles: list(
                chatdb.query_direct_messages(
                    connection,
                    handles,
                    limit=None if processed else self.deep_cap,
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
        if processed:
            return [message for message in out if message.fingerprint() not in processed][:self.deep_cap]
        return out

    def _count_imessage_dms(self, person: Person) -> int:
        """True total of the person's iMessage DMs (so capping is honest)."""
        return int(
            self._chat_query(
                person,
                chatdb.count_direct_messages,
                0,
            )
        )

    def _read_imessage_groups(self, person: Person) -> list[str]:
        """Named iMessage group chats this contact belongs to (names only)."""
        cap = 25
        rows = self._chat_query(
            person,
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

    def _read_imessage_group_messages(self, person: Person, *, processed: frozenset[str] = frozenset()) -> list[MessageEntry]:
        """Bodies from the person's size-capped shared groups; imessage_groups returns names only.

        Each row's sender handle is compared against this person's own resolved
        handles (the same ids that scoped which groups to read at all) so a
        message from a third group participant renders as FROM_OTHER rather
        than being indistinguishable from the contact's own words.
        """
        rows, contact_handle_ids = self._chat_query(
            person,
            lambda connection, handles: (
                list(
                    chatdb.query_small_group_messages(
                        connection,
                        handles,
                        max_group_size=self.max_group_size,
                        limit=None if processed else self.deep_cap,
                    )
                ),
                frozenset(handles),
            ),
            ([], frozenset()),
        )
        out: list[MessageEntry] = []
        for row in rows:
            text = chatdb.message_text(row)
            if not text:
                continue
            group = (row["dn"] or row["rn"] or "group").strip()
            direction = MessageDirection.of_group(
                from_me=bool(row["is_from_me"]),
                handle_id=row["handle_id"],
                contact_handle_ids=contact_handle_ids,
            )
            out.append(
                MessageEntry(
                    channel=MessageChannel.IMESSAGE_GROUP,
                    at=apple_epoch_iso(row["date"]),
                    direction=direction,
                    subject=group,
                    text=text.strip(),
                )
            )
        if processed:
            return [message for message in out if message.fingerprint() not in processed][:self.deep_cap]
        return out

    def _read_whatsapp(self, person: Person, *, processed: frozenset[str] = frozenset()) -> list[MessageEntry]:
        """Recent DM bodies from the schema-tolerant shared wacli reader."""
        if not person.phones or not self.wacli_db.exists():
            return []

        def read() -> list[chatdb.sqlite3.Row]:
            con = wacli_store.open_readonly_db(self.wacli_db)
            try:
                return list(
                    wacli_messages.query_whatsapp_messages(
                        con,
                        phones=person.phones,
                        limit=None if processed else self.deep_cap,
                        newest_first=True,
                    )
                )
            finally:
                con.close()

        rows = _read_source(self.wacli_db, read)
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
        if processed:
            return [message for message in out if message.fingerprint() not in processed][:self.deep_cap]
        return out

    def collect_person(self, person: Person, *, processed: frozenset[str] = frozenset()) -> tuple[list[MessageEntry], int]:
        """Return the bounded cross-source pool and its uncapped available count."""
        readiness = self._require_readiness()
        has_gmail = readiness.gmail_available and bool(person.emails)
        gmail = self._read_gmail(person, processed=processed) if has_gmail else []
        gmail_total = self._count_gmail(person) if has_gmail else 0
        whatsapp = self._read_whatsapp(person, processed=processed) if person.phones else []
        direct = self._read_imessage(person, processed=processed) + whatsapp if person.phones else []
        chat_total = self._count_imessage_dms(person) + len(whatsapp) if person.phones else 0
        group = self._read_imessage_group_messages(person, processed=processed) if person.phones else []

        # Order here decides who wins the cap below (gmail first, since EmailContext
        # already ranked it by signal; direct/group are just newest-first) — a
        # different job from pool.sort() after truncation, which orders for reading.
        ordered = (
            gmail
            # Content makes equal timestamps stable across store rebuilds; rowid cannot.
            + sorted(direct, key=MessageEntry.content_order_key, reverse=True)
            + sorted(group, key=MessageEntry.content_order_key, reverse=True)
        )
        pool: list[MessageEntry] = []
        used = 0
        for message in ordered:
            text = message.text or ""
            if not text:
                continue
            # `pool and` keeps the first message even if it alone exceeds the cap.
            if pool and used + len(text) > SAFETY_CHAR_CAP:
                break
            pool.append(message)
            used += len(text)
        # The serialized bundle is chronological, with exact content breaking ties.
        pool.sort(key=MessageEntry.content_order_key)
        return pool, gmail_total + chat_total + len(group)

    def imessage_groups(self, person: Person) -> list[str]:
        """Group names only, no bodies — bodies are _read_imessage_group_messages."""
        return self._read_imessage_groups(person)
