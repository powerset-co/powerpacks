"""Compute review manifests, enrichment state, and explicit spend approval."""

from __future__ import annotations

import math
from dataclasses import replace

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.db.workflow_views import (
    WorkflowState,
    workflow_state,
)
from packs.ingestion.primitives.deep_context.manifests.receipt_status import (
    ReceiptStatus,
)
from packs.ingestion.primitives.deep_context.manifests.review_manifest import (
    ReviewManifest,
)
from packs.ingestion.primitives.deep_context.enrich.parallel_research.config import (
    DEFAULT_PROCESSOR,
)
from packs.ingestion.primitives.deep_context.enrich.research_reconcile import (
    selection as research_selection,
)
from packs.ingestion.primitives.deep_context.review.models import (
    EnrichmentApproval,
    EnrichmentCounts,
    EnrichmentView,
)

STAGES = ("worth", "enrich", "linkedin")
STAGE_BY_ACTION = {
    "review_people": "worth",
    "enrich": "enrich",
    "review_linkedin": "linkedin",
    "realize": "done",
}


def enrichment_view(
    db: Db,
    confirm_threshold: float,
    state: WorkflowState | None = None,
    *,
    enrichment_running: bool = False,
    running_error: str | None = None,
) -> EnrichmentView:
    """Render state from the DB plan plus the one local pipeline thread.

    One server, one user, one process: nothing is running unless this
    process's lock says so. A crashed run leaves no lock, so the plan
    recomputes and the approve button returns — no receipt reconciliation,
    no cross-process fingerprint matching. The manifest file is write-only
    observability (and the SSE payload source); it is never read here.
    """
    state = state or workflow_state(db)
    plan = research_selection.select_research(
        db,
        processor=DEFAULT_PROCESSOR,
        confirm_threshold=confirm_threshold,
        include_plausibly_absent=True,
        fingerprint=state.selection,
    )
    current_selection = plan.fingerprint
    pending, total = len(plan.pending), plan.deduped_total
    # While the local thread runs, live progress IS the plan: every projected
    # result moves a row from pending to reused_completed at the next read.
    if enrichment_running:
        return EnrichmentView(
            source="reconcile_deep_research",
            eligible=len(plan.eligible),
            eligible_candidates=plan.eligible_candidates,
            would_submit=len(plan.pending),
            reused_completed=plan.reused_completed,
            duplicate_handles=plan.duplicate_handles,
            processor=plan.processor,
            cost_per_person_usd=plan.cost_per_person_usd,
            estimated_usd=plan.estimated_usd,
            selection=current_selection,
            request_fingerprint=plan.request_fingerprint,
            stage="enrich",
            status=ReceiptStatus.RUNNING,
            counts=EnrichmentCounts(total, plan.reused_completed, pending),
            state="running",
            approvable=False,
        )
    status = "completed" if not total else (
        ReceiptStatus.NEEDS_APPROVAL if pending else ("completed" if plan.reused_completed else "not_started")
    )
    route_state = "done" if not total else (
        "needs_approval" if pending else ("done" if plan.reused_completed else "profile_prep_pending")
    )
    payload = EnrichmentView(
        source="reconcile_deep_research",
        eligible=len(plan.eligible),
        eligible_candidates=plan.eligible_candidates,
        would_submit=len(plan.pending),
        reused_completed=plan.reused_completed,
        duplicate_handles=plan.duplicate_handles,
        processor=plan.processor,
        cost_per_person_usd=plan.cost_per_person_usd,
        estimated_usd=plan.estimated_usd,
        selection=current_selection,
        request_fingerprint=plan.request_fingerprint,
        stage="enrich",
        status=status,
        counts=EnrichmentCounts(total, plan.reused_completed, pending),
        state=route_state,
        approvable=bool(pending),
    )
    # A just-failed run's error rides in memory (the pipeline thread's last
    # write); after a restart it is gone and the button returns.
    if running_error:
        return replace(
            payload,
            counts=replace(payload.counts, pending=0),
            status=ReceiptStatus.FAILED,
            state="failed",
            error=running_error,
            approvable=False,
        )
    return payload


def review_manifest(
    db: Db,
    confirm_threshold: float,
    stage: str | None = None,
    *,
    state: WorkflowState | None = None,
    enrichment: EnrichmentView | None = None,
) -> ReviewManifest:
    """Project one typed review manifest from current queue state."""
    state = state or workflow_state(db)
    progress = state.progress
    enrichment = enrichment or enrichment_view(db, confirm_threshold, state)
    pending = (
        ("worth", bool(progress.worth_pending)),
        ("enrich", enrichment.status != "completed"),
        ("linkedin", bool(progress.linkedin_pending)),
    )
    selected = stage or STAGE_BY_ACTION[state.next_action]
    if selected == "worth":
        counts = (
            ("total", progress.worth_total),
            ("yes", progress.worth_yes),
            ("no", progress.worth_no),
            ("pending", progress.worth_pending),
            ("ready_for_lookup", progress.lookup_ready),
        )
    elif selected == "enrich":
        counts = (
            ("total", enrichment.counts.total),
            ("completed", enrichment.counts.completed),
            ("pending", enrichment.counts.pending),
        )
    elif selected == "linkedin":
        counts = (
            ("total", progress.linkedin_total),
            ("yes_or_no", progress.linkedin_done),
            ("pending", progress.linkedin_pending),
        )
    else:
        counts = ()
    completed = tuple(name for name, is_pending in pending if not is_pending)
    return ReviewManifest(
        stage=selected,
        status=("completed" if selected == "done" or selected in completed else "awaiting_user"),
        counts=counts,
        completed_stages=completed,
        people_revision=state.selection.review_revision,
        privacy=(
            ("message_bodies_read", False),
            ("network_called", False),
            ("paid_provider_called", False),
        ),
    )


def approve_enrichment(db: Db, confirm_threshold: float) -> EnrichmentView:
    """Validate and attach the explicit one-shot spend approval payload."""
    state = workflow_state(db)
    enrichment = enrichment_view(
        db,
        confirm_threshold,
        state,
    )
    if enrichment.status in {
        ReceiptStatus.RUNNING,
        "submitted",
        "completed",
    }:
        return enrichment
    if (
        enrichment.status != ReceiptStatus.NEEDS_APPROVAL
        and enrichment.state != "profile_prep_pending"
    ):
        raise StoreError("Enrichment is not waiting for approval")
    expected_count = enrichment.would_submit
    estimate = enrichment.estimated_usd
    if expected_count < 0:
        raise StoreError("Enrichment submit count cannot be negative")
    if not math.isfinite(estimate) or estimate < 0:
        raise StoreError("Enrichment estimate must be a finite non-negative amount")
    return replace(
        enrichment,
        approval=EnrichmentApproval(
            status="approved",
            approved_at=now_iso(),
            approved_budget_usd=estimate,
            estimated_usd=estimate,
            would_submit=expected_count,
            selection_fingerprint=state.selection.fingerprint,
            review_revision=state.selection.review_revision,
        ),
    )
