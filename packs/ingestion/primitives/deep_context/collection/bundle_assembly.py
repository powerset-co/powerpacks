"""Assemble and union parent-owned collection bundle outputs."""

from __future__ import annotations

import json
from typing import Iterable

from packs.ingestion.primitives.deep_context.collection.models import (
    CollectionBundle,
    CollectionPolicy,
    MessageEntry,
    ThreadParticipants,
)
from packs.ingestion.primitives.deep_context.db.models import IsoTimestamp
from packs.ingestion.primitives.deep_context.shared.common import Person


def union_bundles(
    parent_id: str,
    parent_name: str,
    bundles: Iterable[CollectionBundle],
) -> CollectionBundle:
    """Combine cached child bundles without reading a message store."""
    source = tuple(bundles)

    policies = [bundle.policy for bundle in source if bundle.policy is not None]
    policy: CollectionPolicy | None = (
        policies[0] if policies and all(item == policies[0] for item in policies) else None
    )
    messages = _unique_messages(source)
    threads = _unique_threads(source)
    available = sum(bundle.messages_available or len(bundle.messages) for bundle in source)
    return CollectionBundle(
        person_id=parent_id,
        full_name=parent_name or next((bundle.full_name for bundle in source if bundle.full_name), ""),
        emails=_merge_deduplicated_strings(bundle.emails for bundle in source),
        phones=_merge_deduplicated_strings(bundle.phones for bundle in source),
        source_channels=_merge_deduplicated_strings(bundle.source_channels for bundle in source),
        groups=_merge_deduplicated_strings(bundle.groups for bundle in source),
        thread_participants=threads,
        messages=messages,
        messages_available=max(available, len(messages)),
        capped=any(bundle.capped for bundle in source),
        policy=policy,
        collected_at=max(
            (bundle.collected_at for bundle in source if bundle.collected_at),
            default=None,
        ),
    )


def _merge_deduplicated_strings(
    groups: Iterable[Iterable[str]],
) -> tuple[str, ...]:
    """Normalize, deduplicate, and sort string values from several bundles."""
    return tuple(sorted({value.strip() for group in groups for value in group if value.strip()}))


def _unique_messages(source: tuple[CollectionBundle, ...]) -> tuple[MessageEntry, ...]:
    unique: dict[str, MessageEntry] = {}
    for bundle in source:
        for message in bundle.messages:
            key = json.dumps(
                message.to_payload(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            unique.setdefault(key, message)
    return tuple(unique[key] for key in sorted(unique))


def _unique_threads(
    source: tuple[CollectionBundle, ...],
) -> tuple[ThreadParticipants, ...]:
    unique: dict[str, ThreadParticipants] = {}
    for bundle in source:
        for thread in bundle.thread_participants:
            key = json.dumps(
                thread.to_payload(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            unique.setdefault(key, thread)
    return tuple(unique[key] for key in sorted(unique))


def build_bundle(
    person: Person,
    *,
    messages: list[MessageEntry],
    groups: list[str],
    thread_participants: tuple[ThreadParticipants, ...],
    available: int,
    deep_cap: int,
    include_groups: bool,
    max_group_size: int,
    collected_at: IsoTimestamp,
) -> CollectionBundle:
    return CollectionBundle(
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
        policy=CollectionPolicy(
            deep_cap,
            bool(include_groups),
            max_group_size if include_groups else 0,
        ),
        collected_at=collected_at,
    )
