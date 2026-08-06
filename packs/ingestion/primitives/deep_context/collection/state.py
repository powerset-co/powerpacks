"""Hydrate collector inputs and resume policy from canonical SQLite projections."""
from __future__ import annotations

import json
from typing import Any

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
)


def bundle_matches_policy(
    bundle: dict[str, Any], *, deep_cap: int, include_groups: bool, max_group_size: int,
) -> bool:
    policy = bundle.get("collection_policy")
    return bool(
        isinstance(policy, dict)
        and policy.get("deep_cap") == deep_cap
        and policy.get("include_groups") is bool(include_groups)
        and (not include_groups or policy.get("max_group_size") == max_group_size)
    )


def source_people(
    snapshot: CanonicalSnapshot, *, limit: int = 0, person_id: str = "",
) -> list[Person]:
    sources: dict[str, list[str]] = {}
    for row in snapshot.sources:
        sources.setdefault(row.person_id, []).append(row.source)
    identifiers: dict[str, dict[str, list[str]]] = {}
    for row in snapshot.identifiers:
        identifiers.setdefault(row.person_id, {}).setdefault(row.kind, []).append(
            row.normalized_value
        )

    people: list[Person] = []
    message_channels = {GMAIL_CHANNEL, IMESSAGE_CHANNEL, WHATSAPP_CHANNEL}
    for row in snapshot.people:
        channels = sources.get(row.person_id, [])
        values = identifiers.get(row.person_id, {})
        if (
            (not person_id or row.person_id == person_id)
            and message_channels.intersection(channels)
            and (values.get(IdentifierKind.EMAIL.value) or values.get(IdentifierKind.PHONE.value))
        ):
            people.append(Person(
                row.person_id, row.display_name or "",
                emails=list(values.get(IdentifierKind.EMAIL.value, [])),
                phones=list(values.get(IdentifierKind.PHONE.value, [])),
                source_channels=list(channels),
            ))
            if limit and len(people) >= limit:
                break
    return people


def projected_bundles(snapshot: CanonicalSnapshot) -> dict[str, dict[str, Any]]:
    bundles: dict[str, dict[str, Any]] = {}
    for artifact in snapshot.artifacts:
        if artifact.kind != ArtifactKind.SOURCE_BUNDLE.value or artifact.status != "projected":
            continue
        try:
            payload = json.loads(artifact.payload_json or "{}")
        except json.JSONDecodeError:
            continue
        if artifact.person_id and isinstance(payload, dict):
            bundles[artifact.person_id] = payload
    return bundles


def retained_group_policy(bundles: dict[str, dict[str, Any]]) -> tuple[int, int]:
    count = max_size = 0
    for bundle in bundles.values():
        groups = [
            message for message in bundle.get("messages") or []
            if isinstance(message, dict) and message.get("channel") == "imessage_group"
        ]
        if groups:
            count += len(groups)
            policy = bundle.get("collection_policy")
            if isinstance(policy, dict) and isinstance(policy.get("max_group_size"), int):
                max_size = max(max_size, policy["max_group_size"])
    return count, max_size


def purge_group_scope(bundles: dict[str, dict[str, Any]], *, partial: bool) -> set[str]:
    unsafe = any(
        not isinstance(bundle.get("collection_policy"), dict)
        or bundle["collection_policy"].get("include_groups") is not False
        or any(message.get("channel") == "imessage_group"
               for message in bundle.get("messages") or [] if isinstance(message, dict))
        for bundle in bundles.values()
    )
    if not unsafe:
        return set()
    if partial:
        raise ValueError(
            "existing raw bundles have group-enabled or legacy privacy scope; "
            "run a full default collection without --person/--limit to rebuild them safely"
        )
    return set(bundles)


def bundle_payload(
    person: Person,
    *,
    messages: list[dict[str, Any]],
    groups: list[str],
    thread_participants: list[dict[str, Any]],
    available: int,
    deep_cap: int,
    include_groups: bool,
    max_group_size: int,
    collected_at: str,
) -> dict[str, Any]:
    return {
        **vars(person),
        "groups": groups,
        "thread_participants": thread_participants,
        "messages": messages,
        "messages_available": available,
        "capped": available > len(messages),
        "collection_policy": {
            "deep_cap": deep_cap,
            "include_groups": bool(include_groups),
            "max_group_size": max_group_size if include_groups else 0,
        },
        "collected_at": collected_at,
    }
