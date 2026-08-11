"""Frozen selection, proposal, and orchestration rows for deep research."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.view_models import EnrichmentQueueRow
from packs.ingestion.primitives.deep_context.db.workflow_views import ReviewSelection
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.results import (
    RetargetProposal,
)
from packs.ingestion.primitives.deep_context.manifests.enrichment_receipt import (
    EnrichmentReceipt,
)
from packs.ingestion.primitives.deep_context.enrich.judge_models import IdentityTask
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
    eligible: tuple[EnrichmentQueueRow, ...]
    queue: tuple[ResearchQueueRow, ...]
    pending: tuple[ResearchQueueRow, ...]
    reused_completed: int  # fingerprint-matching projected artifact already exists; free
    duplicate_handles: int  # same handle collapsed by filter_already_done; never queued or billed
    eligible_candidates: int
    processor: str
    cost_per_person_usd: float
    estimated_usd: float

    def result_base(self, budget: float) -> dict[str, Any]:
        return {
            "source": "reconcile_deep_research",
            "eligible": len(self.eligible),
            "eligible_candidates": self.eligible_candidates,
            "would_submit": len(self.pending),
            "reused_completed": self.reused_completed,
            "duplicate_handles": self.duplicate_handles,
            "processor": self.processor,
            "cost_per_person_usd": self.cost_per_person_usd,
            "estimated_usd": self.estimated_usd,
            "budget_usd": budget,
            "selection": asdict(self.fingerprint),
            "updated_at": now_iso(),
        }


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


@dataclass(frozen=True)
class JudgingProgress:
    done: int
    total: int

    @property
    def completed(self) -> int:
        # Satisfies the structural ProgressEvent protocol (review/models.py) shared
        # with ResearchProgress, so EnrichmentProgress.from_event reads progress
        # uniformly across research and judging phases with no isinstance check.
        return self.done

    def to_payload(self) -> dict[str, object]:
        return {
            "status": "running",
            "phase": "judging_retargets",
            "counts": {"done": self.done, "total": self.total},
        }


ResearchProgressEvent = ResearchProgress | JudgingProgress


@dataclass(frozen=True)
class ReconcileOptions:
    out_dir: Path
    queue_csv: Path
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


@dataclass(frozen=True)
class ReconcileOutput:
    selection: ResearchSelection
    budget: float
    status: str
    queue_csv: str
    elapsed_ms: int
    reason: str | None = None
    message: str | None = None
    output_dir: str | None = None
    research_status: str | None = None
    research_error: str | None = None
    progress: str | None = None
    retargets_proposed: int | None = None
    judge_calls: int | None = None
    cached_verdicts: int | None = None
    grandfathered: int | None = None

    def to_payload(self) -> dict[str, Any]:
        payload = self.selection.result_base(self.budget)
        payload.update({"status": self.status, "queue_csv": self.queue_csv})
        for key in (
            "reason",
            "message",
            "output_dir",
            "research_status",
            "research_error",
            "progress",
            "retargets_proposed",
            "judge_calls",
            "cached_verdicts",
            "grandfathered",
        ):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        payload["elapsed_ms"] = self.elapsed_ms
        return payload

