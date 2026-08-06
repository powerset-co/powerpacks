"""SQLite adapter for the frozen Deep Context HTTP contract."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db import views
from packs.ingestion.primitives.deep_context.db.models import (
    SpendApprovalRow,
    StageStateRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url

STAGES = ("worth", "enrich", "linkedin")
NO_FILES = Path("/__powerpacks_sqlite_no_files__")


def _image_type(body: bytes) -> str:
    signatures = (
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
    )
    for signature, content_type in signatures:
        if body.startswith(signature):
            return content_type
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return "image/webp"
    if len(body) >= 12 and body[4:12] in {b"ftypavif", b"ftypavis"}:
        return "image/avif"
    return "application/octet-stream"


@dataclass
class SqliteReviewAdapter:
    db: Db
    review_path: Path
    synthetic_path: Path
    manifest_path: Path

    def parents(self) -> list[dict[str, Any]]:
        return views.all_parents(self.db)

    def progress(self) -> dict[str, int]:
        return views.stage_progress(self.db)

    def selection(self) -> dict[str, Any]:
        return views.review_selection(self.db)

    def manifest(self, stage: str | None = None) -> dict[str, Any]:
        return {
            **views.review_state(self.db, stage),
            "review_csv": str(self.review_path),
            "synthetic_people_csv": str(self.synthetic_path),
            "privacy": {"message_bodies_read": False, "network_called": False, "paid_provider_called": False},
        }

    def enrichment(self) -> dict[str, Any]:
        return views.enrichment_state(self.db)

    def phase_completed(self, stage: str) -> bool:
        return stage in views.review_state(self.db)["completed_stages"]

    def save_stage(self, stage: str, complete: bool) -> dict[str, Any]:
        if stage not in STAGES:
            raise StoreError(f"unknown review stage: {stage}")
        if stage == "enrich":
            enrichment = self.enrichment()
            if enrichment.get("status") != "completed" or not enrichment.get("current"):
                raise StoreError("Enrichment is not complete for the current People decisions")
        complete = complete or self.phase_completed(stage)
        now = now_iso()
        self.db.save_stage(
            StageStateRow(
                stage,
                "complete" if complete else "pending",
                self.selection()["sha256"],
                completed_at=now if complete else None,
                updated_at=now,
            )
        )
        return self.manifest(stage)

    def set_worth(self, key: str, value: str, note: str = "") -> None:
        if value == "restore":
            views.reset_worth(self.db, key)
        else:
            views.set_worth(self.db, key, value, note=note or None)

    def decide(self, key: str, decision: str, new_url: str = "") -> tuple[dict[str, str], list[str]]:
        parent = self.parent_for_candidate(key)
        candidate = self.candidate(parent, key)
        if candidate is None:
            raise StoreError(f"review row not found: {key}")
        if decision == "reset":
            resolved = views.reset_identity(self.db, candidate["row_key"])
            current = self.candidate(self.parent_for_candidate(candidate["row_key"]), candidate["row_key"]) or {}
            return {
                name: str(current.get(source) or "")
                for name, source in (("action", "action"), ("approved", "approved"), ("new_url", "new_url"))
            }, resolved
        action = {
            "keep": "retarget" if candidate.get("new_url") else "verify",
            "detach": "detach",
            "fix": "retarget",
            "exclude": "exclude",
        }.get(decision)
        if action is None:
            raise StoreError(f"unknown decision: {decision}")
        replacement = (
            new_url if decision == "fix" else str(candidate.get("new_url") or "") if action == "retarget" else ""
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
        resolved = views.settle_identity(self.db, candidate["row_key"], action, **kwargs)
        return {"action": action, "approved": "yes", "new_url": replacement}, resolved

    def approve_enrichment(self) -> dict[str, Any]:
        enrichment = self.enrichment()
        if not enrichment.get("current"):
            raise StoreError("Enrichment preview is stale; refresh the preview before approving")
        if enrichment.get("status") in {"running", "submitted", "research_complete", "completed"}:
            return enrichment
        if enrichment.get("status") != "needs_approval":
            raise StoreError("Enrichment is not waiting for approval")
        count, estimate = (
            int(enrichment.get("would_submit") or 0),
            round(float(enrichment.get("estimated_usd") or 0), 2),
        )
        if count <= 0:
            raise StoreError("No paid enrichment approval is required")
        if not math.isfinite(estimate) or estimate <= 0:
            raise StoreError("Enrichment estimate must be a positive finite amount")
        self.db.approve_spend(SpendApprovalRow("enrich", self.selection()["sha256"], count, estimate, now_iso()))
        return self.enrichment()

    def retargets(self) -> list[dict[str, Any]]:
        rows = views.retarget_snapshot(self.db)["guidance"]
        return [
            row.get("detail")
            or {
                "slug": row.get("handle"),
                "pub": row.get("candidate_key") or "",
                "guidance": row.get("guidance") or "",
                "state": row.get("state") or "",
                "detail": "",
                "submitted_at": row.get("submitted_at") or "",
                "updated_at": row.get("submitted_at") or "",
            }
            for row in reversed(rows)
        ]

    def parent(self, key: str) -> dict[str, Any] | None:
        return views.person_detail(self.db, key)

    def parent_for_candidate(self, key: str, slug: str = "") -> dict[str, Any] | None:
        if slug:
            parent = self.parent(slug)
            if self.candidate(parent, key):
                return parent
        return next((parent for parent in self.parents() if self.candidate(parent, key)), None)

    @staticmethod
    def candidate(parent: dict[str, Any] | None, key: str) -> dict[str, Any] | None:
        key = key.strip().lower()
        return next(
            (
                row
                for row in (parent or {}).get("candidates") or []
                if key in {str(row.get("row_key") or "").lower(), str(row.get("pub") or "").lower()}
            ),
            None,
        )

    @staticmethod
    def _path(value: str | None) -> Path | None:
        path = Path(value) if value else None
        return path if path and path.is_absolute() else None

    def dossier_markdown(self, key: str) -> str:
        path = self._path(views.dossier_path(self.db, key))
        try:
            return path.read_text(encoding="utf-8") if path else ""
        except OSError:
            return ""

    def avatar(self, key: str) -> tuple[bytes, str] | None:
        path = self._path(views.avatar_path(self.db, key))
        try:
            body = path.read_bytes() if path else b""
        except OSError:
            body = b""
        return (body, _image_type(body)) if body else None

    def render_dirs(self, parent: dict[str, Any]) -> tuple[Path, Path, dict[str, Any]]:
        copy = dict(parent)
        path = self._path(views.dossier_path(self.db, str(parent.get("parent_id") or parent.get("slug") or "")))
        if path and path.suffix.lower() == ".md":
            copy.update({"slug": path.stem, "dossier_slug": path.stem})
            return path.parent, NO_FILES, copy
        return NO_FILES, NO_FILES, copy

    def workflow_status(self, *, job_running: bool = False) -> dict[str, Any]:
        payload = views.workflow_state(self.db, job_running=job_running)
        payload["review_manifest"] = self.manifest()
        return payload

    def status(self, *, job_running: bool = False) -> dict[str, Any]:
        workflow = self.workflow_status(job_running=job_running)
        stage = {
            "review_people": "worth",
            "review_linkedin": "linkedin",
            "finish_linkedin": "linkedin",
            "realize": "done",
        }.get(workflow["next_action"], "enrich")
        return {
            "primitive": "reconcile_review_web",
            "ok": True,
            "manifest": str(self.manifest_path),
            "stage": stage,
            "next_action": workflow["next_action"],
            "state_token": workflow["state_token"],
        }

    def counts(self) -> dict[str, int]:
        parents = self.parents()
        candidates = [row for parent in parents for row in parent.get("candidates") or []]
        return {
            "parents": len(parents),
            "candidates": len(candidates),
            "pending": sum(not row.get("resolved") for row in candidates),
            "approved": sum(row.get("approved") in {"yes", "auto"} for row in candidates),
            "rejected": sum(row.get("approved") == "no" for row in candidates),
        }
