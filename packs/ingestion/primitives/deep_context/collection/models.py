"""Collection-stage domain records: messages, threads, and the per-parent bundle.

CollectionBundle.to_payload/from_payload pin the bundle's exact JSON shape —
synthesis fingerprints these serialized bytes as a paid-cache key
(input_evidence_fingerprint), so key set, key order, and value types here are
load-bearing, not incidental. CollectionBundle.union is the merge policy for
combining cached per-child bundles into one parent bundle without re-reading
a message store.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable, Literal, Mapping, Protocol, Self, TypeVar

from packs.ingestion.primitives.deep_context.db.models import IsoTimestamp
from packs.ingestion.primitives.deep_context.shared.common import Person


@dataclass(frozen=True)
class EmailMessage:
    """One normalized msgvault row selected for collection."""

    at: IsoTimestamp  # "" when the source row had no timestamp, never None; sorts and renders as-is.
    sender: str
    from_role: Literal["contact", "me"]
    subject: str
    snippet: str  # Misnomer: the cleaned message body; only Gmail's preview when the body cleans to empty.


@dataclass(frozen=True)
class EmailRankedMessage:
    """Email plus the exact signal/contact/recency ordering key."""

    rank: tuple[int, int, IsoTimestamp]
    message: EmailMessage


@dataclass(frozen=True)
class ChatDbProbe:
    """Typed result of the external Apple Messages store readiness probe."""

    exists: bool
    readable: bool
    messages: int
    handles: int
    error: str | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> Self:
        error: object = payload.get("error")
        return cls(
            exists=bool(payload.get("exists")),
            readable=bool(payload.get("readable")),
            messages=int(payload.get("messages") or 0),
            handles=int(payload.get("handles") or 0),
            error=error if isinstance(error, str) else None,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "exists": self.exists,
            "readable": self.readable,
            "messages": self.messages,
            "handles": self.handles,
            "error": self.error,
        }

    @property
    def status(self) -> str:
        if self.readable:
            return "ok"
        return "missing" if not self.exists else "unreadable_full_disk_access"


@dataclass(frozen=True)
class ContextSourcesReadiness:
    """Source availability established before any person is collected."""

    gmail_available: bool
    gmail_accounts: tuple[str, ...]
    chat_db: ChatDbProbe


class MessageChannel(StrEnum):
    """Load-bearing channel values persisted in source-bundle message rows."""

    # Real-store census: gmail 17,111; imessage 11,099; imessage_group 19,084;
    # whatsapp 2,574. Gmail deliberately differs from SourceChannel.GMAIL because
    # message channels are rendered into synthesis prompts and fingerprinted.
    GMAIL = "gmail"
    IMESSAGE = "imessage"
    IMESSAGE_GROUP = "imessage_group"
    WHATSAPP = "whatsapp"


class MessageDirection(StrEnum):
    FROM_ME = "from_me"
    FROM_THEM = "from_them"

    @classmethod
    def of(cls, from_me: bool) -> "MessageDirection":
        return cls.FROM_ME if from_me else cls.FROM_THEM


@dataclass(frozen=True)
class MessageEntry:
    channel: MessageChannel
    at: IsoTimestamp
    direction: MessageDirection
    subject: str
    text: str

    @classmethod
    def of(
        cls,
        channel: MessageChannel,
        at: IsoTimestamp,
        *,
        from_me: bool,
        text: str,
        subject: str = "",
    ) -> "MessageEntry":
        return cls(channel, at, MessageDirection.of(from_me), subject, text)

    @classmethod
    def from_payload(cls, payload: object) -> Self | None:
        """Parse one file/provider message at the collection boundary.

        Channel, direction, and text are required pipeline invariants.
        Missing timestamps and subjects are external variance normalized to "".
        """
        if not isinstance(payload, dict):
            return None
        channel_value = payload.get("channel")
        at_value = payload.get("at")
        if at_value is None:
            at = ""
        elif isinstance(at_value, str):
            at = at_value
        else:
            return None
        direction_value = payload.get("direction")
        subject_value = payload.get("subject")
        if subject_value is None:
            subject = ""
        elif isinstance(subject_value, str):
            subject = subject_value
        else:
            return None
        text = payload.get("text")
        if not (
            isinstance(channel_value, str)
            and channel_value
            and isinstance(at, str)
            and isinstance(direction_value, str)
            and direction_value
            and isinstance(subject, str)
            and isinstance(text, str)
            and text
        ):
            return None
        try:
            channel = MessageChannel(channel_value)
            direction = MessageDirection(direction_value)
        except ValueError:
            return None
        return cls(channel, at, direction, subject, text)

    def to_payload(self) -> dict[str, str]:
        """Project fields in the historical message-key order."""
        return {
            "channel": self.channel,
            "at": self.at,
            "direction": self.direction,
            "subject": self.subject,
            "text": self.text,
        }

    def content_order_key(self) -> tuple[str, str, str, str, str]:
        """Order by the exact persisted content; identical keys serialize alike."""
        return (
            self.at,
            self.channel,
            self.direction,
            self.subject,
            self.text,
        )


@dataclass(frozen=True)
class ThreadParticipants:
    subject: str
    participants: tuple[str, ...]

    @classmethod
    def from_payload(cls, payload: object) -> Self | None:
        if not isinstance(payload, dict):
            return None
        participants = payload.get("participants")
        if not isinstance(participants, list):
            return None
        return cls(
            subject=str(payload.get("subject") or ""),
            participants=tuple(str(value) for value in participants if str(value)),
        )

    def to_payload(self) -> dict[str, str | list[str]]:
        return {"subject": self.subject, "participants": list(self.participants)}


class _PayloadRow(Protocol):
    """Anything with a to_payload() usable as a canonical dedup key."""

    def to_payload(self) -> Mapping[str, object]: ...


_RowT = TypeVar("_RowT", bound=_PayloadRow)


def _dedupe_by_payload(rows: Iterable[_RowT]) -> tuple[_RowT, ...]:
    """Dedupe rows by canonical JSON payload key; return sorted-by-key tuple.

    One algorithm shared by CollectionBundle.union for both messages and
    thread_participants — same dedup/sort shape, different row type.

    Result order is the sorted JSON key: total and reproducible, but NOT
    MessageEntry.content_order_key's order — a bundle built by union can
    therefore tie-break same-`at` messages differently than the same messages
    collected fresh. batches() (prompting.py) sorts stably on `at` alone, so
    that tie-break difference reaches the rendered prompt and the paid-cache
    input_evidence_fingerprint.
    """
    unique: dict[str, _RowT] = {}
    for row in rows:
        key = json.dumps(row.to_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        unique.setdefault(key, row)
    return tuple(unique[key] for key in sorted(unique))


def _merge_deduplicated_strings(groups: Iterable[Iterable[str]]) -> tuple[str, ...]:
    """Normalize, deduplicate, and sort string values from several bundles."""
    return tuple(sorted({value.strip() for group in groups for value in group if value.strip()}))


@dataclass(frozen=True)
class CollectionBundle:
    person_id: str
    full_name: str
    emails: tuple[str, ...]
    phones: tuple[str, ...]
    source_channels: tuple[str, ...]
    groups: tuple[str, ...]
    thread_participants: tuple[ThreadParticipants, ...]
    messages: tuple[MessageEntry, ...]
    messages_available: int
    capped: bool

    @classmethod
    def of(
        cls,
        person: Person,
        *,
        messages: list[MessageEntry],
        groups: list[str],
        thread_participants: tuple[ThreadParticipants, ...],
        available: int,
    ) -> Self:
        """Assemble one freshly sampled per-person bundle from source reads."""
        return cls(
            person_id=person.person_id,
            full_name=person.full_name,
            emails=tuple(person.emails),
            phones=tuple(person.phones),
            source_channels=tuple(person.source_channels),
            groups=tuple(groups),
            thread_participants=thread_participants,
            messages=tuple(messages),
            messages_available=available,
            capped=available > len(messages),
        )

    @classmethod
    def union(
        cls,
        parent_id: str,
        parent_name: str,
        bundles: Iterable[CollectionBundle],
    ) -> Self:
        """Combine cached child bundles without reading a message store."""
        source = tuple(bundles)
        messages = _dedupe_by_payload(message for bundle in source for message in bundle.messages)
        threads = _dedupe_by_payload(thread for bundle in source for thread in bundle.thread_participants)
        available = sum(bundle.messages_available or len(bundle.messages) for bundle in source)
        return cls(
            person_id=parent_id,
            # Parent's own name wins: children are legacy per-child bundles whose
            # cached name can be stale or narrower than the current parent identity.
            full_name=parent_name or next((bundle.full_name for bundle in source if bundle.full_name), ""),
            emails=_merge_deduplicated_strings(bundle.emails for bundle in source),
            phones=_merge_deduplicated_strings(bundle.phones for bundle in source),
            source_channels=_merge_deduplicated_strings(bundle.source_channels for bundle in source),
            groups=_merge_deduplicated_strings(bundle.groups for bundle in source),
            thread_participants=threads,
            messages=messages,
            # `available` sums each child's own count, but dedup runs across children,
            # so the two can disagree; max keeps available >= what's actually carried.
            messages_available=max(available, len(messages)),
            capped=any(bundle.capped for bundle in source),
        )

    @classmethod
    def from_payload(cls, payload: object) -> Self | None:
        """Parse the SQLite/file payload once before collection business logic."""
        if not isinstance(payload, dict):
            return None

        def strings(field: str) -> tuple[str, ...]:
            values = payload.get(field)
            return tuple(str(value) for value in values if str(value)) if isinstance(values, list) else ()

        messages = tuple(
            message for raw in payload.get("messages") or [] if (message := MessageEntry.from_payload(raw)) is not None
        )
        threads = tuple(
            thread
            for raw in payload.get("thread_participants") or []
            if (thread := ThreadParticipants.from_payload(raw)) is not None
        )
        available = payload.get("messages_available")
        return cls(
            person_id=str(payload.get("person_id") or ""),
            full_name=str(payload.get("full_name") or ""),
            emails=strings("emails"),
            phones=strings("phones"),
            source_channels=strings("source_channels"),
            groups=strings("groups"),
            thread_participants=threads,
            messages=messages,
            messages_available=(int(available) if available not in (None, "") else len(messages)),
            capped=bool(payload.get("capped")),
        )

    def to_payload(self) -> dict[str, Any]:
        """Project the historical ordered JSON shape; this order is pinned."""
        # Synthesis fingerprints these serialized bytes; key/order drift re-bills every parent.
        payload: dict[str, Any] = {
            "person_id": self.person_id,
            "full_name": self.full_name,
            "emails": list(self.emails),
            "phones": list(self.phones),
            "source_channels": list(self.source_channels),
            "groups": list(self.groups),
            "thread_participants": [row.to_payload() for row in self.thread_participants],
            "messages": [row.to_payload() for row in self.messages],
            "messages_available": self.messages_available,
            "capped": self.capped,
        }
        return payload
