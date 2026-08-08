"""Select collection targets and summarize projected bundle contents."""

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


def source_parents(db: Db, *, limit: int | None = None) -> list[Person]:
    """Return one message-store lookup subject per canonical parent.

    A `limit` truncates this to a partial view of the corpus, not a filtered
    one; collect_person_context.execute skips its orphan sweep whenever `limit`
    is set, since parents this call never reached are not orphans.
    """
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
    """Parse parent-owned bundle payloads once at the SQLite artifact boundary.

    This is the parse-at-the-boundary point: raw JSON becomes typed
    CollectionBundles here, once, and every caller downstream takes typed
    values. A payload that fails to parse is skipped, not raised.
    """
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


def retained_group_message_count(bundles: dict[str, CollectionBundle]) -> int:
    """Count iMessage group bodies the store still holds, for the manifest's privacy block.

    Describes the store as it now stands, not what this run wrote: `bundles`
    includes parents a `--limit` run never revisited, so their prior group
    bodies are counted too.
    """
    count = 0
    for bundle in bundles.values():
        count += sum(
            1
            for message in bundle.messages
            if message.channel == MessageChannel.IMESSAGE_GROUP
        )
    return count
