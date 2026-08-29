"""Run the existing enrichment stages behind one async SQLite job receipt."""

from __future__ import annotations

import json
import threading
from typing import Any, Callable

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.assemble_synthetic_profile import AssembleSyntheticProfile
from packs.ingestion.primitives.deep_context.db.identity_views import linkedin_review
from packs.ingestion.primitives.deep_context.db.models import (
    JobKind,
    JobRow,
    JobStatus,
    RESEARCH_CONFIRM_THRESHOLD,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.prefetch_profiles import PrefetchProfiles
from packs.ingestion.primitives.deep_context.reconcile_deep_research import ReconcileDeepResearch

JOB_NAME = "review-web-enrichment"


class EnrichmentPipeline:
    def __init__(
        self, db: Db, confirm_threshold: float = RESEARCH_CONFIRM_THRESHOLD,
        *, on_change: Callable[[], None], on_finish: Callable[[], None],
    ) -> None:
        self.db, self.confirm_threshold = db, confirm_threshold
        self.on_change, self.on_finish = on_change, on_finish

    def _run(self, budget: float, on_progress: Callable[[dict[str, Any]], None]) -> None:
        ReconcileDeepResearch(
            db=self.db, approve=True, budget=round(budget, 2), on_progress=on_progress,
            confirm_threshold=self.confirm_threshold,
            include_candidates=True, include_plausibly_absent=True,
        ).run()
        AssembleSyntheticProfile(db=self.db).run()
        PrefetchProfiles(db=self.db, fetch=True).run()

    def running(self) -> bool:
        status = str((linkedin_review(
            self.db, "latest_job", job_kind=JobKind.ENRICHMENT.value,
        ) or {}).get("status") or "")
        return status in {JobStatus.QUEUED.value, JobStatus.RUNNING.value}

    def _save(
        self, status: JobStatus, selection: str, total: int, budget: float,
        started_at: str, *, completed: int = 0, error: str | None = None,
        result: dict[str, Any] | None = None, finished: bool = False,
    ) -> None:
        self.db.project_rows((JobRow(
            JOB_NAME, JobKind.ENRICHMENT.value, status.value,
            selection_fingerprint=selection, completed_count=min(completed, total),
            total_count=total, error=error,
            result_json=json.dumps({"approved_budget_usd": budget, **(result or {})},
                                   separators=(",", ":")),
            started_at=started_at, finished_at=now_iso() if finished else None,
        ),))

    def start(self, total: int, budget: float, selection: str) -> bool:
        started_at = now_iso()
        if not self.db.start_job(JobRow(
            JOB_NAME, JobKind.ENRICHMENT.value, JobStatus.RUNNING.value,
            selection_fingerprint=selection, total_count=total,
            result_json=json.dumps({"approved_budget_usd": budget}, separators=(",", ":")),
            started_at=started_at,
        )):
            return False

        def progress(payload: dict[str, Any]) -> None:
            counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
            completed = int(counts.get("completed") or counts.get("done") or 0)
            self._save(JobStatus.RUNNING, selection, total, budget, started_at,
                       completed=completed, result={"progress": payload})
            self.on_change()

        def run() -> None:
            try:
                self._run(budget, progress)
                self._save(JobStatus.APPLIED, selection, total, budget, started_at,
                           completed=total, result={"status": "completed"}, finished=True)
            except BaseException as exc:
                self._save(JobStatus.FAILED, selection, total, budget, started_at,
                           error=f"enrichment: {type(exc).__name__}: {exc}"[:500], finished=True)
            finally:
                self.on_change()
                self.on_finish()

        threading.Thread(target=run, name="pipeline-job-enrichment", daemon=True).start()
        return True
