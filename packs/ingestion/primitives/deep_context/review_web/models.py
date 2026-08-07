"""Frozen rows passed through the local Deep Context review server."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from packs.ingestion.primitives.deep_context.db.workflow_views import ReviewSelection


@dataclass(frozen=True)
class EnrichmentCounts:
    total: int
    completed: int
    pending: int
    failed: int


@dataclass(frozen=True)
class EnrichmentApproval:
    status: str
    approved_at: str
    approved_budget_usd: float
    estimated_usd: float
    would_submit: int
    selection_fingerprint: str
    review_revision: str


@dataclass(frozen=True)
class EnrichmentView:
    source: str
    eligible: int
    eligible_candidates: int
    candidates_skipped_not_added: int
    would_submit: int
    reused_completed: int
    duplicate_handles: int
    processor: str
    cost_per_person_usd: float
    estimated_usd: float
    budget_usd: float
    selection: ReviewSelection
    updated_at: str
    stage: str
    status: str
    counts: EnrichmentCounts
    current: bool
    approval_current: bool
    state: str
    approvable: bool
    approval: EnrichmentApproval | None = None
    approved_budget_usd: float | None = None
    progress_json: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": self.source,
            "eligible": self.eligible,
            "eligible_candidates": self.eligible_candidates,
            "candidates_skipped_not_added": self.candidates_skipped_not_added,
            "would_submit": self.would_submit,
            "reused_completed": self.reused_completed,
            "duplicate_handles": self.duplicate_handles,
            "processor": self.processor,
            "cost_per_person_usd": self.cost_per_person_usd,
            "estimated_usd": self.estimated_usd,
            "budget_usd": self.budget_usd,
            "selection": asdict(self.selection),
            "updated_at": self.updated_at,
            "stage": self.stage,
            "status": self.status,
            "counts": asdict(self.counts),
            "current": self.current,
            "approval_current": self.approval_current,
            "state": self.state,
            "approvable": self.approvable,
        }
        if self.approval:
            payload["approval"] = asdict(self.approval)
        if self.approved_budget_usd is not None:
            payload["approved_budget_usd"] = self.approved_budget_usd
        if self.progress_json is not None:
            payload["progress"] = json.loads(self.progress_json)
        if self.error is not None:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True)
class ReviewManifest:
    stage: str
    status: str
    counts: tuple[tuple[str, int], ...]
    completed_stages: tuple[str, ...]
    people_revision: str
    updated_at: None
    review_csv: str
    synthetic_people_csv: str
    privacy: tuple[tuple[str, bool], ...]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["counts"] = dict(self.counts)
        payload["completed_stages"] = list(self.completed_stages)
        payload["privacy"] = dict(self.privacy)
        return payload


@dataclass(frozen=True)
class ReviewCounts:
    parents: int
    candidates: int
    pending: int
    approved: int
    rejected: int


@dataclass(frozen=True)
class GuidanceViewRow:
    slug: str
    row_key: str
    name: str
    guidance: str
    state: str
    detail: str
    submitted_at: str
    updated_at: str
    new_url: str
    wire_fields: tuple[str, ...]
    extra_json: str = "{}"

    def as_dict(self) -> dict[str, Any]:
        values: dict[str, Any] = {
            "slug": self.slug,
            "row_key": self.row_key,
            "name": self.name,
            "guidance": self.guidance,
            "state": self.state,
            "detail": self.detail,
            "submitted_at": self.submitted_at,
            "updated_at": self.updated_at,
            "new_url": self.new_url,
            **json.loads(self.extra_json),
        }
        return {field: values[field] for field in self.wire_fields if field in values}
