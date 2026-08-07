"""Frozen rows passed through the local Deep Context review server."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from packs.ingestion.primitives.deep_context.db.workflow_views import ReviewSelection
from packs.ingestion.primitives.deep_context.db.models import IsoTimestamp


@dataclass(frozen=True)
class EnrichmentCounts:
    total: int
    completed: int
    pending: int


@dataclass(frozen=True)
class EnrichmentApproval:
    status: str
    approved_at: IsoTimestamp
    approved_budget_usd: float
    estimated_usd: float
    would_submit: int
    selection_fingerprint: str
    review_revision: IsoTimestamp


@dataclass(frozen=True)
class EnrichmentView:
    source: str
    eligible: int
    eligible_candidates: int
    would_submit: int
    reused_completed: int
    duplicate_handles: int
    processor: str
    cost_per_person_usd: float
    estimated_usd: float
    selection: ReviewSelection
    stage: str
    status: str
    counts: EnrichmentCounts
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
            "would_submit": self.would_submit,
            "reused_completed": self.reused_completed,
            "duplicate_handles": self.duplicate_handles,
            "processor": self.processor,
            "cost_per_person_usd": self.cost_per_person_usd,
            "estimated_usd": self.estimated_usd,
            "selection": asdict(self.selection),
            "stage": self.stage,
            "status": self.status,
            "counts": asdict(self.counts),
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
    submitted_at: IsoTimestamp
    updated_at: IsoTimestamp
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


@dataclass(frozen=True)
class EnrichmentJobResult:
    approved_budget_usd: float | None = None
    progress_json: str | None = None
    status: str | None = None

    @classmethod
    def from_json(cls, value: str | None) -> EnrichmentJobResult:
        try:
            payload = json.loads(value or "{}")
        except json.JSONDecodeError:
            return cls()
        if not isinstance(payload, dict):
            return cls()
        budget = payload.get("approved_budget_usd")
        progress = payload.get("progress")
        return cls(
            approved_budget_usd=float(budget) if budget is not None else None,
            progress_json=(
                json.dumps(progress, separators=(",", ":"))
                if isinstance(progress, dict)
                else None
            ),
            status=str(payload.get("status")) if payload.get("status") is not None else None,
        )

    def to_json(self) -> str:
        payload: dict[str, Any] = {}
        if self.approved_budget_usd is not None:
            payload["approved_budget_usd"] = self.approved_budget_usd
        if self.progress_json is not None:
            payload["progress"] = json.loads(self.progress_json)
        if self.status is not None:
            payload["status"] = self.status
        return json.dumps(payload, separators=(",", ":"))


@dataclass(frozen=True)
class EnrichmentProgress:
    completed: int
    payload_json: str

    @classmethod
    def from_event(cls, event: ProgressEvent) -> EnrichmentProgress:
        return cls(
            completed=event.completed,
            payload_json=json.dumps(event.to_payload(), separators=(",", ":")),
        )


class ProgressEvent(Protocol):
    @property
    def completed(self) -> int: ...

    def to_payload(self) -> dict[str, object]: ...


@dataclass(frozen=True)
class DecisionResult:
    action: str
    approved: str
    new_url: str
    resolved_pubs: tuple[str, ...]


@dataclass(frozen=True)
class FeedbackAlert:
    status: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"status": self.status, "error": self.error}


@dataclass(frozen=True)
class FeedbackSubmission:
    status: str
    error: str
    payload_json: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> FeedbackSubmission:
        return cls(
            status=str(payload.get("status") or "failed"),
            error=str(payload.get("error") or ""),
            payload_json=json.dumps(payload, separators=(",", ":")),
        )

    def as_dict(self) -> dict[str, Any]:
        return json.loads(self.payload_json)
