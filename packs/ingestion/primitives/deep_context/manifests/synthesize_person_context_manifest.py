"""Manifest emitted by parent-context synthesis."""

from pydantic import Field

from packs.ingestion.primitives.deep_context.db.models import IsoTimestamp
from packs.ingestion.primitives.deep_context.synthesis import prompting
from packs.ingestion.primitives.deep_context.synthesis.models import WorthSyncResult
from packs.ingestion.primitives.deep_context.synthesis.prompting import (
    DEFAULT_TARGET_CONFIDENCE,
)
from packs.ingestion.primitives.pipeline.contract import StageManifest

DEFAULT_MAX_BATCHES = 20


class SynthesizePersonContextManifest(StageManifest):
    source: str = "synthesize_person_context"
    people: int = 0
    chunk_people: int = 0
    people_done: int = 0
    batches_run: int = 0
    avg_batches_per_person: float = 0.0
    stop_reasons: dict[str, int] = Field(default_factory=dict)
    errors: int = 0
    model: str = ""
    synthesis_version: str = prompting.SYNTHESIS_VERSION
    reasoning_effort: str = ""
    owner_context: bool = False
    orphan_facts_removed: int = 0
    rejudge: bool = False
    target_confidence: float = DEFAULT_TARGET_CONFIDENCE
    max_batches: int = DEFAULT_MAX_BATCHES
    concurrency: int = 0
    tokens: dict[str, int] = Field(default_factory=dict)
    estimated_cost_usd: float = 0.0
    out_dir: str = ""
    worth_sync: WorthSyncResult | None = None
    elapsed_ms: int = 0
    updated_at: IsoTimestamp | None = None
