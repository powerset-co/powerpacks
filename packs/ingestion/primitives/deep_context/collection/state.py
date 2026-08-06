"""Hydrate collector inputs and resume policy from canonical SQLite projections."""
from __future__ import annotations

import json
from typing import Any, Iterable

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
    bundle: dict[str, Any],
    person: Person,
    *,
    deep_cap: int,
    include_groups: bool,
    max_group_size: int,
) -> bool:
    policy = bundle.get("collection_policy")
    return bool(
        isinstance(policy, dict)
        and policy.get("deep_cap") == deep_cap
        and policy.get("include_groups") is bool(include_groups)
        and (not include_groups or policy.get("max_group_size") == max_group_size)
        and set(bundle.get("emails") or []) == set(person.emails)
        and set(bundle.get("phones") or []) == set(person.phones)
        and set(bundle.get("source_channels") or []) == set(person.source_channels)
    )


def source_parents(
    snapshot: CanonicalSnapshot, *, limit: int = 0, person_id: str = "",
) -> list[Person]:
    """Return one message-store lookup subject per canonical parent."""
    sources: dict[str, list[str]] = {}
    for row in snapshot.sources:
        sources.setdefault(row.person_id, []).append(row.source)
    identifiers: dict[str, dict[str, list[str]]] = {}
    for row in snapshot.identifiers:
        identifiers.setdefault(row.person_id, {}).setdefault(row.kind, []).append(
            row.normalized_value
        )

    selected_parent = next(
        (row.parent_id for row in snapshot.people if row.person_id == person_id),
        person_id,
    )
    parents = {row.parent_id: row for row in snapshot.parents}
    grouped: dict[str, dict[str, set[str]]] = {}
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
        bucket = grouped.setdefault(
            row.parent_id, {"emails": set(), "phones": set(), "sources": set()},
        )
        bucket["emails"].update(values.get(IdentifierKind.EMAIL.value, []))
        bucket["phones"].update(values.get(IdentifierKind.PHONE.value, []))
        bucket["sources"].update(channels)

    result: list[Person] = []
    for parent_id in sorted(grouped):
        if selected_parent and parent_id != selected_parent:
            continue
        values = grouped[parent_id]
        parent = parents[parent_id]
        result.append(Person(
            parent_id,
            parent.display_name or "",
            emails=sorted(values["emails"]),
            phones=sorted(values["phones"]),
            source_channels=sorted(values["sources"]),
        ))
        if limit and len(result) >= limit:
            break
    return result


def projected_bundles(snapshot: CanonicalSnapshot) -> dict[str, dict[str, Any]]:
    bundles: dict[str, dict[str, Any]] = {}
    for artifact in snapshot.artifacts:
        if artifact.kind != ArtifactKind.SOURCE_BUNDLE.value or artifact.status != "projected":
            continue
        try:
            payload = json.loads(artifact.payload_json or "{}")
        except json.JSONDecodeError:
            continue
        if artifact.person_id is None and isinstance(payload, dict):
            bundles[artifact.parent_id] = payload
    return bundles


def union_bundles(
    parent_id: str,
    parent_name: str,
    bundles: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Combine cached child bundles without reading a message store."""
    source = tuple(bundles)

    def strings(field: str) -> list[str]:
        return sorted({
            str(value).strip()
            for bundle in source
            for value in bundle.get(field) or []
            if str(value).strip()
        })

    def objects(field: str) -> list[dict[str, Any]]:
        unique: dict[str, dict[str, Any]] = {}
        for bundle in source:
            for value in bundle.get(field) or []:
                if isinstance(value, dict):
                    key = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    unique.setdefault(key, value)
        return [unique[key] for key in sorted(unique)]

    policies = [
        bundle.get("collection_policy")
        for bundle in source
        if isinstance(bundle.get("collection_policy"), dict)
    ]
    policy = policies[0] if policies and all(item == policies[0] for item in policies) else None
    messages = objects("messages")
    available = sum(int(bundle.get("messages_available") or len(bundle.get("messages") or [])) for bundle in source)
    payload: dict[str, Any] = {
        "person_id": parent_id,
        "full_name": parent_name or next(
            (str(bundle.get("full_name") or "") for bundle in source if bundle.get("full_name")),
            "",
        ),
        "emails": strings("emails"),
        "phones": strings("phones"),
        "source_channels": strings("source_channels"),
        "groups": strings("groups"),
        "thread_participants": objects("thread_participants"),
        "messages": messages,
        "messages_available": max(available, len(messages)),
        "capped": any(bool(bundle.get("capped")) for bundle in source),
        "collected_at": max(
            (str(bundle.get("collected_at") or "") for bundle in source), default="",
        ),
    }
    if policy is not None:
        payload["collection_policy"] = policy
    return payload


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
