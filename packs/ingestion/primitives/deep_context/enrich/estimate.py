"""Shared Parallel and identity judgment estimate for enrichment: what it costs, how long it takes."""

from __future__ import annotations

import math
from dataclasses import dataclass

from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.workflow_views import StageProgress, WorkflowState, workflow_state
from packs.ingestion.primitives.deep_context.enrich.parallel_research.config import DEFAULT_PROCESSOR
from packs.ingestion.primitives.deep_context.enrich.profiles.prefetch import RAPIDAPI_RPM_DEFAULT
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.models import ResearchSelection
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.selection import select_research
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.judging import mapped_identity_tasks
from packs.ingestion.primitives.deep_context.shared.openai_responses import estimate_cost_usd
from packs.search.primitives.llm_rerank_candidates.jev.client import INPUT_PRICE_PER_MILLION

ESTIMATED_JUDGMENT_INPUT_TOKENS = 2000
ESTIMATED_JUDGMENT_OUTPUT_TOKENS = 1500

# Rough rates for the time left. Lookups are all sent at once: the first thousand take about a
# quarter of an hour, each further thousand a little more, and even a few take some minutes.
LOOKUP_BATCH = 1000
LOOKUP_BATCH_MINUTES = 15
LOOKUP_FURTHER_BATCH_MINUTES = 2
LOOKUP_SHORTEST_MINUTES = 4
# Unsure matches settle at about two a second.
QUESTIONS_PER_MINUTE = 120


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
    progress = state.progress
    mapped_pending = sum(item[-1] is None for item in mapped_identity_tasks(db))
    remaining = (progress.lookups_pending + mapped_pending
                 + progress.questions_pending + progress.synthetic_pending - len(plan.eligible))
    count = remaining + len(plan.pending)
    # Two JEV requests and one possible Sol comparison per candidate.
    return EnrichmentEstimate(
        plan, remaining, count,
        estimate_cost_usd(ESTIMATED_JUDGMENT_INPUT_TOKENS * count,
            ESTIMATED_JUDGMENT_OUTPUT_TOKENS * count, "gpt-6.1-sol"),
        2 * count * ESTIMATED_JUDGMENT_INPUT_TOKENS * INPUT_PRICE_PER_MILLION / 1_000_000,
    )


def minutes_left(progress: StageProgress) -> int:
    """About how long the rest of enrichment takes: what is left, at each step's rate."""
    lookups = progress.lookups_pending
    lookup_minutes = 0.0
    if lookups:
        first = LOOKUP_BATCH_MINUTES * min(lookups, LOOKUP_BATCH) / LOOKUP_BATCH
        further = LOOKUP_FURTHER_BATCH_MINUTES * max(0, lookups - LOOKUP_BATCH) / LOOKUP_BATCH
        lookup_minutes = max(LOOKUP_SHORTEST_MINUTES, first) + further
    # A lookup still out may find a LinkedIn, which is then fetched and judged.
    fetch_minutes = (progress.judgments_pending + lookups) / RAPIDAPI_RPM_DEFAULT
    question_minutes = progress.questions_pending / QUESTIONS_PER_MINUTE
    return math.ceil(lookup_minutes + fetch_minutes + question_minutes)
