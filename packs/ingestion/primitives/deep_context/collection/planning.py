"""Select collection targets and summarize projected bundle contents."""

from __future__ import annotations

from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context.collection.models import CollectionBundle
from packs.ingestion.primitives.deep_context.shared.common import Person
from packs.ingestion.primitives.deep_context.db.models import ArtifactKind
from packs.ingestion.primitives.deep_context.db.context_queries import collection_sources
from packs.ingestion.primitives.deep_context.db.queries import artifacts
from packs.ingestion.primitives.deep_context.db.store import Db


def source_people(db: Db) -> list[Person]:
    """Return one message-store lookup subject per contact."""
    return [
        Person(
            row.person_id,
            row.display_name,
            emails=list(row.emails),
            phones=list(row.phones),
            source_channels=list(row.source_channels),
        )
        for row in collection_sources(db)
    ]


def projected_bundles(db: Db) -> dict[str, CollectionBundle]:
    """Read parent display bundles from projected artifacts."""
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
