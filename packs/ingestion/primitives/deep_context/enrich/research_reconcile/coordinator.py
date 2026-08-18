"""Construct and run one cost-gated Parallel research and identity pass."""

from __future__ import annotations

import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.ingestion.primitives.deep_context.db.models import RESEARCH_CONFIRM_THRESHOLD
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich.parallel_research import config, driver
from packs.ingestion.primitives.deep_context.enrich.parallel_research.models import (
    ResearchRunParams,
    ResearchRunResult,
)
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.judging import (
    propose_retargets,
)
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.models import (
    EnrichmentProgress,
    ResearchOutcome,
    ResearchSelection,
    RetargetRunResult,
)
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.selection import (
    select_research,
)
from packs.ingestion.primitives.deep_context.manifests.receipt_counts import ReceiptCounts
from packs.ingestion.primitives.deep_context.manifests.receipt_status import ReceiptStatus
from packs.ingestion.primitives.deep_context.shared.common import DEEP_RESEARCH_DIR
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import owner_background


@dataclass(frozen=True)
class ReconcileDeepResearch:
    """One select -> consent -> research -> judge stage."""

    db: Db
    out_dir: Path = DEEP_RESEARCH_DIR
    processor: str = config.DEFAULT_PROCESSOR
    confirm_threshold: float = RESEARCH_CONFIRM_THRESHOLD
    budget: float = 0.0
    approve: bool = False
    dry_run: bool = False
    include_plausibly_absent: bool = False
    include_candidates: bool = False
    model: str = DEFAULT_MODEL
    reasoning_effort: str = "medium"
    on_progress: Callable[[EnrichmentProgress], None] | None = None

    def _outcome(
        self,
        status: ReceiptStatus,
        plan: ResearchSelection | None,
        started: float,
        *,
        counts: ReceiptCounts | None = None,
        proposals: RetargetRunResult | None = None,
        errors: tuple[str, ...] = (),
        reason: str | None = None,
        message: str | None = None,
    ) -> ResearchOutcome:
        total = plan.deduped_total if plan else 0
        return ResearchOutcome(
            status=status,
            counts=counts or ReceiptCounts.create(total=total),
            plan=plan,
            budget_usd=self.budget,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            proposals=proposals,
            errors=errors,
            reason=reason,
            message=message,
            output_dir=str(self.out_dir) if plan else None,
        )

    def _progress(
        self,
        phase: str,
        counts: ReceiptCounts,
        *,
        phase_done: int = 0,
        phase_total: int = 0,
    ) -> None:
        if self.on_progress:
            self.on_progress(
                EnrichmentProgress(phase, counts, phase_done, phase_total)
            )

    def run(self) -> ResearchOutcome:
        started = time.monotonic()
        if not math.isfinite(self.budget) or self.budget < 0:
            message = "--budget must be a finite, non-negative USD amount"
            return self._outcome(
                ReceiptStatus.INVALID_BUDGET,
                None,
                started,
                errors=(message,),
                message=message,
            )

        plan = select_research(
            self.db,
            processor=self.processor,
            confirm_threshold=self.confirm_threshold,
            include_plausibly_absent=self.include_plausibly_absent,
            include_candidates=self.include_candidates,
        )
        self.out_dir.mkdir(parents=True, exist_ok=True)
        total = plan.deduped_total
        research_completed = plan.reused_completed

        def provider_progress(local: ReceiptCounts) -> None:
            counts = ReceiptCounts.create(
                total=total,
                completed=plan.reused_completed + local.completed,
                failed=local.failed,
            )
            self._progress(
                "research",
                counts,
                phase_done=local.completed,
                phase_total=local.total,
            )

        def heartbeat(done: int, judge_total: int) -> None:
            self._progress(
                "judging_retargets",
                ReceiptCounts.create(total=total, completed=research_completed),
                phase_done=done,
                phase_total=judge_total,
            )

        def propose() -> RetargetRunResult:
            return propose_retargets(
                plan.eligible,
                db=self.db,
                owner_block=owner_background(self.db),
                model=self.model,
                effort=self.reasoning_effort,
                confirm_threshold=self.confirm_threshold,
                heartbeat=heartbeat,
            )

        if not plan.eligible:
            return self._outcome(
                ReceiptStatus.NOOP,
                plan,
                started,
                reason="no effective-Yes contacts need enrichment",
            )
        if self.dry_run:
            return self._outcome(
                ReceiptStatus.DRY_RUN,
                plan,
                started,
                counts=ReceiptCounts.create(
                    total=total,
                    completed=plan.reused_completed,
                ),
            )
        if not self.approve or plan.estimated_usd > self.budget:
            return self._outcome(
                ReceiptStatus.NEEDS_APPROVAL,
                plan,
                started,
                counts=ReceiptCounts.create(
                    total=total,
                    completed=plan.reused_completed,
                ),
                message=(
                    f"deep research has {len(plan.pending)} net-new Parallel subject(s) "
                    f"(~${plan.estimated_usd:.2f}; {plan.reused_completed} completed reused, "
                    f"{plan.duplicate_handles} duplicates skipped). Approval also covers "
                    "cache-first LinkedIn profile hydration and identity judging for available "
                    "research results; those provider costs are not included in this Parallel-only "
                    f"estimate. Re-run with --approve and --budget >= ${plan.estimated_usd:.2f} "
                    f"(current ${self.budget:.2f})."
                ),
            )

        if not plan.pending:
            proposals = propose()
            if proposals.judge_errors:
                error = (
                    "identity judge returned no verdict for "
                    f"{proposals.judge_errors} proposal(s)"
                )
                return self._outcome(
                    ReceiptStatus.FAILED,
                    plan,
                    started,
                    counts=ReceiptCounts(total, total, 0, 0),
                    proposals=proposals,
                    errors=(error,),
                )
            return self._outcome(
                ReceiptStatus.REUSED,
                plan,
                started,
                counts=ReceiptCounts(total, total, 0, 0),
                proposals=proposals,
                reason="all eligible people already have completed Parallel research",
            )

        self._progress(
            "research",
            ReceiptCounts.create(total=total, completed=plan.reused_completed),
        )
        print(
            f"[deep-research] researching {len(plan.pending)} net-new people via "
            f"Parallel.ai ({self.processor}); this can take several minutes — live "
            "progress below:",
            file=sys.stderr,
            flush=True,
        )
        params = ResearchRunParams(
            output_dir=self.out_dir,
            rows=plan.pending,
            processor=self.processor,
            on_progress=provider_progress,
            db=self.db,
        )
        try:
            research = driver.run_research(params)
        except SystemExit as exc:
            research = ResearchRunResult.failed(len(plan.pending), f"SystemExit: {exc}")
        except Exception as exc:
            research = ResearchRunResult.failed(
                len(plan.pending), f"{type(exc).__name__}: {exc}"
            )
        research_completed = plan.reused_completed + research.completed
        print(
            f"[deep-research] research finished "
            f"({research.completed}/{research.total}).",
            file=sys.stderr,
            flush=True,
        )

        proposals = propose() if research.usable else None
        errors = research.errors
        if proposals and proposals.judge_errors:
            errors = (
                *errors,
                "identity judge returned no verdict for "
                f"{proposals.judge_errors} proposal(s)",
            )
        complete = research.complete and not errors
        failed = 0 if complete else max(0, total - research_completed)
        return self._outcome(
            ReceiptStatus.RAN if complete else ReceiptStatus.FAILED,
            plan,
            started,
            counts=ReceiptCounts.create(
                total=total,
                completed=research_completed,
                failed=failed,
            ),
            proposals=proposals,
            errors=errors,
        )
