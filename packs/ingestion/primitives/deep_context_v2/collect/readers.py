"""Bounded message reads for one candidate across msgvault (Gmail), chat.db (iMessage) and wacli.db (WhatsApp).

Gmail is selected for signal (see gmail.py); chat channels keep only the newest `deep_cap`
messages. The pool is Gmail first, then direct chats, then group chats, cut at a character
cap, then sorted chronologically.

Created: 2026-10-06
"""
from __future__ import annotations

import errno
import sqlite3
import sys
import time
from pathlib import Path
from typing import Callable, TypeVar

from packs.ingestion.primitives.deep_context_v2.collect import gmail
from packs.ingestion.primitives.deep_context_v2.collect.bundle import (
    MessageChannel,
    MessageDirection,
    MessageEntry,
    Person,
    ThreadParticipants,
)
from packs.ingestion.primitives.discover.messages import chatdb
from packs.ingestion.primitives.discover.messages.wacli import message_db as wacli_messages
from packs.ingestion.primitives.discover.messages.wacli import store_db as wacli_store

# Each channel keeps up to this many messages; the available count stays uncapped.
CHAT_MESSAGE_CAP = 1600
# Bounds characters, not messages: one heavy correspondent cannot make a bundle unbounded.
SAFETY_CHAR_CAP = 1_800_000
WACLI_DB_RELATIVE = Path("messages") / "wacli" / "wacli.db"
MAX_THREADS = 25
QueryResult = TypeVar("QueryResult")
_READ_RETRIES = 3
_RETRY_DELAY_SECONDS = 0.25
_SQLITE_PRIMARY_MASK = 0xFF
_TRANSIENT_SQLITE = {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_IOERR}
_TRANSIENT_IO = {errno.EAGAIN, errno.EBUSY, errno.EINTR, errno.EIO, errno.ETIMEDOUT}


def _read_source(path: Path, read: Callable[[], QueryResult]) -> QueryResult:
    """Retry transient read errors; any other failure fails the run, never becomes empty evidence."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return read()
        except (sqlite3.Error, OSError) as exc:
            code = getattr(exc, "sqlite_errorcode", 0)
            transient = code & _SQLITE_PRIMARY_MASK in _TRANSIENT_SQLITE
            if isinstance(exc, OSError) and exc.errno in _TRANSIENT_IO:
                transient = True
            if not transient or attempt > _READ_RETRIES:
                raise RuntimeError(f"Cannot read {path} after {attempt} attempts: {type(exc).__name__}: {exc}") from exc
            print(f"[collect] retry {attempt}/{_READ_RETRIES} reading {path}: {exc}", file=sys.stderr, flush=True)
            time.sleep(_RETRY_DELAY_SECONDS * attempt)


def apple_epoch_iso(value: object) -> str:
    return chatdb.apple_timestamp_to_iso(value) or ""


class ContextSources:
    """One run's message stores and fixed tuning.

    A store is read only for candidates whose channels came from it, so a user who never linked
    a channel never has that store opened. `channels` is the set across the run's candidates. When
    Gmail is among them the archive is opened once here and the owner's own mailbox addresses are
    read from it: they decide a message's direction (from the owner, or from the candidate).
    iMessage and WhatsApp are opened per read.
    """

    def __init__(self, *, channels: set[str], msgvault_db: Path, chat_db: Path, wacli_db: Path,
                 deep_cap: int, max_group_size: int) -> None:
        self.msgvault_db = msgvault_db
        self.chat_db = chat_db
        self.wacli_db = wacli_db
        self.deep_cap = deep_cap
        self.max_group_size = max_group_size
        self.msgvault: sqlite3.Connection | None = None
        self.accounts: set[str] = set()
        if "gmail_msgvault" in channels:
            self.msgvault = gmail.open_msgvault(msgvault_db)
            self.accounts = _read_source(msgvault_db, lambda: gmail.account_emails(self.msgvault))

    def close(self) -> None:
        if self.msgvault is not None:
            self.msgvault.close()

    def thread_participants(self, person: Person) -> tuple[ThreadParticipants, ...]:
        if "gmail_msgvault" not in person.source_channels:
            return ()
        rows = _read_source(
            self.msgvault_db, lambda: gmail.thread_participant_rosters(self.msgvault, person.emails, MAX_THREADS)
        )
        threads = []
        for row in rows:
            threads.append(ThreadParticipants.from_payload(row))
        return tuple(threads)

    def _read_gmail(self, person: Person) -> list[MessageEntry]:
        """Signature-aware email bodies per address, deduplicated across the candidate's addresses."""
        out: list[MessageEntry] = []
        for email in person.emails:
            entries = _read_source(
                self.msgvault_db,
                lambda: gmail.recent_emails_for(self.msgvault, email, self.deep_cap, self.accounts),
            )
            for entry in entries:
                text = entry.snippet.strip()
                if not text:
                    continue
                out.append(
                    MessageEntry.of(
                        MessageChannel.GMAIL, entry.at, from_me=entry.from_role == "me", text=text, subject=entry.subject
                    )
                )
        return out

    def _count_gmail(self, person: Person) -> int:
        total = 0
        for email in person.emails:
            total += _read_source(
                self.msgvault_db, lambda: gmail.count_messages_for(self.msgvault, email, self.accounts)
            )
        return total

    def _chat_query(
        self,
        person: Person,
        query: Callable[[sqlite3.Connection, list[int]], QueryResult],
        empty: QueryResult,
    ) -> QueryResult:
        """Resolve the candidate's chat.db handles, then run one query; no handles, `empty`."""
        if "imessage" not in person.source_channels:
            return empty

        def read() -> QueryResult:
            # immutable=True: no lock and no WAL side files against Apple's live store.
            connection = chatdb.open_sqlite_readonly(self.chat_db, immutable=True)
            try:
                handles = chatdb.resolve_handle_ids(connection, person.phones, cache_key=self.chat_db)
                if not handles:
                    return empty
                return query(connection, handles)
            finally:
                connection.close()

        return _read_source(self.chat_db, read)

    def _read_imessage(self, person: Person) -> list[MessageEntry]:
        """Direct-message bodies; group bodies come from `_read_imessage_group_messages`."""
        rows = self._chat_query(
            person,
            lambda connection, handles: list(
                chatdb.query_direct_messages(connection, handles, limit=self.deep_cap, newest_first=True)
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
                    MessageChannel.IMESSAGE, apple_epoch_iso(row["date"]), from_me=bool(row["is_from_me"]), text=text.strip()
                )
            )
        return out

    def _count_imessage_dms(self, person: Person) -> int:
        return int(self._chat_query(person, chatdb.count_direct_messages, 0))

    def _read_imessage_groups(self, person: Person) -> list[str]:
        """Names of the named iMessage group chats the candidate belongs to."""
        rows = self._chat_query(
            person,
            lambda connection, handles: list(chatdb.query_group_chats_for_handles(connection, handles)),
            [],
        )
        names: list[str] = []
        for row in rows:
            for candidate in (row["dn"], row["rn"]):
                name = (candidate or "").strip()
                if name and name != (row["ci"] or "") and name not in names:
                    names.append(name)
        return names

    def _read_imessage_group_messages(self, person: Person) -> list[MessageEntry]:
        """Bodies from the candidate's small groups; each sender is the owner, the candidate, or another member."""

        def query(connection: sqlite3.Connection, handles: list[int]) -> tuple[list[sqlite3.Row], frozenset[int]]:
            rows = list(
                chatdb.query_small_group_messages(
                    connection, handles, max_group_size=self.max_group_size, limit=self.deep_cap
                )
            )
            return rows, frozenset(handles)

        rows, contact_handle_ids = self._chat_query(person, query, ([], frozenset()))
        out: list[MessageEntry] = []
        for row in rows:
            text = chatdb.message_text(row)
            if not text:
                continue
            group = (row["dn"] or row["rn"] or "group").strip()
            direction = MessageDirection.of_group(
                from_me=bool(row["is_from_me"]), handle_id=row["handle_id"], contact_handle_ids=contact_handle_ids
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
        return out

    def _read_whatsapp(self, person: Person) -> list[MessageEntry]:
        """Newest WhatsApp direct-message bodies."""

        def read() -> list[sqlite3.Row]:
            con = wacli_store.open_readonly_db(self.wacli_db)
            try:
                return list(
                    wacli_messages.query_whatsapp_messages(
                        con, phones=person.phones, limit=self.deep_cap, newest_first=True
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
        return out

    def collect_person(self, person: Person) -> tuple[list[MessageEntry], int]:
        """The bounded cross-source pool, chronological, and the uncapped available count."""
        gmail_messages: list[MessageEntry] = []
        gmail_total = 0
        if "gmail_msgvault" in person.source_channels:
            gmail_messages = self._read_gmail(person)
            gmail_total = self._count_gmail(person)
        direct: list[MessageEntry] = []
        group: list[MessageEntry] = []
        chat_total = 0
        whatsapp: list[MessageEntry] = []
        if "whatsapp" in person.source_channels:
            whatsapp = self._read_whatsapp(person)
        if "imessage" in person.source_channels:
            direct = self._read_imessage(person)
            chat_total = self._count_imessage_dms(person)
            group = self._read_imessage_group_messages(person)
        direct = direct + whatsapp
        chat_total = chat_total + len(whatsapp)

        # This order decides who wins the character cap: Gmail first (already ranked by signal),
        # then direct and group chats newest first, with content breaking equal timestamps.
        ordered = list(gmail_messages)
        ordered.extend(sorted(direct, key=MessageEntry.content_order_key, reverse=True))
        ordered.extend(sorted(group, key=MessageEntry.content_order_key, reverse=True))
        pool: list[MessageEntry] = []
        used = 0
        for message in ordered:
            text = message.text or ""
            if not text:
                continue
            # `pool and` keeps the first message even when it alone exceeds the cap.
            if pool and used + len(text) > SAFETY_CHAR_CAP:
                break
            pool.append(message)
            used += len(text)
        pool.sort(key=MessageEntry.content_order_key)
        return pool, gmail_total + chat_total + len(group)

    def imessage_groups(self, person: Person) -> list[str]:
        return self._read_imessage_groups(person)
