"""Run enrichment from the CLI or the review server using saved stage outputs.

Changelog:
- 2026-10-01: the synchronous named chain owns progress and terminal receipts.
- 2026-10-01: the run records where it stands in SQLite, step by step, and what it left
  unfinished when it completes; the flow and the waiting screen read that record.
- 2026-10-01: a step that raises is run once more before the run fails.
"""

from __future__ import annotations

import threading
import sys
import time
from pathlib import Path
from typing import Callable

from packs.ingestion.primitives.deep_context.db.models import (
    RESEARCH_CONFIRM_THRESHOLD,
    EnrichRun,
    EnrichRunStatus,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.workflow_views import enrichment_work
from packs.ingestion.primitives.deep_context.enrich.profiles.prefetch import PrefetchProfiles
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.models import EnrichmentProgress
from packs.ingestion.primitives.deep_context.manifests.receipt_counts import ReceiptCounts
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.coordinator import (
    ReconcileDeepResearch,
)
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.judging import judge_mapped_candidates
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
from packs.ingestion.primitives.deep_context.merge_candidates.linkedin_name_matches import apply_linkedin_name_matches
from packs.ingestion.primitives.deep_context.synthesis.normalization import normalize_parent_cache


# A step that raises is run once more after this wait: every step skips what is already stored,
# and what stops one is most often a dropped connection.
STEP_RETRY_SECONDS = 5


class ResearchStopped(RuntimeError):
    """Research declined to run (no approval, a bad budget): running it again changes nothing."""


class EnrichmentPipeline:
    """One approved research -> profile -> judge -> relationships -> settle -> synthetic chain."""

    def __init__(
        self,
        db: Db,
        confirm_threshold: float = RESEARCH_CONFIRM_THRESHOLD,
        *,
        on_change: Callable[[], None] | None = None,
        on_finish: Callable[[], None] | None = None,
        manifest: Path | None = None,
    ) -> None:
        self.db = db
        self.confirm_threshold = confirm_threshold
        self.on_change = on_change
        self.on_finish = on_finish
        self.receipt = EnrichmentReceipt(manifest if manifest is not None else ENRICH_MANIFEST)
        self._running = threading.Lock()
        # Last-written receipt payload; the server hands this to /api/events
        # subscribers as the SSE `job` field (None until a job has written).
        self.last_job: dict[str, object] | None = None
        # The most recent run's failure text, if any — in memory only; a
        # restart forgets it and the approve button returns.
        self.last_error: str | None = None

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
        steps: tuple[str, ...] = (),
    ) -> dict[str, object]:
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
            "steps": list(steps),
            "errors": list(errors),
        }
        if phase:
            payload["phase"] = phase
        if progress:
            payload["progress"] = progress
        if error:
            payload["error"] = error[:500]
        self.receipt.write(payload)
        # The last-written payload is the SSE job payload — the review UI's
        # live progress rides in on /api/events, no polling.
        self.last_job = payload
        return payload

    def _research(
        self,
        budget: float,
        on_progress: Callable[[EnrichmentProgress], None],
    ) -> tuple[str, ...]:
        errors: list[str] = []
        research = ReconcileDeepResearch(
            db=self.db,
            out_dir=self.receipt.path.parent,
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
            raise ResearchStopped(
                f"research stopped with status {research.status.value}"
                f"{f': {detail}' if detail else ''}"
            )
        return tuple(errors)

    def _profiles(self) -> tuple[str, ...]:
        profiles = PrefetchProfiles(db=self.db, fetch=True).run()
        if profiles.status != "completed":
            return (f"profiles: {profiles.note or profiles.status}; details in SQLite profile artifacts",)
        return ()

    def _identity(self, on_progress: Callable[[EnrichmentProgress], None]) -> tuple[str, ...]:
        judged = judge_mapped_candidates(
            self.db,
            heartbeat=lambda done, total: on_progress(EnrichmentProgress(
                "judging_retargets", ReceiptCounts.create(total=total, completed=done), done, total,
            )),
        )
        if judged.judge_errors:
            return (f"identity: {judged.judge_errors} candidate(s) deferred; see reconcile/identity/manifest.json",)
        return ()

    def _relationships(self) -> tuple[str, ...]:
        errors: list[str] = []
        reviews = ReviewRelationships(db=self.db, approve_spend=True).run()
        if reviews["status"] != "completed":
            if reviews.get("errors"):
                errors.extend(f"relationships: {item['parent_id']}: {item['error']}" for item in reviews["errors"])
            else:
                errors.append(f"relationships: {reviews.get('error') or reviews['status']}")
        return tuple(errors)

    def _settle(self) -> tuple[str, ...]:
        SettleEnrichment(db=self.db).run()
        return ()

    def _synthetic(self) -> tuple[str, ...]:
        AssembleSyntheticProfile(db=self.db).run()
        return ()

    def steps(
        self, budget: float, on_progress: Callable[[EnrichmentProgress], None],
    ) -> tuple[tuple[str, Callable[[], tuple[str, ...]]], ...]:
        """The ordered chain shared by synchronous and server runs."""
        return (
            ("research", lambda: self._research(budget, on_progress)),
            ("profiles", self._profiles),
            ("identity", lambda: self._identity(on_progress)),
            ("relationships", self._relationships),
            ("settle", self._settle),
            ("synthetic", self._synthetic),
        )

    def _attempt(self, phase: str, step: Callable[[], tuple[str, ...]]) -> tuple[str, ...]:
        """Run one step, and once more if it raises; the errors it reports."""
        try:
            return step()
        except ResearchStopped:
            raise
        except Exception as exc:
            again = f"{phase}: ran again after {type(exc).__name__}: {exc}"[:300]
            print(f"[enrich] {again}", file=sys.stderr, flush=True)
            time.sleep(STEP_RETRY_SECONDS)
            return (again, *step())

    def run(self, *, total: int, budget: float, request_fingerprint: str) -> dict[str, object]:
        """Run every step; SQLite and stage outputs decide what needs work."""
        if apply_linkedin_name_matches(self.db):
            normalize_parent_cache(self.db, raw_dir=self.db.db_path.parent / 'raw', facts_dir=self.db.db_path.parent / 'facts')
        errors: list[str] = []
        steps: list[str] = []
        phase = "research"

        def progress(event: EnrichmentProgress) -> None:
            self._write(
                ReceiptStatus.RUNNING, request_fingerprint, total, budget,
                completed=event.completed, phase=phase, progress=event.to_payload(),
                errors=tuple(errors), steps=tuple(steps),
            )
            if self.on_change:
                self.on_change()

        for phase, step in self.steps(budget, progress):
            steps.append(phase)
            self.db.record_enrich_run(EnrichRun(EnrichRunStatus.RUNNING, phase, tuple(errors)))
            self._write(
                ReceiptStatus.RUNNING, request_fingerprint, total, budget,
                phase=phase, errors=tuple(errors), steps=tuple(steps),
            )
            print(f"[enrich] {phase}", file=sys.stderr, flush=True)
            if self.on_change:
                self.on_change()
            try:
                errors.extend(self._attempt(phase, step))
            except BaseException as exc:
                self.last_error = f"enrichment: {type(exc).__name__}: {exc}"
                self.db.record_enrich_run(
                    EnrichRun(EnrichRunStatus.FAILED, phase, (*errors, self.last_error))
                )
                self._write(
                    ReceiptStatus.FAILED, request_fingerprint, total, budget,
                    phase=phase, error=self.last_error, errors=tuple(errors),
                    steps=tuple(steps),
                )
                raise

        self.last_error = None
        # Whatever is still left was tried and did not finish; it no longer holds the flow.
        self.db.record_enrich_run(
            EnrichRun(EnrichRunStatus.COMPLETED, phase, tuple(errors), enrichment_work(self.db))
        )
        payload = self._write(
            "completed", request_fingerprint, total, budget, completed=total,
            phase=phase, errors=tuple(errors), steps=tuple(steps),
        )
        for error in errors:
            print(f"[enrich] {error}", file=sys.stderr, flush=True)
        return payload

    def start(self, total: int, budget: float, request_fingerprint: str) -> bool:
        if not self._running.acquire(blocking=False):
            return False

        def run() -> None:
            try:
                self.run(total=total, budget=budget, request_fingerprint=request_fingerprint)
            except BaseException:
                pass  # run() has already written the failed step and error.
            finally:
                self._running.release()
                if self.on_change:
                    self.on_change()
                if self.on_finish:
                    self.on_finish()

        threading.Thread(target=run, name="pipeline-enrichment", daemon=True).start()
        return True
