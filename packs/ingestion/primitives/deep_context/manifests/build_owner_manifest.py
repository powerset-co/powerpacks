"""Manifest emitted by owner-profile construction."""

from packs.ingestion.primitives.deep_context.db.models import IsoTimestamp
from packs.ingestion.primitives.pipeline.contract import StageManifest


class BuildOwnerManifest(StageManifest):
    source: str = "build_owner"
    path: str | None = None
    name: str | None = None
    schools: list[str] | None = None
    employers: list[str] | None = None
    hint: str | None = None
    error: str | None = None
    from_cache: bool | None = None
    locations: list[str] | None = None
    updated_at: IsoTimestamp | None = None
