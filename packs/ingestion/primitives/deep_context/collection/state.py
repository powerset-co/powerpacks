"""Hydrate collector inputs and resume policy from canonical SQLite projections."""
from __future__ import annotations

import json
from typing import Iterable

from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context.collection.models import (
    CollectionBundle,
    CollectionPolicy,
    MessageEntry,
    ParentSourceIdentifiers,
    ThreadParticipants,
)
from packs.ingestion.primitives.deep_context.common import (
    GMAIL_CHANNEL,
    IMESSAGE_CHANNEL,
    WHATSAPP_CHANNEL,
    Person,
)
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    CanonicalSnapshot,
    IdentifierKind,
    IsoTimestamp,
)


def bundle_matches_policy(
    bundle: CollectionBundle,
    person: Person,
    *,
    deep_cap: int,
    include_groups: bool,
    max_group_size: int,
) -> bool:
    policy = bundle.policy
    return bool(
        policy is not None
        and policy.deep_cap == deep_cap
        and policy.include_groups is bool(include_groups)
        and (not include_groups or policy.max_group_size == max_group_size)
        and set(bundle.emails) == set(person.emails)
        and set(bundle.phones) == set(person.phones)
        and set(bundle.source_channels) == set(person.source_channels)
    )


def source_parents(snapshot: CanonicalSnapshot, *, limit: int | None = None) -> list[Person]:
    """Return one message-store lookup subject per canonical parent."""
    sources: dict[str, list[str]] = {}
    for row in snapshot.sources:
        sources.setdefault(row.person_id, []).append(row.source)
    identifiers: dict[str, dict[str, list[str]]] = {}
    for row in snapshot.identifiers:
        identifiers.setdefault(row.person_id, {}).setdefault(row.kind, []).append(
            row.normalized_value
        )

    parents = {row.parent_id: row for row in snapshot.parents}
    grouped: dict[str, ParentSourceIdentifiers] = {}
    message_channels = {GMAIL_CHANNEL, IMESSAGE_CHANNEL, WHATSAPP_CHANNEL}
    for row in snapshot.people:
        if row.is_owner:
            continue
        channels = sources.get(row.person_id, [])
        values = identifiers.get(row.person_id, {})
        if not message_channels.intersection(channels) or not (
            values.get(IdentifierKind.EMAIL.value) or values.get(IdentifierKind.PHONE.value)
        ):
            continue
        grouped[row.parent_id] = grouped.get(
            row.parent_id, ParentSourceIdentifiers(),
        ).combined(
            emails=values.get(IdentifierKind.EMAIL.value, []),
            phones=values.get(IdentifierKind.PHONE.value, []),
            sources=channels,
        )

    result: list[Person] = []
    for parent_id in sorted(grouped):
        values = grouped[parent_id]
        parent = parents[parent_id]
        result.append(Person(
            parent_id,
            parent.display_name or "",
            emails=sorted(values.emails),
            phones=sorted(values.phones),
            source_channels=sorted(values.sources),
        ))
        if limit and len(result) >= limit:
            break
    return result


def projected_bundles(snapshot: CanonicalSnapshot) -> dict[str, CollectionBundle]:
    """Parse parent-owned bundle payloads once at the SQLite artifact boundary."""
    bundles: dict[str, CollectionBundle] = {}
    for artifact in snapshot.artifacts:
        if (
            artifact.kind != ArtifactKind.SOURCE_BUNDLE.value
            or artifact.status != "projected"
            or artifact.person_id is not None
        ):
            continue
        bundle: CollectionBundle | None = CollectionBundle.from_payload(
            parse_json_object(artifact.payload_json)
        )
        if bundle is not None:
            bundles[artifact.parent_id] = bundle
    return bundles


def union_bundles(
    parent_id: str,
    parent_name: str,
    bundles: Iterable[CollectionBundle],
) -> CollectionBundle:
    """Combine cached child bundles without reading a message store."""
    source = tuple(bundles)

    def strings(field: str) -> list[str]:
        return sorted({
            value.strip()
            for bundle in source
            for value in getattr(bundle, field)
            if value.strip()
        })

    policies = [bundle.policy for bundle in source if bundle.policy is not None]
    policy: CollectionPolicy | None = (
        policies[0]
        if policies and all(item == policies[0] for item in policies)
        else None
    )
    messages = _unique_messages(source)
    threads = _unique_threads(source)
    available = sum(
        bundle.messages_available or len(bundle.messages) for bundle in source
    )
    return CollectionBundle(
        person_id=parent_id,
        full_name=parent_name or next(
            (bundle.full_name for bundle in source if bundle.full_name), ""
        ),
        emails=tuple(strings("emails")),
        phones=tuple(strings("phones")),
        source_channels=tuple(strings("source_channels")),
        groups=tuple(strings("groups")),
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


def retained_group_policy(bundles: dict[str, CollectionBundle]) -> tuple[int, int]:
    count = max_size = 0
    for bundle in bundles.values():
        groups = [message for message in bundle.messages
                  if message.channel == "imessage_group"]
        if groups:
            count += len(groups)
            if bundle.policy is not None:
                max_size = max(max_size, bundle.policy.max_group_size)
    return count, max_size


def purge_group_scope(
    bundles: dict[str, CollectionBundle], *, limited: bool,
) -> set[str]:
    """Refuse a limited run when removing prior group-enabled bundles needs a full pass."""
    unsafe = any(
        bundle.policy is None
        or bundle.policy.include_groups is not False
        or any(message.channel == "imessage_group" for message in bundle.messages)
        for bundle in bundles.values()
    )
    if not unsafe:
        return set()
    if limited:
        raise ValueError(
            "existing raw bundles have group-enabled or legacy privacy scope; "
            "run a full default collection without --limit to rebuild them safely"
        )
    return set(bundles)


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
