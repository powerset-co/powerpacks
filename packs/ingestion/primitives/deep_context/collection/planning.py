"""Select collection targets and decide cache reuse and privacy-scope purges."""

from __future__ import annotations

from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context.collection.models import (
    CollectionBundle,
    MessageChannel,
)
from packs.ingestion.primitives.deep_context.shared.common import Person
from packs.ingestion.primitives.deep_context.db.models import ArtifactKind
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


def retained_group_policy(bundles: dict[str, CollectionBundle]) -> tuple[int, int]:
    count = max_size = 0
    for bundle in bundles.values():
        groups = [
            message
            for message in bundle.messages
            if message.channel == MessageChannel.IMESSAGE_GROUP
        ]
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
        or any(
            message.channel == MessageChannel.IMESSAGE_GROUP
            for message in bundle.messages
        )
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
