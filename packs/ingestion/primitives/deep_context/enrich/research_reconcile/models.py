"""Frozen selection, proposal, and orchestration rows for deep research."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.view_models import EnrichmentQueueRow
from packs.ingestion.primitives.deep_context.db.workflow_views import ReviewSelection
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.results import (
    RetargetProposal,
)
from packs.ingestion.primitives.deep_context.manifests.enrichment_receipt import (
    EnrichmentReceipt,
)
from packs.ingestion.primitives.deep_context.manifests.receipt_status import (
    ReceiptStatus,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import IdentityTask
from packs.ingestion.primitives.deep_context.enrich.parallel_research.queue import (
    ResearchQueueRow,
)
from packs.ingestion.primitives.deep_context.enrich.parallel_research.models import (
    ResearchProgress,
)


@dataclass(frozen=True)
class ResearchSelection:
    """One parsed snapshot of the SQLite queue and its paid-work estimate."""

    fingerprint: ReviewSelection
    request_fingerprint: str
    eligible: tuple[EnrichmentQueueRow, ...]
    queue: tuple[ResearchQueueRow, ...]
    pending: tuple[ResearchQueueRow, ...]
    reused_completed: int  # fingerprint-matching projected artifact already exists; free
    duplicate_handles: int  # same handle collapsed by filter_already_done; never queued or billed
    eligible_candidates: int
    processor: str
    cost_per_person_usd: float
    estimated_usd: float

    @property
    def deduped_total(self) -> int:
        # The one receipt-count basis, matching the driver's reused + todo
        # arithmetic: duplicate handles are never queued or billed, so no
        # receipt sink ever counts them (they stay visible via
        # duplicate_handles).
        return self.reused_completed + len(self.pending)

@dataclass(frozen=True)
class PreparedResearchProposal:
    """One main-path proposal after fingerprint/cache classification."""

    proposal: RetargetProposal
    task: IdentityTask | None
    disposition: str  # "cached" | "grandfathered" | "pending" — see prepare_research_proposal


@dataclass(frozen=True)
class RetargetRunResult:
    path: str
    proposed: int
    preserved_user_rows: int
    total_rows: int
    judge_calls: int
    cached_verdicts: int
    grandfathered: int
    judge_errors: int = 0


@dataclass(frozen=True)
class JudgingProgress:
    done: int
    total: int

    @property
    def completed(self) -> int:
        # Matches ResearchProgress so the pipeline can display both event types.
        return self.done

    def to_payload(self) -> dict[str, object]:
        return {
            "status": ReceiptStatus.RUNNING,
            "phase": "judging_retargets",
            "counts": {"done": self.done, "total": self.total},
        }


ResearchProgressEvent = ResearchProgress | JudgingProgress


@dataclass(frozen=True)
class ReconcileOptions:
    out_dir: Path
    manifest_path: Path | None
    processor: str
    confirm_threshold: float
    budget: float
    approve: bool
    dry_run: bool
    include_plausibly_absent: bool
    include_candidates: bool
    model: str
    reasoning_effort: str
    on_progress: Callable[[ResearchProgressEvent], None] | None
    db: Db
    receipt: EnrichmentReceipt | None
