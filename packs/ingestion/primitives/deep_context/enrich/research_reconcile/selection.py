"""Select the canonical SQLite enrichment queue for provider research."""

from __future__ import annotations

from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.deep_context.db.models import ArtifactKind, ProjectionStatus
from packs.ingestion.primitives.deep_context.db.identity_views import enrichment_queue
from packs.ingestion.primitives.deep_context.db.view_models import EnrichmentQueueRow
from packs.ingestion.primitives.deep_context.db.workflow_views import (
    ReviewSelection,
    workflow_state,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import (
    DossierEvidence,
    owner_background,
)
from packs.ingestion.primitives.deep_context.enrich.parallel_research.config import (
    PROCESSOR_PRICING_USD,
)
from packs.ingestion.primitives.deep_context.enrich.parallel_research.queue import (
    ResearchQueueRow,
    filter_already_done,
    request_plan_fingerprint,
)
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.models import (
    ResearchSelection,
)

def build_queue_row(
    db: Db,
    row: EnrichmentQueueRow,
    *,
    owner_context: str,
    guidance: str = "",
) -> ResearchQueueRow:
    """Render the one provider input shared by ordinary and guided research."""
    email = next(
        (value for value in row.match_emails if "@" in value),
        "",
    )
    phone = next(
        (value for value in row.match_phones if value),
        "",
    )
    context = ""
    if row.linkedin_url:
        # Feeds the provider the profile the attached-link judge already rejected
        # (and why), so paid research doesn't just re-surface the same wrong link.
        context = f"Rejected LinkedIn: {row.linkedin_url}. Reason: {row.verdict_reason}"
    if owner_context:
        context = "\n".join(filter(None, (context, f"Mailbox owner: {owner_context}")))
    return ResearchQueueRow(
        parent_id=row.parent_id,
        candidate_exists=row.candidate_exists,
        row_key=row.row_key,
        handle=row.parent_slug,
        source_person_ids=row.person_ids,
        source_candidate_public_identifier="",
        display_name=row.name,
        bio=DossierEvidence.from_db(db, row.person_ids).research_bio(),
        known_info=context,
        primary_email=email,
        phone_e164=phone,
        retarget_hint=guidance.strip(),
    )


def build_queue(
    subset: list[EnrichmentQueueRow],
    db: Db,
    *,
    guidance: str = "",
) -> list[ResearchQueueRow]:
    owner_context = owner_background(db)
    return [
        build_queue_row(
            db,
            row,
            owner_context=owner_context,
            guidance=guidance,
        )
        for row in subset
    ]


def select_research(
    db: Db,
    *,
    processor: str,
    confirm_threshold: float,
    include_plausibly_absent: bool,
    include_candidates: bool,
    fingerprint: ReviewSelection | None = None,
) -> ResearchSelection:
    if fingerprint is None:
        fingerprint = workflow_state(db).selection
    # Strict worth='yes' here, not the '!=no' ("maybe" included) predicate attached
    # judging uses — research is paid per person, so only the confirmed-worth set
    # qualifies. A row also drops out once it already carries a live (non-rejected)
    # retarget proposal, so a settled parent doesn't re-enter this queue next run.
    eligible = enrichment_queue(
        db,
        include_plausibly_absent=include_plausibly_absent,
        include_candidates=include_candidates,
        confirm_threshold=confirm_threshold,
    )
    queue = build_queue(eligible, db)
    request_queue = build_queue(
        enrichment_queue(
            db,
            include_plausibly_absent=include_plausibly_absent,
            include_candidates=include_candidates,
            include_applied_retargets=True,
            confirm_threshold=confirm_threshold,
        ),
        db,
    )
    # pending/reused_completed is the artifact-level reuse that makes an unchanged
    # re-run free: filter_already_done matches each row's input_fingerprint against
    # the last projected research artifact for its handle.
    pending, reused_completed = filter_already_done(
        queue,
        queries.artifacts(
            db,
            kind=ArtifactKind.RESEARCH.value,
            status=ProjectionStatus.PROJECTED.value,
        ),
        processor=processor,
    )
    # filter_already_done doesn't report duplicates directly — it silently drops
    # rows whose handle repeats — so this is the only place that count exists.
    duplicate_handles = max(0, len(queue) - len(pending) - reused_completed)
    # No .get(..., default) fallback: the CLI's --processor choices are already
    # constrained to this table's keys (build_parser in reconcile_deep_research.py),
    # so an unlisted processor here is a caller bug, not a value to paper over
    # with a different processor's price.
    cost_per = PROCESSOR_PRICING_USD[processor]
    return ResearchSelection(
        fingerprint=fingerprint,
        request_fingerprint=request_plan_fingerprint(
            request_queue,
            processor=processor,
        ),
        eligible=tuple(eligible),
        pending=tuple(pending),
        reused_completed=reused_completed,
        duplicate_handles=duplicate_handles,
        eligible_candidates=sum(bool(row.candidate_origin) for row in eligible),
        processor=processor,
        cost_per_person_usd=cost_per,
        estimated_usd=round(len(pending) * cost_per, 2),
    )
