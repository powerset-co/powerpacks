"""Typed display receipt for the message-collection stage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Self

from pydantic import Field

from packs.ingestion.primitives.deep_context.db.models import IsoTimestamp
from packs.ingestion.primitives.pipeline.contract import StageManifest


@dataclass(frozen=True)
class EmailMessage:
    """One normalized msgvault row selected for collection."""

    at: IsoTimestamp
    sender: str
    from_role: Literal["contact", "me"]
    subject: str
    snippet: str


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


@dataclass(frozen=True)
class MessageEntry:
    channel: str | None
    at: IsoTimestamp | None
    direction: str | None
    subject: str | None
    text: str | None

    @classmethod
    def from_payload(cls, payload: object) -> Self | None:
        """Parse one file/provider message at the collection boundary."""
        if not isinstance(payload, dict):
            return None
        channel = str(payload.get("channel") or "") if "channel" in payload else None
        text = str(payload.get("text") or "") if "text" in payload else None
        return cls(
            channel=channel,
            at=str(payload.get("at") or "") if "at" in payload else None,
            direction=(str(payload.get("direction") or "") if "direction" in payload else None),
            subject=(str(payload.get("subject") or "") if "subject" in payload else None),
            text=text,
        )

    def to_payload(self) -> dict[str, str]:
        """Project fields in the historical message-key order."""
        payload: dict[str, str] = {}
        if self.channel is not None:
            payload["channel"] = self.channel
        if self.at is not None:
            payload["at"] = self.at
        if self.direction is not None:
            payload["direction"] = self.direction
        if self.subject is not None:
            payload["subject"] = self.subject
        if self.text is not None:
            payload["text"] = self.text
        return payload


@dataclass(frozen=True)
class CollectionPolicy:
    deep_cap: int
    include_groups: bool
    max_group_size: int

    @classmethod
    def from_payload(cls, payload: object) -> Self | None:
        if not isinstance(payload, dict):
            return None
        deep_cap = payload.get("deep_cap")
        include_groups = payload.get("include_groups")
        max_group_size = payload.get("max_group_size")
        if not isinstance(deep_cap, int) or not isinstance(include_groups, bool) or not isinstance(max_group_size, int):
            return None
        return cls(deep_cap, include_groups, max_group_size)

    def to_payload(self) -> dict[str, int | bool]:
        return {
            "deep_cap": self.deep_cap,
            "include_groups": self.include_groups,
            "max_group_size": self.max_group_size,
        }


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
    policy: CollectionPolicy | None
    collected_at: IsoTimestamp | None

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
            policy=CollectionPolicy.from_payload(payload.get("collection_policy")),
            collected_at=(str(payload["collected_at"]) if payload.get("collected_at") else None),
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
        if self.policy is not None:
            payload["collection_policy"] = self.policy.to_payload()
        payload["collected_at"] = self.collected_at or ""
        return payload


class CollectPersonContextManifest(StageManifest):
    source: str = "collect_person_context"
    privacy_schema_version: int = 2
    dry_run: bool = False
    people_total: int = 0
    people_with_context: int = 0
    people_skipped_existing: int = 0
    total_messages_sampled: int = 0
    people_capped: int = 0
    channel_message_counts: dict[str, int] = Field(default_factory=dict)
    contacts_per_sec: float = 0.0
    messages_per_sec: float = 0.0
    ms_per_contact: float | int = 0
    deep_cap_per_person: int = 0
    groups_included: bool = False
    max_group_size: int = 0
    bundles_purged_for_scope: int = 0
    orphan_bundles_removed: int = 0
    msgvault_available: bool = False
    chat_db_available: bool = False
    chat_db_probe: dict[str, Any] = Field(default_factory=dict)
    wacli_available: bool = False
    out_dir: str = ""
    elapsed_ms: int = 0
    updated_at: IsoTimestamp | None = None
    privacy: dict[str, Any] = Field(default_factory=dict)
