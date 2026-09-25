"""Select collection targets and summarize projected bundle contents."""

from __future__ import annotations

from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context.collection.models import CollectionBundle
from packs.ingestion.primitives.deep_context.shared.common import Person
from packs.ingestion.primitives.deep_context.db.models import ArtifactKind
from packs.ingestion.primitives.deep_context.db.context_queries import collection_sources
from packs.ingestion.primitives.deep_context.db.queries import artifacts
from packs.ingestion.primitives.deep_context.db.store import Db


def source_parents(db: Db) -> list[Person]:
    """Return one message-store lookup subject per canonical parent.

    Selection requires a person_sources row tagged with a message channel
    (see db.context_queries.collection_sources) — a parent with no such row
    is legitimately excluded here while still being a real parent. The
    collection stage's orphan sweep checks parents-table existence (see
    db.context_queries.existing_parent_ids), never this selection, so an
    unselected parent's bundle survives.
    """
    return [
        Person(
            row.parent_id,
            row.display_name,
            emails=list(row.emails),
            phones=list(row.phones),
            source_channels=list(row.source_channels),
        )
        for row in collection_sources(db)
    ]


def projected_bundles(db: Db) -> dict[str, CollectionBundle]:
    """Parse parent-owned bundle payloads once at the SQLite artifact boundary.

    This is the parse-at-the-boundary point: raw JSON becomes typed
    CollectionBundles here, once, and every caller downstream takes typed
    values. A payload that fails to parse is skipped, not raised. Callers
    that only need to know WHICH parents have a bundle (not their message
    bodies) should use db.context_queries.collection_bundle_parent_ids
    instead — this parses every message body of every parent into memory and
    holds it for the caller's lifetime.
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
