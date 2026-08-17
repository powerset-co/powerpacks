"""Manifest emitted by attached-LinkedIn reconciliation."""

from typing import Any

from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.models import (
    IdentityProjectionResult,
    ProfileFetchCounts,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import IdentityUsage
from packs.ingestion.primitives.pipeline.contract import StageManifest


class ReconcileLinkedinManifest(StageManifest):
    source: str = "reconcile_linkedin"
    judge: str = ""
    parents: int = 0
    tasks: int = 0
    judged: int = 0        # LLM calls this run billed
    reused: int = 0        # verdicts answered from the store, unchanged input
    human_settled: int = 0  # skipped: you already answered these, so a verdict would be discarded
    ground_truth_connections: int = 0
    verdicts: dict[str, int] = {}
    conflicts: int = 0
    conflicts_auto_resolved: int = 0
    conflicts_to_review: int = 0
    profile_fetch: ProfileFetchCounts | None = None
    errors: int = 0
    overrides: IdentityProjectionResult | None = None
    needs_review: int = 0
    deep_research_eligible: int = 0
    deep_research_est_usd: float = 0.0
    tokens: IdentityUsage = IdentityUsage()
    estimated_cost_usd: float = 0.0
    elapsed_ms: int = 0
    needs_approval: dict[str, Any] | None = None
