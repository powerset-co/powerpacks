"""Manifest emitted by the legacy carry-over seed."""

from packs.ingestion.primitives.deep_context.db.models import IsoTimestamp
from packs.ingestion.primitives.pipeline.contract import StageManifest


class SeedManifest(StageManifest):
    source: str = "seed"
    legacy_root: str = ""
    merges_applied: int = 0
    families_ambiguous: int = 0
    facts_carried: int = 0
    facts_duplicate_dropped: int = 0
    facts_two_plus: int = 0
    facts_unmatched: int = 0
    worth_carried: int = 0
    worth_two_plus: int = 0
    worth_unmatched: int = 0
    identity_carried: int = 0
    identity_two_plus: int = 0
    identity_unmatched: int = 0
    research_carried: int = 0
    research_duplicate_dropped: int = 0
    research_two_plus: int = 0
    research_unmatched: int = 0
    machine_review_rows_not_carried: int = 0
    synthetic_rows_not_carried: int = 0
    seeded_at: IsoTimestamp | None = None
