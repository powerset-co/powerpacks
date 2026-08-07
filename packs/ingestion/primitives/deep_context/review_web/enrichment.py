"""Compute review manifests, enrichment state, and explicit spend approval."""

from __future__ import annotations

import math
from dataclasses import replace

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.assemble_synthetic_profile import DEFAULT_OUT
from packs.ingestion.primitives.deep_context.db.identity_views import latest_job
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.db.workflow_views import (
    WorkflowState,
    workflow_state,
)
from packs.ingestion.primitives.deep_context.research_reconcile import (
    selection as research_selection,
)
from packs.ingestion.primitives.deep_context.review_web.models import (
    EnrichmentApproval,
    EnrichmentCounts,
    EnrichmentJobResult,
    EnrichmentView,
    ReviewManifest,
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
) -> EnrichmentView:
    """Return the current research plan plus any matching async-job receipt."""
    state = state or workflow_state(db)
    plan = research_selection.select_research(
        db,
        processor=research_selection.DEFAULT_PROCESSOR,
        confirm_threshold=confirm_threshold,
        include_candidates=True,
        include_plausibly_absent=True,
        fingerprint=state.selection,
    )
    current_selection = plan.fingerprint
    pending, total = len(plan.pending), len(plan.eligible)
    status = "completed" if not total else ("needs_approval" if pending else "not_started")
    route_state = "done" if not total else (
        "needs_approval" if pending else "profile_prep_pending"
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
        stage="enrich",
        status=status,
        counts=EnrichmentCounts(total, plan.reused_completed, pending),
        state=route_state,
        approvable=bool(pending),
    )
    job = latest_job(db, "enrichment")
    if job is None or job.selection_fingerprint != current_selection.fingerprint:
        return payload

    status = job.status
    result = EnrichmentJobResult.from_json(job.result_json)
    completed = job.completed_count
    job_total = job.total_count or total
    payload = replace(
        payload,
        counts=EnrichmentCounts(job_total, completed, max(0, job_total - completed)),
        approved_budget_usd=result.approved_budget_usd,
        progress_json=result.progress_json,
    )
    if total and status in {"queued", "running"}:
        return replace(payload, status="running", state="running")
    if status == "applied":
        return replace(payload, status="completed", state="done", approvable=False)
    if total and status == "failed":
        return replace(
            payload,
            counts=replace(payload.counts, pending=0),
            status="failed",
            state="failed",
            error=job.error,
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
        synthetic_people_csv=str(DEFAULT_OUT),
        privacy=(
            ("message_bodies_read", False),
            ("network_called", False),
            ("paid_provider_called", False),
        ),
    )


def approve_enrichment(db: Db, confirm_threshold: float) -> EnrichmentView:
    """Validate and attach the explicit one-shot spend approval payload."""
    state = workflow_state(db)
    enrichment = enrichment_view(db, confirm_threshold, state)
    if enrichment.status in {
        "running",
        "submitted",
        "research_complete",
        "completed",
    }:
        return enrichment
    if (
        enrichment.status != "needs_approval"
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
