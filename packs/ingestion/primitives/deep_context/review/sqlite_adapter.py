"""SQLite adapter for the frozen Deep Context HTTP contract."""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
from typing import Any

from packs.ingestion.primitives.deep_context.shared.common import REVIEW_MANIFEST
from packs.ingestion.primitives.deep_context.db.identity_views import (
    linkedin_parents,
    resolve_identity_key,
)
from packs.ingestion.primitives.deep_context.db.models import (
    GuidanceSnapshotRow,
    PARENT_WORTH_PREFIX,
    RESEARCH_CONFIRM_THRESHOLD,
    ReviewExportRow,
)
from packs.ingestion.primitives.deep_context.db.people_views import (
    CandidateViewRow,
    ParentViewRow,
    avatar_payload,
    person_detail,
)
from packs.ingestion.primitives.deep_context.db.identity_queries import (
    guidance_rows,
    review_rows,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.db.workflow_views import (
    WorkflowState,
    workflow_state,
)
from packs.ingestion.primitives.deep_context.manifests.review_manifest import (
    ReviewManifest,
)
from packs.ingestion.primitives.deep_context.review.enrichment import (
    STAGES,
    STAGE_BY_ACTION,
    approve_enrichment,
    enrichment_view,
    review_manifest,
)
from packs.ingestion.primitives.deep_context.review.models import (
    DecisionResult,
    EnrichmentView,
    GuidanceViewRow,
    ReviewCounts,
)
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url

__all__ = ["STAGES", "SqliteReviewAdapter"]


def _guidance_view_row(row: GuidanceSnapshotRow) -> GuidanceViewRow:
    """Project the typed SQLite guidance snapshot onto the HTTP wire row."""
    if row.detail:
        detail = row.detail
        return GuidanceViewRow(
            slug=detail.slug,
            row_key=detail.row_key,
            name=detail.name,
            guidance=detail.guidance,
            state=detail.state,
            detail=detail.detail,
            submitted_at=detail.submitted_at or "",
            updated_at=detail.updated_at or "",
            new_url=detail.new_url or "",
            wire_fields=detail.wire_fields,
            extra_json=detail.extra_json,
        )
    return GuidanceViewRow(
        slug=row.handle,
        row_key=row.candidate_key or "",
        name="",
        guidance=row.guidance,
        state=row.state,
        detail="",
        submitted_at=row.submitted_at or "",
        updated_at=row.submitted_at or "",
        new_url="",
        wire_fields=(
            "slug",
            "row_key",
            "guidance",
            "state",
            "detail",
            "submitted_at",
            "updated_at",
        ),
    )


@dataclass
class SqliteReviewAdapter:
    db: Db
    confirm_threshold: float = RESEARCH_CONFIRM_THRESHOLD

    def snapshot(self, *, job_running: bool = False) -> WorkflowState:
        return workflow_state(self.db, job_running=job_running)

    def manifest(
        self,
        stage: str | None = None,
        *,
        state: WorkflowState | None = None,
        enrichment: EnrichmentView | None = None,
    ) -> ReviewManifest:
        return review_manifest(
            self.db,
            self.confirm_threshold,
            stage,
            state=state,
            enrichment=enrichment,
        )

    def enrichment(
        self,
        state: WorkflowState | None = None,
    ) -> EnrichmentView:
        return enrichment_view(self.db, self.confirm_threshold, state)

    def set_worth(self, key: str, value: str, note: str = "") -> None:
        self.db.decide_worth(
            key.removeprefix(PARENT_WORTH_PREFIX),
            None if value == "restore" else value,
            note=note or None,
        )

    def decide(
        self,
        key: str,
        decision: str,
        new_url: str = "",
        note: str = "",
    ) -> DecisionResult:
        candidate: ReviewExportRow | None = next(iter(review_rows(self.db, key=key)), None)
        if candidate is None:
            raise StoreError(f"review row not found: {key}")
        if decision == "reset":
            resolved = self.db.decide_identity(candidate.key, None)
            current: ReviewExportRow | None = next(iter(review_rows(self.db, key=candidate.key)), None)
            return DecisionResult(
                action=current.action or "" if current else "",
                approved=current.approved or "" if current else "",
                new_url=current.new_linkedin_url or "" if current else "",
                resolved_pubs=tuple(resolved),
            )
        action = {
            "keep": "retarget" if candidate.new_linkedin_url else "verify",
            "detach": "detach",
            "fix": "retarget",
            "exclude": "exclude",
        }.get(decision)
        if action is None:
            raise StoreError(f"unknown decision: {decision}")
        replacement = (
            new_url if decision == "fix" else str(candidate.new_linkedin_url or "") if action == "retarget" else ""
        )
        if action == "retarget":
            replacement = normalize_linkedin_url(replacement)
            if not replacement:
                raise StoreError("fix needs a LinkedIn URL")
            resolved = self.db.decide_identity(
                candidate.key,
                action,
                note=note or None,
                replacement_url=replacement,
                replacement_public_identifier=extract_public_identifier(replacement).lower(),
            )
        else:
            resolved = self.db.decide_identity(
                candidate.key,
                action,
                note=note or None,
            )
        return DecisionResult(action, "yes", replacement, tuple(resolved))

    def approve_enrichment(self) -> EnrichmentView:
        return approve_enrichment(self.db, self.confirm_threshold)

    def retargets(self) -> list[GuidanceViewRow]:
        rows = guidance_rows(self.db)
        return [_guidance_view_row(row) for row in reversed(rows)]

    def resolve_row_key(self, value: str) -> str | None:
        """Resolve one external row key or public identifier at the HTTP edge."""
        resolved = resolve_identity_key(self.db, value)
        return resolved[0] if resolved else None

    def resolve_candidate(
        self,
        value: str,
    ) -> tuple[str, ParentViewRow] | None:
        """Resolve one external identity key and hydrate its canonical parent once."""
        resolved = resolve_identity_key(self.db, value)
        if not resolved:
            return None
        row_key, parent_id = resolved
        parent = person_detail(self.db, parent_id)
        return (row_key, parent) if parent else None

    @staticmethod
    def candidate(parent: ParentViewRow | None, row_key: str) -> CandidateViewRow | None:
        return (
            next(
                (row for row in parent.candidates if row.row_key == row_key),
                None,
            )
            if parent
            else None
        )

    def avatar(self, key: str) -> tuple[bytes, str] | None:
        payload = avatar_payload(self.db, key)
        if payload is None:
            return None
        try:
            body = base64.b64decode(payload.base64, validate=True)
        except (ValueError, TypeError):
            return None
        return (body, payload.content_type) if body else None

    def workflow_status(
        self,
        *,
        job_running: bool = False,
        state: WorkflowState | None = None,
    ) -> dict[str, Any]:
        current = state or self.snapshot(job_running=job_running)
        payload = asdict(current)
        enrichment = self.enrichment(current)
        payload["review_manifest"] = self.manifest(
            state=current,
            enrichment=enrichment,
        ).as_dict()
        payload["enrichment"] = enrichment.as_dict()
        return payload

    def status(self, *, job_running: bool = False) -> dict[str, Any]:
        workflow = self.snapshot(job_running=job_running)
        return {
            "primitive": "reconcile_review_web",
            "ok": True,
            "manifest": str(REVIEW_MANIFEST),
            "stage": STAGE_BY_ACTION[workflow.next_action],
            "next_action": workflow.next_action,
            "state_token": workflow.state_token,
        }

    def counts(self) -> ReviewCounts:
        parents = linkedin_parents(self.db)
        candidates = [row for parent in parents for row in parent.candidates]
        return ReviewCounts(
            parents=len(parents),
            candidates=len(candidates),
            pending=sum(row.pending for row in candidates),
            approved=sum(row.approved in {"yes", "auto"} for row in candidates),
            rejected=sum(row.approved == "no" for row in candidates),
        )
