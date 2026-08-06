"""Build and write the one fixed enrichment receipt during reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.deep_research_contacts import (
    ResearchRunParams,
)
from packs.ingestion.primitives.deep_context.enrichment_contract import (
    STATUS_FAILED,
    STATUS_RESEARCH_COMPLETE,
    STATUS_RUNNING,
)
from packs.ingestion.primitives.deep_context.enrichment_receipt import (
    EnrichmentReceipt,
    EnrichmentReceiptBody,
    enrichment_counts,
)
from packs.ingestion.primitives.deep_context.research_reconcile.selection import (
    ResearchSelection,
)
from packs.ingestion.primitives.deep_context.parallel_research.driver import (
    research_artifact_inventory,
)


@dataclass(frozen=True)
class ReceiptPolicy:
    """Render stable terminal and in-flight receipts from one queue snapshot."""

    receipt: EnrichmentReceipt | None
    manifest_path: Path | None
    selection: ResearchSelection
    params: ResearchRunParams
    overrides_csv: Path
    facts_dir: Path
    queue_csv: Path
    out_dir: Path
    budget: float

    def inventory(self) -> list[dict[str, Any]] | None:
        return research_artifact_inventory(self.params)

    def body(
        self,
        status: str,
        result: dict[str, Any],
        *,
        completed: int = 0,
        failed: int = 0,
    ) -> dict[str, Any]:
        plan = self.selection
        return {
            "stage": "enrich",
            "status": status,
            "counts": enrichment_counts(
                total=len(plan.queue), completed=completed, failed=failed
            ),
            "selection": plan.fingerprint,
            "eligible": len(plan.eligible),
            "eligible_candidates": plan.eligible_candidates,
            "candidates_skipped_not_added": plan.candidates_skipped_not_added,
            "would_submit": len(plan.pending),
            "reused_completed": plan.reused_completed,
            "duplicate_handles": plan.duplicate_handles,
            "processor": plan.processor,
            "cost_per_person_usd": plan.cost_per_person_usd,
            "estimated_usd": plan.estimated_usd,
            "budget_usd": self.budget,
            "input": {
                "review_csv": str(self.overrides_csv),
                "facts_dir": str(self.facts_dir),
                "queue_csv": str(self.queue_csv),
            },
            "outputs": {
                "research_dir": str(self.out_dir),
                "review_csv": str(self.overrides_csv),
            },
            "privacy": {
                "message_bodies_read": False,
                "paid_provider_called": status
                in {STATUS_RUNNING, STATUS_RESEARCH_COMPLETE, STATUS_FAILED},
            },
            "result_status": result.get("status", ""),
            "error": (
                str(result.get("error") or result.get("research_error") or "")
                if status == STATUS_FAILED
                else None
            ),
            "artifacts": self.inventory(),
        }

    def write(self, payload: dict[str, Any]) -> None:
        if not self.receipt:
            return
        self.receipt.write({**payload, "artifacts": self.inventory() or []})

    def terminal(
        self,
        result: dict[str, Any],
        status: str,
        *,
        completed: int = 0,
        failed: int = 0,
    ) -> EnrichmentReceiptBody:
        return EnrichmentReceiptBody(
            source=self.manifest_path.parent.name if self.manifest_path else None,
            **self.body(status, result, completed=completed, failed=failed),
        )

    def judging(self, done: int, total: int) -> dict[str, Any]:
        return {
            "stage": "enrich",
            "status": STATUS_RUNNING,
            "phase": "judging_retargets",
            "done": done,
            "total": total,
            "counts": enrichment_counts(
                total=len(self.selection.queue),
                completed=self.selection.reused_completed,
            ),
            "selection": self.selection.fingerprint,
        }
