"""The bundle: one candidate's lookup keys and its bounded messages, as stored in `bundles.payload_json`.

`to_payload` fixes the key set and value types; synthesize renders prompts from `from_payload`.

Created: 2026-10-06
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


@dataclass
class Person:
    """A candidate as the message readers take it: its id, name, lookup keys and channels."""

    person_id: str
    full_name: str
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    source_channels: list[str] = field(default_factory=list)


class MessageChannel(StrEnum):
    GMAIL = "gmail"
    IMESSAGE = "imessage"
    IMESSAGE_GROUP = "imessage_group"
    WHATSAPP = "whatsapp"


class MessageDirection(StrEnum):
    FROM_ME = "from_me"
    FROM_THEM = "from_them"
    # Group chats only: a third participant, neither the owner nor this candidate.
    FROM_OTHER = "from_other"

    @classmethod
    def of(cls, from_me: bool) -> MessageDirection:
        if from_me:
            return cls.FROM_ME
        return cls.FROM_THEM

    @classmethod
    def of_group(cls, *, from_me: bool, handle_id: int | None, contact_handle_ids: frozenset[int]) -> MessageDirection:
        """The owner's own messages first, then this candidate's handles, then everyone else."""
        if from_me:
            return cls.FROM_ME
        if handle_id is not None and handle_id in contact_handle_ids:
            return cls.FROM_THEM
        return cls.FROM_OTHER


@dataclass(frozen=True)
class MessageEntry:
    channel: MessageChannel
    at: str  # ISO-8601, or "" when the source row had no timestamp
    direction: MessageDirection
    subject: str
    text: str

    @classmethod
    def of(cls, channel: MessageChannel, at: str, *, from_me: bool, text: str, subject: str = "") -> MessageEntry:
        return cls(channel, at, MessageDirection.of(from_me), subject, text)

    @classmethod
    def from_payload(cls, payload: dict[str, str]) -> MessageEntry:
        return cls(
            MessageChannel(payload["channel"]),
            payload["at"],
            MessageDirection(payload["direction"]),
            payload["subject"],
            payload["text"],
        )

    def to_payload(self) -> dict[str, str]:
        return {
            "channel": self.channel,
            "at": self.at,
            "direction": self.direction,
            "subject": self.subject,
            "text": self.text,
        }

    def content_order_key(self) -> tuple[str, str, str, str, str]:
        """Order by content, so equal timestamps sort the same after a source store is rebuilt."""
        return (self.at, self.channel, self.direction, self.subject, self.text)


@dataclass(frozen=True)
class ThreadParticipants:
    subject: str
    participants: tuple[str, ...]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ThreadParticipants:
        return cls(payload["subject"], tuple(payload["participants"]))

    def to_payload(self) -> dict[str, str | list[str]]:
        return {"subject": self.subject, "participants": list(self.participants)}


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
    ) -> CollectionBundle:
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
    def from_payload(cls, payload: dict[str, Any]) -> CollectionBundle:
        threads = []
        for raw in payload["thread_participants"]:
            threads.append(ThreadParticipants.from_payload(raw))
        messages = []
        for raw in payload["messages"]:
            messages.append(MessageEntry.from_payload(raw))
        return cls(
            person_id=payload["person_id"],
            full_name=payload["full_name"],
            emails=tuple(payload["emails"]),
            phones=tuple(payload["phones"]),
            source_channels=tuple(payload["source_channels"]),
            groups=tuple(payload["groups"]),
            thread_participants=tuple(threads),
            messages=tuple(messages),
            messages_available=payload["messages_available"],
            capped=payload["capped"],
        )

    def to_payload(self) -> dict[str, Any]:
        threads = []
        for thread in self.thread_participants:
            threads.append(thread.to_payload())
        messages = []
        for message in self.messages:
            messages.append(message.to_payload())
        return {
            "person_id": self.person_id,
            "full_name": self.full_name,
            "emails": list(self.emails),
            "phones": list(self.phones),
            "source_channels": list(self.source_channels),
            "groups": list(self.groups),
            "thread_participants": threads,
            "messages": messages,
            "messages_available": self.messages_available,
            "capped": self.capped,
        }
