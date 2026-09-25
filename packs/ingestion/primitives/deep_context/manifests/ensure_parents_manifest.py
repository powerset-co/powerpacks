"""Manifest emitted by imported-person parent projection."""

from packs.ingestion.primitives.deep_context.db.models import IsoTimestamp
from packs.ingestion.primitives.pipeline.contract import StageManifest


class EnsureParentsManifest(StageManifest):
    source: str = "ensure_parents"
    people_projected: int = 0
    updated_at: IsoTimestamp | None = None
