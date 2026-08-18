"""Frozen inputs and factual counts for one Parallel research pass."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.manifests.receipt_counts import ReceiptCounts
from packs.ingestion.primitives.deep_context.enrich.parallel_research import config
from packs.ingestion.primitives.deep_context.enrich.parallel_research.queue import ResearchQueueRow


@dataclass(frozen=True)
class ResearchRunParams:
    """One explicit configuration door for an in-process research pass."""

    output_dir: Path
    db: Db
    rows: tuple[ResearchQueueRow, ...] = ()
    processor: str = config.DEFAULT_PROCESSOR
    api_key: str | None = None
    base_url: str = config.DEFAULT_BASE_URL
    beta_header: str = config.DEFAULT_BETA_HEADER
    batch_size: int = config.DEFAULT_BATCH_SIZE
    stream_timeout: int = config.DEFAULT_STREAM_TIMEOUT
    on_progress: Callable[[ReceiptCounts], None] | None = None


@dataclass(frozen=True)
class ResearchRunResult:
    """Factual outcome; callers derive policy from counts, not status aliases."""

    total: int
    completed: int = 0
    errors: tuple[str, ...] = ()

    @classmethod
    def failed(cls, total: int, error: str) -> ResearchRunResult:
        return cls(total, errors=(error,))

    @property
    def complete(self) -> bool:
        return self.completed == self.total and not self.errors

    @property
    def usable(self) -> bool:
        return self.total == 0 or self.completed > 0
