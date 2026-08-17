"""Display-only receipt body for Deep Context research work."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from packs.ingestion.primitives.deep_context.db.workflow_views import ReviewSelection
from packs.ingestion.primitives.deep_context.manifests.receipt_counts import (
    ReceiptCounts,
)


@dataclass(frozen=True)
class ResearchReceiptBody:
    source: str | None
    status: str
    counts: ReceiptCounts
    selection: ReviewSelection | None = None
    request_fingerprint: str | None = None
    stage: str = "enrich"
    eligible: int | None = None
    eligible_candidates: int | None = None
    would_submit: int | None = None
    reused_completed: int | None = None
    duplicate_handles: int | None = None
    processor: str | None = None
    cost_per_person_usd: float | None = None
    estimated_usd: float | None = None
    budget_usd: float | None = None
    error: str | None = None
    reason: str | None = None
    message: str | None = None
    output_dir: str | None = None
    research_status: str | None = None
    research_error: str | None = None
    research_errors: tuple[str, ...] = ()
    progress: str | None = None
    retargets_proposed: int | None = None
    judge_calls: int | None = None
    cached_verdicts: int | None = None
    grandfathered: int | None = None
    judge_errors: int | None = None
    elapsed_ms: int | None = None
    phase: str | None = None
    done: int | None = None
    total: int | None = None

    def to_payload(self) -> dict[str, Any]:
        values = asdict(self)
        if not self.research_errors:
            values.pop("research_errors")
        else:
            values["research_errors"] = list(self.research_errors)
        return {key: value for key, value in values.items() if value is not None}
