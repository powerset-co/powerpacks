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
from typing import Any, Callable

from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.ingestion.primitives.deep_context.common import (
    DEEP_RESEARCH_DIR,
    PROFILE_CACHE_DIR,
)
from packs.ingestion.primitives.deep_context.db.people_views import person_detail
from packs.ingestion.primitives.deep_context.db.models import GuidanceState, RESEARCH_CONFIRM_THRESHOLD
from packs.ingestion.primitives.deep_context.db.snapshots import identity_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.identity_reconcile.guided import GuidedResearch
from packs.ingestion.primitives.deep_context.identity_reconcile.guidance import (
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
        runner: Callable[[GuidanceRequest], dict[str, Any]] | None = None,
        on_change: Callable[[], None] | None = None,
        research_dir: Path = DEEP_RESEARCH_DIR,
        profile_cache_dir: Path = PROFILE_CACHE_DIR,
        use_llm: bool = True,
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
            use_llm,
            model,
            reasoning_effort,
            confirm_threshold,
        )
        self.runner = runner or self.service.research
        self._thread: threading.Thread | None = None
        self._pending: list[GuidanceRequest] = []

    def submit(self, request: GuidanceRequest) -> dict[str, Any]:
        parent = person_detail(self.db, request.queue_slug or request.slug)
        if not parent:
            raise StoreError(f"person not found: {request.queue_slug or request.slug}")
        parent_id = str(parent["parent_id"])
        active = any(
            row.get("handle") == parent_id
            and row.get("state") in ACTIVE_GUIDANCE_STATES
            for row in identity_snapshot(self.db).guidance
        )
        if active:
            raise StoreError(f"{request.name or request.slug} is already being retargeted")
        url, public_identifier = linkedin_url_in_guidance(request.guidance)
        if url:
            resolved = self.db.decide_identity(
                request.pub,
                "retarget",
                replacement_url=url,
                replacement_public_identifier=public_identifier,
            )
            item = self.service.record(
                parent_id, request, GuidanceState.APPLIED, "applied",
                "user-provided LinkedIn applied directly",
                new_url=url, resolved_pubs=resolved,
            )
            self.on_change()
            return item
        item = self.service.record(
            parent_id, request, GuidanceState.PENDING, "queued"
        )
        self._enqueue(request)
        return item

    def resume(self) -> int:
        resumed = 0
        for row in identity_snapshot(self.db).guidance:
            if row.get("state") not in ACTIVE_GUIDANCE_STATES:
                continue
            detail = row.get("detail") or {}
            request_data = detail.get("request") if isinstance(detail, dict) else None
            if not isinstance(request_data, dict):
                continue
            values = dict(request_data)
            for key in ("person_ids", "match_emails", "match_phones"):
                values[key] = tuple(values.get(key) or ())
            request = GuidanceRequest(**values)
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
            parent = person_detail(self.db, request.queue_slug or request.slug)
            if not parent:
                continue
            parent_id = str(parent["parent_id"])
            self.service.record(
                parent_id, request, GuidanceState.RUNNING,
                "researching", "Parallel research running",
            )
            self.on_change()
            try:
                result = self.runner(request)
                self.service.apply_provider_result(parent_id, parent, request, result)
            except BaseException as exc:
                self.service.record(
                    parent_id, request, GuidanceState.FAILED, "failed",
                    f"{type(exc).__name__}: {exc}"[:500],
                )
            self.on_change()
