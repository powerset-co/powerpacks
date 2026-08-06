"""SQLite adapter for the frozen Deep Context HTTP contract."""

from __future__ import annotations

import base64
import json
import math
from dataclasses import dataclass
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.assemble_synthetic_profile import DEFAULT_OUT
from packs.ingestion.primitives.deep_context.common import LINKEDIN_OVERRIDES_CSV, REVIEW_MANIFEST
from packs.ingestion.primitives.deep_context.db.identity_views import (
    linkedin_review,
    resolve_identity_key,
)
from packs.ingestion.primitives.deep_context.db.models import (
    PARENT_WORTH_PREFIX,
    RESEARCH_CONFIRM_THRESHOLD,
)
from packs.ingestion.primitives.deep_context.db.people_views import avatar_payload, person_detail
from packs.ingestion.primitives.deep_context.db.snapshots import identity_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.db.workflow_views import workflow_state
from packs.ingestion.primitives.deep_context.research_reconcile import selection as research_selection
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url

STAGES = ("worth", "enrich", "linkedin")
STAGE_BY_ACTION = {
    "review_people": "worth",
    "enrich": "enrich",
    "review_linkedin": "linkedin",
    "realize": "done",
}


@dataclass
class SqliteReviewAdapter:
    db: Db
    confirm_threshold: float = RESEARCH_CONFIRM_THRESHOLD

    def snapshot(self, *, job_running: bool = False) -> dict[str, Any]:
        return workflow_state(self.db, job_running=job_running)

    def manifest(
        self,
        stage: str | None = None,
        *,
        state: dict[str, Any] | None = None,
        enrichment: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = state or self.snapshot()
        progress = state["progress"]
        enrichment = enrichment or self.enrichment(state)
        pending = {
            "worth": progress["worth_pending"],
            "enrich": enrichment.get("status") != "completed",
            "linkedin": progress["linkedin_pending"],
        }
        selected = stage or STAGE_BY_ACTION[state["next_action"]]
        counts = {
            "worth": {
                "total": progress["worth_total"],
                "yes": progress["worth_yes"],
                "no": progress["worth_no"],
                "pending": progress["worth_pending"],
                "ready_for_lookup": progress["lookup_ready"],
            },
            "enrich": {key: int(value or 0) for key, value in (enrichment.get("counts") or {}).items()},
            "linkedin": {
                "total": progress["linkedin_total"],
                "yes_or_no": progress["linkedin_done"],
                "pending": progress["linkedin_pending"],
            },
        }
        completed = [name for name in STAGES if not pending[name]]
        return {
            "stage": selected,
            "status": "completed" if selected == "done" or selected in completed else "awaiting_user",
            "counts": counts.get(selected, {}),
            "completed_stages": completed,
            "people_revision": state["selection"]["review_revision"],
            "updated_at": None,
            "review_csv": str(LINKEDIN_OVERRIDES_CSV),
            "synthetic_people_csv": str(DEFAULT_OUT),
            "privacy": {"message_bodies_read": False, "network_called": False, "paid_provider_called": False},
        }

    def enrichment(
        self, state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = state or self.snapshot()
        plan = research_selection.select_research(
            self.db,
            processor=research_selection.DEFAULT_PROCESSOR,
            confirm_threshold=self.confirm_threshold,
            include_candidates=True,
            include_plausibly_absent=True,
            fingerprint=state["selection"],
        )
        current_selection, pending, total = plan.fingerprint, len(plan.pending), len(plan.eligible)
        status = "completed" if not total else ("needs_approval" if pending else "not_started")
        route_state = "done" if not total else ("needs_approval" if pending else "free_pending")
        payload: dict[str, Any] = {
            **plan.result_base(0),
            "stage": "enrich",
            "status": status,
            "counts": {
                "total": total,
                "completed": plan.reused_completed,
                "pending": pending,
                "failed": 0,
            },
            "selection": current_selection,
            "current": True,
            "approval_current": False,
            "state": route_state,
            "approvable": bool(pending),
        }
        job = linkedin_review(self.db, "latest_job", job_kind="enrichment") or {}
        if job.get("selection_fingerprint") == current_selection["fingerprint"]:
            status = str(job.get("status") or "")
            try:
                result = json.loads(str(job.get("result_json") or "{}"))
            except json.JSONDecodeError:
                result = {}
            if isinstance(result, dict):
                payload.update(result)
            completed = int(job.get("completed_count") or 0)
            job_total = int(job.get("total_count") or total)
            payload["counts"] = {
                "total": job_total,
                "completed": completed,
                "pending": max(0, job_total - completed),
                "failed": 0,
            }
            if total and status in {"queued", "running"}:
                payload.update(status="running", state="running")
            elif status == "applied":
                payload.update(status="completed", state="done", approvable=False)
            elif total and status == "failed":
                payload["counts"]["failed"] = payload["counts"]["pending"]
                payload["counts"]["pending"] = 0
                payload.update(status="failed", state="failed", error=job.get("error"))
        return payload

    def set_worth(self, key: str, value: str, note: str = "") -> None:
        self.db.decide_worth(
            key.removeprefix(PARENT_WORTH_PREFIX),
            None if value == "restore" else value,
            note=note or None,
        )

    def decide(self, key: str, decision: str, new_url: str = "") -> tuple[dict[str, str], list[str]]:
        candidate = next(
            (row for row in identity_snapshot(self.db).review_rows if row.key == key),
            None,
        )
        if candidate is None:
            raise StoreError(f"review row not found: {key}")
        if decision == "reset":
            resolved = self.db.decide_identity(candidate.key, None)
            current = next(
                (
                    row
                    for row in identity_snapshot(self.db).review_rows
                    if row.key == candidate.key
                ),
                None,
            )
            return {
                name: str(getattr(current, source, "") or "")
                for name, source in (
                    ("action", "action"),
                    ("approved", "approved"),
                    ("new_url", "new_linkedin_url"),
                )
            }, resolved
        action = {
            "keep": "retarget" if candidate.new_linkedin_url else "verify",
            "detach": "detach",
            "fix": "retarget",
            "exclude": "exclude",
        }.get(decision)
        if action is None:
            raise StoreError(f"unknown decision: {decision}")
        replacement = (
            new_url
            if decision == "fix"
            else str(candidate.new_linkedin_url or "")
            if action == "retarget"
            else ""
        )
        kwargs: dict[str, str] = {}
        if action == "retarget":
            replacement = normalize_linkedin_url(replacement)
            if not replacement:
                raise StoreError("fix needs a LinkedIn URL")
            kwargs = {
                "replacement_url": replacement,
                "replacement_public_identifier": extract_public_identifier(replacement).lower(),
            }
        resolved = self.db.decide_identity(candidate.key, action, **kwargs)
        return {"action": action, "approved": "yes", "new_url": replacement}, resolved

    def approve_enrichment(self) -> dict[str, Any]:
        state = self.snapshot()
        enrichment = self.enrichment(state)
        if enrichment.get("status") in {
            "running", "submitted", "research_complete", "completed",
        }:
            return enrichment
        if enrichment.get("status") != "needs_approval":
            raise StoreError("Enrichment is not waiting for approval")
        expected_count = int(enrichment.get("would_submit") or 0)
        estimate = float(enrichment.get("estimated_usd") or 0)
        if expected_count <= 0:
            raise StoreError("No paid enrichment approval is required")
        if not math.isfinite(estimate) or estimate <= 0:
            raise StoreError("Enrichment estimate must be a positive finite amount")
        return {
            **enrichment,
            "approval": {
                "status": "approved",
                "approved_at": now_iso(),
                "approved_budget_usd": estimate,
                "estimated_usd": estimate,
                "would_submit": expected_count,
                "selection_fingerprint": state["selection"]["fingerprint"],
                "review_revision": state["selection"]["review_revision"],
            },
        }

    def retargets(self) -> list[dict[str, Any]]:
        rows = identity_snapshot(self.db).guidance
        return [
            row.get("detail")
            or {
                "slug": row.get("handle"),
                "row_key": row.get("candidate_key") or "",
                "guidance": row.get("guidance") or "",
                "state": row.get("state") or "",
                "detail": "",
                "submitted_at": row.get("submitted_at") or "",
                "updated_at": row.get("submitted_at") or "",
            }
            for row in reversed(rows)
        ]

    def resolve_row_key(self, value: str) -> str | None:
        """Resolve one external row key or public identifier at the HTTP edge."""
        resolved = resolve_identity_key(self.db, value)
        return resolved[0] if resolved else None

    def resolve_candidate(
        self, value: str,
    ) -> tuple[str, dict[str, Any]] | None:
        """Resolve one external identity key and hydrate its canonical parent once."""
        resolved = resolve_identity_key(self.db, value)
        if not resolved:
            return None
        row_key, parent_id = resolved
        parent = person_detail(self.db, parent_id)
        return (row_key, parent) if parent else None

    @staticmethod
    def candidate(parent: dict[str, Any] | None, row_key: str) -> dict[str, Any] | None:
        return next(
            (
                row
                for row in (parent or {}).get("candidates") or []
                if str(row.get("row_key") or "") == row_key
            ),
            None,
        )

    def avatar(self, key: str) -> tuple[bytes, str] | None:
        payload = avatar_payload(self.db, key) or {}
        try:
            body = base64.b64decode(str(payload.get("base64") or ""), validate=True)
        except (ValueError, TypeError):
            return None
        return (body, str(payload.get("content_type") or "application/octet-stream")) if body else None

    def workflow_status(
        self, *, job_running: bool = False, state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = dict(state or self.snapshot(job_running=job_running))
        enrichment = self.enrichment(payload)
        payload["review_manifest"] = self.manifest(
            state=payload, enrichment=enrichment,
        )
        payload["enrichment"] = enrichment
        return payload

    def status(self, *, job_running: bool = False) -> dict[str, Any]:
        workflow = self.snapshot(job_running=job_running)
        return {
            "primitive": "reconcile_review_web", "ok": True,
            "manifest": str(REVIEW_MANIFEST),
            "stage": STAGE_BY_ACTION[workflow["next_action"]],
            "next_action": workflow["next_action"],
            "state_token": workflow["state_token"],
        }

    def counts(self) -> dict[str, int]:
        parents = linkedin_review(self.db, "parents")
        candidates = [row for parent in parents for row in parent.get("candidates") or []]
        return {
            "parents": len(parents),
            "candidates": len(candidates),
            "pending": sum(bool(row.get("pending")) for row in candidates),
            "approved": sum(row.get("approved") in {"yes", "auto"} for row in candidates),
            "rejected": sum(row.get("approved") == "no" for row in candidates),
        }
