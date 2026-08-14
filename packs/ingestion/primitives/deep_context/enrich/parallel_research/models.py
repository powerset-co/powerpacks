"""Frozen result and progress rows for one Parallel research run."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from parallel.types import TaskGroupStatus

from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.manifests.receipt_counts import ReceiptCounts
from packs.ingestion.primitives.deep_context.enrich.parallel_research import config
from packs.ingestion.primitives.deep_context.enrich.parallel_research.queue import ResearchQueueRow


@dataclass(frozen=True)
class ResearchRunCounts:
    run_ids: int
    results_fetched: int
    errors: int
    real_name_found: int
    linkedin_found: int


@dataclass(frozen=True)
class ResearchProgress:
    status: str
    counts: ReceiptCounts

    @property
    def completed(self) -> int:
        return self.counts.completed

    def to_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "counts": {
                "total": self.counts.total,
                "completed": self.counts.completed,
                "pending": self.counts.pending,
                "failed": self.counts.failed,
            },
        }


@dataclass(frozen=True)
class ResearchRunParams:
    """One explicit configuration door for an in-process research pass."""

    output_dir: Path
    db: Db
    rows: tuple[ResearchQueueRow, ...] = ()
    processor: str = config.DEFAULT_PROCESSOR
    selection_fingerprint: str | None = None
    manifest: Path | None = None
    api_key: str | None = None
    base_url: str = config.DEFAULT_BASE_URL
    beta_header: str = config.DEFAULT_BETA_HEADER
    batch_size: int = config.DEFAULT_BATCH_SIZE
    poll_interval: int = config.DEFAULT_POLL_INTERVAL
    max_wait: int = config.DEFAULT_MAX_WAIT
    api_timeout: int = 60
    on_progress: Callable[[ResearchProgress], None] | None = None
    # False when a caller (research_reconcile) already owns the on-disk receipt
    # and only wants progress projected into the DB, not double-written to a
    # second manifest.json.
    owns_receipt: bool = True

    def __post_init__(self) -> None:
        if self.manifest is not None and self.manifest.name != "manifest.json":
            raise SystemExit("--manifest must end in manifest.json")


@dataclass(frozen=True)
class ResearchRunResult:
    """One provider run after its raw SDK payload has been parsed."""

    status: str
    error: str | None = None
    output_dir: str | None = None
    counts: ResearchRunCounts | None = None
    errors: tuple[str, ...] = ()

    @classmethod
    def failed(cls, error: str) -> ResearchRunResult:
        return cls("failed", error=error)


# Terminal statuses where the already-completed portion of a run is usable —
# unlike "failed", where nothing in this pass completed. completed_with_errors
# fires whenever ANY handle in the batch errored, including the benign case at
# parallel_client.py where a completed run just came back without its
# metadata.handle: the rest of the batch still succeeded and billed, so a
# caller must still consume it, not discard the whole pass. Both
# research_reconcile.coordinator and identity_reconcile.guided gate on this
# set — import it from here, don't respell it.
RESEARCH_OK_STATUSES = frozenset({"no_work", "completed", "completed_with_errors"})


@dataclass(frozen=True)
class ParallelExecutionResult:
    run_count: int
    result_count: int
    errors: tuple[str, ...]
    final_status: TaskGroupStatus | None
