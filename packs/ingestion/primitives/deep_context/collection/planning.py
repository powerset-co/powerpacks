"""Plan message collection and assemble its parent-owned bundle outputs.

Selection and resume decisions live here beside bundle union/build operations
because both consume the same projected collection state. Source-store access
stays in ``context_sources`` and artifact migration stays in ``normalization``.
"""

from __future__ import annotations

import json
from typing import Iterable

from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context.collection.models import (
    CollectionBundle,
    CollectionPolicy,
    MessageEntry,
    ThreadParticipants,
)
from packs.ingestion.primitives.deep_context.common import Person
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    IsoTimestamp,
)
from packs.ingestion.primitives.deep_context.db.context_queries import collection_sources
from packs.ingestion.primitives.deep_context.db.queries import artifacts
from packs.ingestion.primitives.deep_context.db.store import Db


def bundle_matches_policy(
    bundle: CollectionBundle,
    person: Person,
    *,
    deep_cap: int,
    include_groups: bool,
    max_group_size: int,
) -> bool:
    policy = bundle.policy
    # A bundle without the current policy contract cannot prove it used today's scope.
    if policy is None:
        return False
    # A changed cap changes the evidence available to synthesis, so reuse would be stale.
    if policy.deep_cap != deep_cap:
        return False
    # Group bodies are privacy-sensitive; reuse only an exact access-scope match.
    if policy.include_groups is not bool(include_groups):
        return False
    # The size ceiling is evidence policy only when group bodies are enabled.
    if include_groups and policy.max_group_size != max_group_size:
        return False
    # Identifier changes can expose a different person's messages under the same parent.
    if set(bundle.emails) != set(person.emails):
        return False
    if set(bundle.phones) != set(person.phones):
        return False
    # Source changes alter which stores are authoritative even when identifiers coincide.
    return set(bundle.source_channels) == set(person.source_channels)


def source_parents(db: Db, *, limit: int | None = None) -> list[Person]:
    """Return one message-store lookup subject per canonical parent."""
    result: list[Person] = []
    for row in collection_sources(db):
        result.append(
            Person(
                row.parent_id,
                row.display_name,
                emails=list(row.emails),
                phones=list(row.phones),
                source_channels=list(row.source_channels),
            )
        )
        if limit and len(result) >= limit:
            break
    return result


def projected_bundles(db: Db) -> dict[str, CollectionBundle]:
    """Parse parent-owned bundle payloads once at the SQLite artifact boundary."""
    bundles: dict[str, CollectionBundle] = {}
    for artifact in artifacts(
        db,
        kind=ArtifactKind.SOURCE_BUNDLE.value,
        status="projected",
        parent_owned=True,
    ):
        bundle: CollectionBundle | None = CollectionBundle.from_payload(parse_json_object(artifact.payload_json))
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


def retained_group_policy(bundles: dict[str, CollectionBundle]) -> tuple[int, int]:
    count = max_size = 0
    for bundle in bundles.values():
        groups = [message for message in bundle.messages if message.channel == "imessage_group"]
        if groups:
            count += len(groups)
            if bundle.policy is not None:
                max_size = max(max_size, bundle.policy.max_group_size)
    return count, max_size


def purge_group_scope(
    bundles: dict[str, CollectionBundle],
    *,
    limited: bool,
) -> set[str]:
    """Refuse a limited run when removing prior group-enabled bundles needs a full pass."""
    # Any legacy or group-enabled bundle may contain bodies the current run forbids.
    unsafe = any(
        bundle.policy is None
        or bundle.policy.include_groups is not False
        or any(message.channel == "imessage_group" for message in bundle.messages)
        for bundle in bundles.values()
    )
    if not unsafe:
        return set()
    # A limited run cannot see every affected parent, so purging would leave mixed scope.
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
