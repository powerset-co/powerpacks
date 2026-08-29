"""Durable SQLite queue over the canonical identity-research contract.

Guided research differs from ordinary enrichment by one optional input: the
user's words.  The provider still receives the same canonical dossier/identity
packet, writes to the same fixed per-handle research directory, and therefore
reuses the same paid result when dossier plus guidance are unchanged.  Guidance
state in SQLite is the only queue/progress record; this module does not create a
second job row or enrichment manifest.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.ingestion.primitives.deep_context.shared.common import (
    DEEP_RESEARCH_DIR,
    PROFILE_CACHE_DIR,
)
from packs.ingestion.primitives.deep_context.db.people_views import person_detail
from packs.ingestion.primitives.deep_context.db.models import (
    GuidanceRequestSnapshot,
    GuidanceState,
    RESEARCH_CONFIRM_THRESHOLD,
    ReviewSource,
)
from packs.ingestion.primitives.deep_context.db.identity_queries import (
    guidance_rows,
    parent_has_contact_identifier,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.guided import (
    GuidanceOutcome,
    GuidedResearch,
)
from packs.ingestion.primitives.deep_context.enrich.parallel_research.result import ResearchResult
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.guidance import (
    ACTIVE_GUIDANCE_STATES,
    GuidanceRequest,
    linkedin_url_in_guidance,
)


class GuidedRetargetWorker:
    """Serial worker whose queue, progress, and result survive server restarts."""

    def __init__(
        self,
        db: Db,
        *,
        runner: Callable[[GuidanceRequest], ResearchResult] | None = None,
        on_change: Callable[[], None] | None = None,
        research_dir: Path = DEEP_RESEARCH_DIR,
        profile_cache_dir: Path = PROFILE_CACHE_DIR,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = "medium",
        confirm_threshold: float = RESEARCH_CONFIRM_THRESHOLD,
    ) -> None:
        self.db = db
        self.on_change = on_change or (lambda: None)
        self.service = GuidedResearch(
            db,
            Path(research_dir),
            Path(profile_cache_dir),
            model,
            reasoning_effort,
            confirm_threshold,
        )
        self.runner = runner or self.service.research
        self._thread: threading.Thread | None = None
        self._pending: list[GuidanceRequest] = []

    def submit(self, request: GuidanceRequest) -> GuidanceOutcome:
        parent = person_detail(self.db, request.slug)
        if not parent:
            raise StoreError(f"person not found: {request.slug}")
        parent_id = parent.parent_id
        active = any(row.handle == parent_id and row.state in ACTIVE_GUIDANCE_STATES for row in guidance_rows(self.db))
        if active:
            raise StoreError(f"{request.name or request.slug} is already being retargeted")
        url, public_identifier = linkedin_url_in_guidance(request.guidance)
        if url:
            resolved = self.db.decide_identity(
                request.row_key,
                "retarget",
                replacement_url=url,
                replacement_public_identifier=public_identifier,
                source=ReviewSource.USER_GUIDANCE.value,
            )
            item = self.service.record(
                parent_id,
                request,
                GuidanceState.APPLIED,
                "applied",
                "user-provided LinkedIn applied directly",
                new_url=url,
                resolved_pubs=resolved,
            )
            self.on_change()
            return item
        # URL-less guidance means paid research addressed by a contact
        # identifier, and a research subject only exists because a message
        # channel discovered it — so a parent with no email/phone on file has
        # nothing to research from. Reject the save here, at the one intake
        # door, mirroring the enrichment-queue view's identifier source.
        if not parent_has_contact_identifier(self.db, parent_id):
            raise StoreError(
                f"nothing to research from: {request.name or request.slug} has no "
                "email or phone on file — paste a LinkedIn URL instead"
            )
        item = self.service.record(parent_id, request, GuidanceState.PENDING, "queued")
        self._enqueue(request)
        return item

    def resume(self) -> int:
        resumed = 0
        for row in guidance_rows(self.db):
            if row.state not in ACTIVE_GUIDANCE_STATES:
                continue
            request_row: GuidanceRequestSnapshot | None = row.detail.request if row.detail else None
            if request_row is None:
                continue
            request = GuidanceRequest(
                slug=request_row.slug,
                row_key=request_row.row_key,
                name=request_row.name,
                guidance=request_row.guidance,
                person_ids=request_row.person_ids,
                linkedin_url=request_row.linkedin_url,
                submitted_at=request_row.submitted_at,
                match_emails=request_row.match_emails,
                match_phones=request_row.match_phones,
            )
            self._enqueue(request)
            resumed += 1
        return resumed

    def _enqueue(self, request: GuidanceRequest) -> None:
        self._pending.append(request)
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._drain,
            name="guided-retarget",
            daemon=True,
        )
        self._thread.start()
        self.on_change()

    def _drain(self) -> None:
        while True:
            if not self._pending:
                self._thread = None
                return
            request = self._pending.pop(0)
            parent = person_detail(self.db, request.slug)
            if not parent:
                continue
            parent_id = parent.parent_id
            self.service.record(
                parent_id,
                request,
                GuidanceState.RUNNING,
                "researching",
                "Parallel research running",
            )
            self.on_change()
            try:
                result = self.runner(request)
                self.service.apply_provider_result(parent_id, parent, request, result)
            except BaseException as exc:
                self.service.record(
                    parent_id,
                    request,
                    GuidanceState.FAILED,
                    "failed",
                    f"{type(exc).__name__}: {exc}"[:500],
                )
            self.on_change()
