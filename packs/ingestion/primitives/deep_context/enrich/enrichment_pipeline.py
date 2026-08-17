"""Run the approved enrichment stages once in the review-server process.

The process-local flag prevents double submission while this server is alive.
The fixed enrichment manifest is display-only progress; SQLite artifacts and
the freshly selected research plan own eligibility, reuse, and resume.
"""

from __future__ import annotations

import threading
from collections.abc import Collection
from typing import Callable

from packs.ingestion.primitives.deep_context.db.models import RESEARCH_CONFIRM_THRESHOLD
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich.profiles.prefetch import PrefetchProfiles
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.models import (
    ResearchProgressEvent,
)
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.reconcile_deep_research import (
    ReconcileDeepResearch,
)
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
    """One approved research -> synthetic -> profile chain."""

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
        self.receipt.write(payload)

    @staticmethod
    def _require(stage: str, payload: dict[str, object], accepted: Collection[str]) -> None:
        status = str(payload.get("status") or "")
        if status not in accepted:
            detail = payload.get("error") or payload.get("message") or payload.get("note")
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(f"{stage} stopped with status {status or 'missing'}{suffix}")

    def _run(
        self,
        budget: float,
        on_progress: Callable[[ResearchProgressEvent], None],
    ) -> None:
        research = ReconcileDeepResearch(
            db=self.db,
            approve=True,
            budget=round(budget, 2),
            on_progress=on_progress,
            confirm_threshold=self.confirm_threshold,
            include_candidates=True,
            include_plausibly_absent=True,
        ).run()
        self._require(
            "research",
            research,
            RECONCILE_SUCCESS_STATUSES,
        )
        synthetic = AssembleSyntheticProfile(db=self.db).execute()
        self._require("synthetic assembly", synthetic, {"completed"})
        profiles = PrefetchProfiles(db=self.db, fetch=True).run()
        self._require("profile prefetch", profiles, {"completed"})

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

        def progress(event: ResearchProgressEvent) -> None:
            self._write(
                ReceiptStatus.RUNNING,
                request_fingerprint,
                total,
                budget,
                completed=event.completed,
                phase=str(event.to_payload().get("phase") or "research"),
                progress=event.to_payload(),
            )
            self.on_change()

        def run() -> None:
            try:
                self._run(budget, progress)
                self._write(
                    "completed",
                    request_fingerprint,
                    total,
                    budget,
                    completed=total,
                    phase="profiles_complete",
                )
            except BaseException as exc:
                self._write(
                    ReceiptStatus.FAILED,
                    request_fingerprint,
                    total,
                    budget,
                    error=f"enrichment: {type(exc).__name__}: {exc}",
                )
            finally:
                self._running.release()
                self.on_change()
                self.on_finish()

        threading.Thread(target=run, name="pipeline-enrichment", daemon=True).start()
        return True
