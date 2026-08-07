"""Select the canonical enrichment queue and render its fixed provider CSV."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.common import DEEP_RESEARCH_DIR
from packs.ingestion.primitives.deep_context.db.identity_views import linkedin_review
from packs.ingestion.primitives.deep_context.db.view_models import EnrichmentQueueRow
from packs.ingestion.primitives.deep_context.db.workflow_views import (
    ReviewSelection,
    workflow_state,
)
from packs.ingestion.primitives.deep_context.db.models import CanonicalSnapshot
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.dossier_evidence import (
    DossierEvidence,
    owner_background,
)
from packs.ingestion.primitives.deep_context.parallel_research.config import (
    PROCESSOR_PRICING_USD,
)
from packs.ingestion.primitives.deep_context.parallel_research.queue import (
    ResearchQueueRow,
    filter_already_done,
)


DEFAULT_PROCESSOR = "core2x"
DR_OUT_DIR = DEEP_RESEARCH_DIR
QUEUE_CSV = DR_OUT_DIR / "research_queue.csv"
QUEUE_FIELDS = [
    "handle",
    "source_parent_slug",
    "source_person_ids",
    "source_candidate_public_identifier",
    "display_name",
    "bio",
    "known_info",
    "primary_email",
    "phone_e164",
    "area_code",
    "source_channel",
    "retarget_hint",
]


@dataclass(frozen=True)
class ResearchSelection:
    """One parsed snapshot of the SQLite queue and its paid-work estimate."""

    fingerprint: ReviewSelection
    eligible: tuple[EnrichmentQueueRow, ...]
    queue: tuple[ResearchQueueRow, ...]
    pending: tuple[ResearchQueueRow, ...]
    reused_completed: int
    duplicate_handles: int
    eligible_candidates: int
    processor: str
    cost_per_person_usd: float
    estimated_usd: float

    def result_base(self, budget: float) -> dict[str, Any]:
        return {
            "source": "reconcile_deep_research",
            "eligible": len(self.eligible),
            "eligible_candidates": self.eligible_candidates,
            "candidates_skipped_not_added": 0,
            "would_submit": len(self.pending),
            "reused_completed": self.reused_completed,
            "duplicate_handles": self.duplicate_handles,
            "processor": self.processor,
            "cost_per_person_usd": self.cost_per_person_usd,
            "estimated_usd": self.estimated_usd,
            "budget_usd": budget,
            "selection": asdict(self.fingerprint),
            "updated_at": now_iso(),
        }


def build_queue_row(
    snapshot: CanonicalSnapshot,
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
        context = (
            f"Rejected LinkedIn: {row.linkedin_url}. "
            f"Reason: {row.verdict_reason}"
        )
    if owner_context:
        context = "\n".join(
            filter(None, (context, f"Mailbox owner: {owner_context}"))
        )
    return ResearchQueueRow(
        parent_id=row.parent_id,
        candidate_exists=row.candidate_exists,
        row_key=row.row_key,
        handle=row.parent_slug,
        source_parent_slug=row.parent_slug,
        source_person_ids=row.person_ids,
        source_candidate_public_identifier="",
        display_name=row.name,
        bio=DossierEvidence.from_snapshot(row.person_ids, snapshot).research_bio(),
        known_info=context,
        primary_email=email,
        phone_e164=phone,
        area_code="",
        source_channel="email" if email else "phone",
        retarget_hint=guidance.strip(),
    )


def build_queue(
    subset: list[EnrichmentQueueRow],
    snapshot: CanonicalSnapshot,
    *,
    guidance: str = "",
) -> list[ResearchQueueRow]:
    owner_context = owner_background(snapshot)
    return [
        build_queue_row(
            snapshot,
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
    eligible = linkedin_review(
        db,
        "enrichment",
        include_plausibly_absent=include_plausibly_absent,
        include_candidates=include_candidates,
        confirm_threshold=confirm_threshold,
    )
    snapshot = canonical_snapshot(db)
    queue = build_queue(eligible, snapshot)
    pending, reused_completed = filter_already_done(queue, snapshot.artifacts)
    duplicate_handles = max(0, len(queue) - len(pending) - reused_completed)
    cost_per = PROCESSOR_PRICING_USD.get(
        processor, PROCESSOR_PRICING_USD[DEFAULT_PROCESSOR]
    )
    return ResearchSelection(
        fingerprint=fingerprint,
        eligible=tuple(eligible),
        queue=tuple(queue),
        pending=tuple(pending),
        reused_completed=reused_completed,
        duplicate_handles=duplicate_handles,
        eligible_candidates=sum(bool(row.candidate_origin) for row in eligible),
        processor=processor,
        cost_per_person_usd=cost_per,
        estimated_usd=round(len(pending) * cost_per, 2),
    )


def write_queue(path: Path, rows: tuple[ResearchQueueRow, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=QUEUE_FIELDS)
        writer.writeheader()
        writer.writerows(row.csv_dict(QUEUE_FIELDS) for row in rows)
