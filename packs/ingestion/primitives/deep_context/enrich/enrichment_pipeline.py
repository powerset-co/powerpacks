"""Run the approved enrichment stages once in the review-server process.

The process-local flag prevents double submission while this server is alive.
The fixed enrichment manifest holds progress and the Parallel provider receipt.
SQLite artifacts and the freshly selected plan own eligibility and result reuse.
"""

from __future__ import annotations

import threading
import sys
from typing import Callable

from packs.ingestion.primitives.deep_context.db.models import RESEARCH_CONFIRM_THRESHOLD
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich.profiles.prefetch import PrefetchProfiles
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.models import EnrichmentProgress
from packs.ingestion.primitives.deep_context.manifests.receipt_counts import ReceiptCounts
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.coordinator import (
    ReconcileDeepResearch,
)
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.judging import judge_mapped_candidates
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.selection import select_research
from packs.ingestion.primitives.deep_context.enrich.parallel_research.config import DEFAULT_PROCESSOR
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.relationship import ReviewRelationships
from packs.ingestion.primitives.deep_context.enrich.settle import SettleEnrichment
from packs.ingestion.primitives.deep_context.enrich.synthetic.assemble import (
    AssembleSyntheticProfile,
)
from packs.ingestion.primitives.deep_context.manifests.enrichment_receipt import (
    EnrichmentReceipt,
)
from packs.ingestion.primitives.deep_context.manifests.receipt_status import (
    RECONCILE_SUCCESS_STATUSES,
    ReceiptStatus,
)
from packs.ingestion.primitives.deep_context.shared.common import ENRICH_MANIFEST


class EnrichmentPipeline:
    """One approved research -> profile -> judge -> relationships -> settle -> synthetic chain."""

    def __init__(
        self,
        db: Db,
        confirm_threshold: float = RESEARCH_CONFIRM_THRESHOLD,
        *,
        on_change: Callable[[], None],
        on_finish: Callable[[], None],
    ) -> None:
        self.db = db
        self.confirm_threshold = confirm_threshold
        self.on_change = on_change
        self.on_finish = on_finish
        self.receipt = EnrichmentReceipt(ENRICH_MANIFEST)
        self._running = threading.Lock()
        # Last-written receipt payload; the server hands this to /api/events
        # subscribers as the SSE `job` field (None until a job has written).
        self.last_job: dict[str, object] | None = None
        # The most recent run's failure text, if any — in memory only; a
        # restart forgets it and the approve button returns.
        self.last_error: str | None = None
        # Fingerprint of the last chain that finished cleanly in this process.
        # The review view reads it to offer stage Continue instead of a $0
        # rerun of the cached chain; a restart forgets it and reruns once.
        self.applied_fingerprint: str | None = None

    def running(self) -> bool:
        return self._running.locked()

    def _write(
        self,
        status: str,
        request_fingerprint: str,
        total: int,
        budget: float,
        *,
        completed: int = 0,
        phase: str | None = None,
        progress: dict[str, object] | None = None,
        error: str | None = None,
        errors: tuple[str, ...] = (),
    ) -> None:
        completed = min(max(0, completed), total)
        failed = int(status == ReceiptStatus.FAILED)
        payload: dict[str, object] = {
            "stage": "enrich",
            "status": status,
            "request_fingerprint": request_fingerprint,
            "counts": {
                "total": total,
                "completed": completed,
                "pending": max(0, total - completed - failed),
                "failed": failed,
            },
            "approved_budget_usd": budget,
        }
        if phase:
            payload["phase"] = phase
        if progress:
            payload["progress"] = progress
        if error:
            payload["error"] = error[:500]
        if errors:
            payload["errors"] = list(errors)
        self.receipt.write(payload)
        # The last-written payload is the SSE job payload — the review UI's
        # live progress rides in on /api/events, no polling.
        self.last_job = payload

    def _run(
        self,
        budget: float,
        on_progress: Callable[[EnrichmentProgress], None],
    ) -> tuple[str, ...]:
        errors: list[str] = []
        research = ReconcileDeepResearch(
            db=self.db,
            approve=True,
            budget=round(budget, 2),
            on_progress=on_progress,
        ).run()
        if research.status == ReceiptStatus.FAILED:
            errors.extend(f"research: {error}" for error in research.errors)
            if not research.errors:
                errors.append(f"research: {research.message or research.reason or 'failed'}")
        elif research.status.value not in RECONCILE_SUCCESS_STATUSES:
            detail = "; ".join(research.errors) or research.message or research.reason
            raise RuntimeError(
                f"research stopped with status {research.status.value}"
                f"{f': {detail}' if detail else ''}"
            )
        profiles = PrefetchProfiles(db=self.db, fetch=True).run()
        if profiles.status != "completed":
            errors.append(f"profiles: {profiles.note or profiles.status}; details in SQLite profile artifacts")
        judged = judge_mapped_candidates(
            self.db,
            heartbeat=lambda done, total: on_progress(EnrichmentProgress(
                "judging_retargets", ReceiptCounts.create(total=total, completed=done), done, total,
            )),
        )
        if judged.judge_errors:
            errors.append(f"identity: {judged.judge_errors} candidate(s) deferred; see reconcile/identity/manifest.json")
        reviews = ReviewRelationships(db=self.db, approve_spend=True).run()
        if reviews["status"] != "completed":
            if reviews.get("errors"):
                errors.extend(f"relationships: {item['parent_id']}: {item['error']}" for item in reviews["errors"])
            else:
                errors.append(f"relationships: {reviews.get('error') or reviews['status']}")
        SettleEnrichment(db=self.db).run()
        AssembleSyntheticProfile(db=self.db).run()
        for error in errors:
            print(f"[enrichment] {error}", file=sys.stderr, flush=True)
        return tuple(errors)

    def start(self, total: int, budget: float, request_fingerprint: str) -> bool:
        if not self._running.acquire(blocking=False):
            return False
        try:
            self._write(
                ReceiptStatus.RUNNING,
                request_fingerprint,
                total,
                budget,
                phase="research",
            )
        except BaseException:
            self._running.release()
            raise

        def progress(event: EnrichmentProgress) -> None:
            self._write(
                ReceiptStatus.RUNNING,
                request_fingerprint,
                total,
                budget,
                completed=event.completed,
                phase=event.phase,
                progress=event.to_payload(),
            )
            self.on_change()

        def run() -> None:
            try:
                errors = self._run(budget, progress)
                self.last_error = None
                # Successful research leaves the queue; remaining failures belong
                # to this completed pass, not an immediate new approval request.
                self.applied_fingerprint = select_research(self.db, processor=DEFAULT_PROCESSOR).request_fingerprint
                self._write(
                    "completed",
                    request_fingerprint,
                    total,
                    budget,
                    completed=total,
                    phase="profiles_complete",
                    errors=errors,
                )
            except BaseException as exc:
                self.last_error = f"enrichment: {type(exc).__name__}: {exc}"
                self._write(
                    ReceiptStatus.FAILED,
                    request_fingerprint,
                    total,
                    budget,
                    error=self.last_error,
                )
            finally:
                self._running.release()
                self.on_change()
                self.on_finish()

        threading.Thread(target=run, name="pipeline-enrichment", daemon=True).start()
        return True
