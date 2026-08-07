"""Run the existing enrichment stages behind one async SQLite job receipt."""

from __future__ import annotations

import threading
from typing import Callable

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.enrich.assemble_synthetic_profile import AssembleSyntheticProfile
from packs.ingestion.primitives.deep_context.db.identity_views import latest_job
from packs.ingestion.primitives.deep_context.db.models import (
    JobKind,
    JobRow,
    JobStatus,
    RESEARCH_CONFIRM_THRESHOLD,
    IsoTimestamp,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich.prefetch_profiles import PrefetchProfiles
from packs.ingestion.primitives.deep_context.enrich.reconcile_deep_research import ReconcileDeepResearch
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.models import (
    ResearchProgressEvent,
)
from packs.ingestion.primitives.deep_context.review.models import (
    EnrichmentJobResult,
    EnrichmentProgress,
)

JOB_NAME = "review-web-enrichment"


class EnrichmentPipeline:
    def __init__(
        self, db: Db, confirm_threshold: float = RESEARCH_CONFIRM_THRESHOLD,
        *, on_change: Callable[[], None], on_finish: Callable[[], None],
    ) -> None:
        self.db, self.confirm_threshold = db, confirm_threshold
        self.on_change, self.on_finish = on_change, on_finish

    def _run(self, budget: float, on_progress: Callable[[EnrichmentProgress], None]) -> None:
        def provider_progress(event: ResearchProgressEvent) -> None:
            on_progress(EnrichmentProgress.from_event(event))

        ReconcileDeepResearch(
            db=self.db, approve=True, budget=round(budget, 2), on_progress=provider_progress,
            confirm_threshold=self.confirm_threshold,
            include_candidates=True, include_plausibly_absent=True,
        ).run()
        AssembleSyntheticProfile(db=self.db).run()
        PrefetchProfiles(db=self.db, fetch=True).run()

    def running(self) -> bool:
        job = latest_job(self.db, JobKind.ENRICHMENT.value)
        status = job.status if job else ""
        return status in {JobStatus.QUEUED.value, JobStatus.RUNNING.value}

    def _save(
        self, status: JobStatus, selection: str, total: int, budget: float,
        started_at: IsoTimestamp, *, completed: int = 0, error: str | None = None,
        result: EnrichmentJobResult | None = None, finished: bool = False,
    ) -> None:
        self.db.project_rows((JobRow(
            JOB_NAME, JobKind.ENRICHMENT.value, status.value,
            selection_fingerprint=selection, completed_count=min(completed, total),
            total_count=total, error=error,
            result_json=(result or EnrichmentJobResult(budget)).to_json(),
            started_at=started_at, finished_at=now_iso() if finished else None,
        ),))

    def start(self, total: int, budget: float, selection: str) -> bool:
        started_at = now_iso()
        if not self.db.start_job(JobRow(
            JOB_NAME, JobKind.ENRICHMENT.value, JobStatus.RUNNING.value,
            selection_fingerprint=selection, total_count=total,
            result_json=EnrichmentJobResult(budget).to_json(),
            started_at=started_at,
        )):
            return False

        def progress(payload: EnrichmentProgress) -> None:
            self._save(JobStatus.RUNNING, selection, total, budget, started_at,
                       completed=payload.completed,
                       result=EnrichmentJobResult(budget, payload.payload_json))
            self.on_change()

        def run() -> None:
            try:
                self._run(budget, progress)
                self._save(JobStatus.APPLIED, selection, total, budget, started_at,
                           completed=total,
                           result=EnrichmentJobResult(budget, status="completed"),
                           finished=True)
            except BaseException as exc:
                self._save(JobStatus.FAILED, selection, total, budget, started_at,
                           error=f"enrichment: {type(exc).__name__}: {exc}"[:500], finished=True)
            finally:
                self.on_change()
                self.on_finish()

        threading.Thread(target=run, name="pipeline-job-enrichment", daemon=True).start()
        return True
