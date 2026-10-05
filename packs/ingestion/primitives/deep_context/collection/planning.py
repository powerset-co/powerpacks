"""Select original contacts for message collection."""

from __future__ import annotations

from packs.ingestion.primitives.deep_context.shared.common import Person
from packs.ingestion.primitives.deep_context.db.context_queries import collection_sources
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
