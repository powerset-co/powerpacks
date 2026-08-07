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
    stage: str = "enrich"
    eligible: int | None = None
    would_submit: int | None = None
    reused_completed: int | None = None
    duplicate_handles: int | None = None
    processor: str | None = None
    estimated_usd: float | None = None
    budget_usd: float | None = None
    result_status: str | None = None
    error: str | None = None
    phase: str | None = None
    done: int | None = None
    total: int | None = None

    def to_payload(self) -> dict[str, Any]:
        values = asdict(self)
        return {key: value for key, value in values.items() if value is not None}
