"""Frozen selection, progress, and outcome rows for deep research."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from packs.ingestion.primitives.deep_context.db.view_models import EnrichmentQueueRow
from packs.ingestion.primitives.deep_context.db.workflow_views import ReviewSelection
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.results import (
    RetargetProposal,
)
from packs.ingestion.primitives.deep_context.manifests.receipt_counts import ReceiptCounts
from packs.ingestion.primitives.deep_context.manifests.receipt_status import (
    ReceiptStatus,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import IdentityTask
from packs.ingestion.primitives.deep_context.enrich.parallel_research.queue import ResearchQueueRow


@dataclass(frozen=True)
class ResearchSelection:
    """One parsed snapshot of the SQLite queue and its paid-work estimate."""

    fingerprint: ReviewSelection
    request_fingerprint: str
    eligible: tuple[EnrichmentQueueRow, ...]
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
    proposed: int
    judge_calls: int
    cached_verdicts: int
    grandfathered: int
    judge_errors: int = 0


@dataclass(frozen=True)
class EnrichmentProgress:
    """One whole-run count vocabulary across research and judging."""

    phase: str
    counts: ReceiptCounts
    phase_done: int = 0
    phase_total: int = 0

    @property
    def completed(self) -> int:
        return self.counts.completed

    def to_payload(self) -> dict[str, object]:
        return {
            "status": ReceiptStatus.RUNNING,
            "phase": self.phase,
            "counts": {
                "total": self.counts.total,
                "completed": self.counts.completed,
                "pending": self.counts.pending,
                "failed": self.counts.failed,
            },
            "phase_done": self.phase_done,
            "phase_total": self.phase_total,
        }


@dataclass(frozen=True)
class ResearchOutcome:
    """Typed terminal result; JSON rendering belongs to the outer boundary."""

    status: ReceiptStatus
    counts: ReceiptCounts
    plan: ResearchSelection | None
    budget_usd: float
    elapsed_ms: int
    proposals: RetargetRunResult | None = None
    errors: tuple[str, ...] = ()
    reason: str | None = None
    message: str | None = None
    output_dir: str | None = None

    def to_payload(self) -> dict[str, object]:
        plan = self.plan
        payload: dict[str, object] = {
            "source": "reconcile_deep_research",
            "stage": "enrich",
            "status": self.status.value,
            "counts": {
                "total": self.counts.total,
                "completed": self.counts.completed,
                "pending": self.counts.pending,
                "failed": self.counts.failed,
            },
            "budget_usd": self.budget_usd,
            "elapsed_ms": self.elapsed_ms,
        }
        if plan is not None:
            payload.update({
                "selection": asdict(plan.fingerprint),
                "request_fingerprint": plan.request_fingerprint,
                "eligible": len(plan.eligible),
                "eligible_candidates": plan.eligible_candidates,
                "would_submit": len(plan.pending),
                "reused_completed": plan.reused_completed,
                "duplicate_handles": plan.duplicate_handles,
                "processor": plan.processor,
                "cost_per_person_usd": plan.cost_per_person_usd,
                "estimated_usd": plan.estimated_usd,
            })
        if self.proposals is not None:
            payload.update({
                "retargets_proposed": self.proposals.proposed,
                "judge_calls": self.proposals.judge_calls,
                "cached_verdicts": self.proposals.cached_verdicts,
                "grandfathered": self.proposals.grandfathered,
                "judge_errors": self.proposals.judge_errors,
            })
        if self.errors:
            payload["errors"] = list(self.errors)
            payload["error"] = "; ".join(self.errors)
        for key, value in (
            ("reason", self.reason),
            ("message", self.message),
            ("output_dir", self.output_dir),
        ):
            if value is not None:
                payload[key] = value
        return payload
