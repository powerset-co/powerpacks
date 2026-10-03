"""Select the canonical SQLite enrichment queue for provider research.

Changelog:
- 2026-09-30: `build_queue` reads dossier evidence once per batch of queue rows,
  not once per row; `build_queue_row` takes the row's evidence.
"""

from __future__ import annotations

import json
from dataclasses import replace

from packs.ingestion.primitives.deep_context.db import context_queries, queries
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
from packs.ingestion.primitives.deep_context.merge_candidates.candidate_pairs import source_names_can_match


RESEARCH_BATCH = 500


def build_queue_row(
    evidence: DossierEvidence,
    row: EnrichmentQueueRow,
    *,
    source_names: tuple[str, ...],
    owner_context: str,
    guidance: str = "",
) -> ResearchQueueRow:
    """Render the one provider input shared by ordinary and guided research."""
    email = next(
        (value for value in evidence.emails if "@" in value),
        "",
    )
    phone = next(
        (value for value in evidence.phones if value),
        "",
    )
    names_compatible = source_names_can_match(source_names)
    context = "\n".join((
        "Source contact names: " + json.dumps(source_names, ensure_ascii=False),
        "Source contact emails: " + json.dumps(evidence.emails, ensure_ascii=False),
        "Source contact phones: " + json.dumps(evidence.phones, ensure_ascii=False),
    ))
    if not names_compatible:
        context += "\nSource contact names missing or conflicting; identity requires review."
    if row.linkedin_url:
        # Feeds the provider the profile the attached-link judge already rejected
        # (and why), so paid research doesn't just re-surface the same wrong link.
        context += f"\nRejected LinkedIn: {row.linkedin_url}. Reason: {row.verdict_reason}"
    if owner_context:
        context = "\n".join(filter(None, (context, f"Mailbox owner: {owner_context}")))
    return ResearchQueueRow(
        parent_id=row.parent_id,
        candidate_exists=row.candidate_exists,
        row_key=row.row_key,
        handle=row.parent_slug,
        source_person_ids=row.person_ids,
        display_name=source_names[0] if names_compatible else "",
        bio=evidence.research_bio(),
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
    if not subset:
        return []
    owner_context = owner_background(db)
    source_names = {row.id: row.full_name or "" for row in queries.imported_people(db)}
    queue: list[ResearchQueueRow] = []
    for start in range(0, len(subset), RESEARCH_BATCH):
        batch = subset[start:start + RESEARCH_BATCH]
        evidence_rows = context_queries.dossier_evidence_rows(
            db, tuple(person_id for row in batch for person_id in row.person_ids),
        )
        source_ids = {
            person.person_id for person in evidence_rows.people
            if person.person_id in source_names and not person.is_owner and not person.is_ghost
        }
        evidence_rows = replace(evidence_rows, identifiers=tuple(
            identifier for identifier in evidence_rows.identifiers if identifier.person_id in source_ids
        ))
        queue.extend(
            build_queue_row(
                DossierEvidence.from_rows(row.person_ids, evidence_rows),
                row,
                source_names=tuple(
                    source_names.get(person.person_id, "")
                    for person in evidence_rows.people
                    if person.parent_id == row.parent_id and not person.is_owner and not person.is_ghost
                ),
                owner_context=owner_context,
                guidance=guidance,
            )
            for row in batch
        )
        del evidence_rows
    return queue


def select_research(
    db: Db,
    *,
    processor: str,
    fingerprint: ReviewSelection | None = None,
) -> ResearchSelection:
    if fingerprint is None:
        fingerprint = workflow_state(db).selection
    # Only worth-Yes parents without a known LinkedIn enter.
    eligible = enrichment_queue(db)
    queue = build_queue(eligible, db)
    # pending/reused_completed is the artifact-level reuse that makes an unchanged
    # re-run free: filter_already_done matches each row's input_fingerprint against
    # the last projected research artifact for its handle.
    pending, reused_completed = filter_already_done(
        queue,
        (
            artifact
            for start in range(0, len(eligible), RESEARCH_BATCH)
            for artifact in queries.artifacts(
                db,
                kind=ArtifactKind.RESEARCH.value,
                status=ProjectionStatus.PROJECTED.value,
                parent_ids=tuple(row.parent_id for row in eligible[start:start + RESEARCH_BATCH]),
            )
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
            queue,
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
