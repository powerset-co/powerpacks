"""Shared Parallel and identity judgment estimate for enrichment."""

from __future__ import annotations

from dataclasses import dataclass

from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.workflow_views import WorkflowState, workflow_state
from packs.ingestion.primitives.deep_context.enrich.parallel_research.config import DEFAULT_PROCESSOR
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.models import ResearchSelection
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.selection import select_research
from packs.ingestion.primitives.deep_context.shared.openai_responses import estimate_cost_usd
from packs.search.primitives.llm_rerank_candidates.jev.client import INPUT_PRICE_PER_MILLION

ESTIMATED_JUDGMENT_INPUT_TOKENS = 2000
ESTIMATED_JUDGMENT_OUTPUT_TOKENS = 1500


@dataclass(frozen=True)
class EnrichmentEstimate:
    research: ResearchSelection
    remaining_judgments: int
    judgment_count: int
    judgment_estimated_usd: float
    jev_estimated_usd: float

    @property
    def estimated_usd(self) -> float:
        return self.research.estimated_usd + self.judgment_estimated_usd + self.jev_estimated_usd

    def to_payload(self) -> dict[str, object]:
        return {
            "would_submit": len(self.research.pending),
            "parallel_estimated_usd": self.research.estimated_usd,
            "judgment_count": self.judgment_count,
            "judgment_estimated_usd": self.judgment_estimated_usd,
            "jev_estimated_usd": self.jev_estimated_usd,
            "estimated_usd": self.estimated_usd,
        }


def estimate_enrichment(db: Db, state: WorkflowState | None = None) -> EnrichmentEstimate:
    state = state or workflow_state(db)
    plan = select_research(db, processor=DEFAULT_PROCESSOR, fingerprint=state.selection)
    remaining = state.progress.enrichment_pending - len(plan.eligible)
    count = remaining + len(plan.pending)
    # Two JEV requests and one possible Sol comparison per candidate.
    return EnrichmentEstimate(
        plan, remaining, count,
        estimate_cost_usd(ESTIMATED_JUDGMENT_INPUT_TOKENS * count,
            ESTIMATED_JUDGMENT_OUTPUT_TOKENS * count, "gpt-6.1-sol"),
        2 * count * ESTIMATED_JUDGMENT_INPUT_TOKENS * INPUT_PRICE_PER_MILLION / 1_000_000,
    )
