"""Apply the fixed gates and callbacks around one typed research selection."""

from __future__ import annotations

import math
import sys
import time
from typing import Any

from packs.ingestion.primitives.deep_context.shared.dossier_evidence import owner_background
from packs.ingestion.primitives.deep_context.enrich.parallel_research import driver
from packs.ingestion.primitives.deep_context.enrich.parallel_research.models import (
    RESEARCH_OK_STATUSES,
    ResearchProgress,
    ResearchRunParams,
    ResearchRunResult,
)
from packs.ingestion.primitives.deep_context.manifests.receipt_counts import (
    ReceiptCounts,
)
from packs.ingestion.primitives.deep_context.manifests.receipt_status import (
    ReceiptStatus,
)
from packs.ingestion.primitives.deep_context.manifests.research_receipt_body import (
    ResearchReceiptBody,
)
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.judging import propose_retargets
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.models import (
    ReconcileOptions,
    JudgingProgress,
    ResearchSelection,
    RetargetRunResult,
)
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.selection import (
    select_research,
)


def _payload(
    options: ReconcileOptions,
    plan: ResearchSelection | None,
    status: str,
    *,
    started: float,
    counts: ReceiptCounts | None = None,
    completed: int = 0,
    failed: int = 0,
    error: str | None = None,
    reason: str | None = None,
    message: str | None = None,
    output_dir: str | None = None,
    research_status: str | None = None,
    research_error: str | None = None,
    research_errors: tuple[str, ...] = (),
    progress: str | None = None,
    proposals: RetargetRunResult | None = None,
    phase: str | None = None,
    done: int | None = None,
    total: int | None = None,
) -> dict[str, Any]:
    """Render the one payload returned, written, and emitted by this stage."""
    plan_total = plan.deduped_total if plan else 0
    return ResearchReceiptBody(
        source="reconcile_deep_research",
        status=status,
        counts=counts or ReceiptCounts.create(
            total=plan_total, completed=completed, failed=failed
        ),
        selection=plan.fingerprint if plan else None,
        request_fingerprint=plan.request_fingerprint if plan else None,
        eligible=len(plan.eligible) if plan else None,
        eligible_candidates=plan.eligible_candidates if plan else None,
        would_submit=len(plan.pending) if plan else None,
        reused_completed=plan.reused_completed if plan else None,
        duplicate_handles=plan.duplicate_handles if plan else None,
        processor=plan.processor if plan else options.processor,
        cost_per_person_usd=plan.cost_per_person_usd if plan else None,
        estimated_usd=plan.estimated_usd if plan else None,
        budget_usd=options.budget,
        error=error,
        reason=reason,
        message=message,
        output_dir=output_dir,
        research_status=research_status,
        research_error=research_error,
        research_errors=research_errors,
        progress=progress,
        retargets_proposed=proposals.proposed if proposals else None,
        judge_calls=proposals.judge_calls if proposals else None,
        cached_verdicts=proposals.cached_verdicts if proposals else None,
        grandfathered=proposals.grandfathered if proposals else None,
        judge_errors=proposals.judge_errors if proposals else None,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        phase=phase,
        done=done,
        total=total,
    ).to_payload()


def execute_reconcile(
    options: ReconcileOptions,
) -> dict[str, Any]:
    """Run one select -> spend-gate -> research -> judge pass."""
    started = time.monotonic()
    if not math.isfinite(options.budget) or options.budget < 0:
        message = "--budget must be a finite, non-negative USD amount"
        return _payload(
            options,
            None,
            ReceiptStatus.INVALID_BUDGET,
            started=started,
            error=message,
            message=message,
        )

    plan = select_research(
        options.db,
        processor=options.processor,
        confirm_threshold=options.confirm_threshold,
        include_plausibly_absent=options.include_plausibly_absent,
        include_candidates=options.include_candidates,
    )
    options.out_dir.mkdir(parents=True, exist_ok=True)

    def provider_progress(progress: ResearchProgress) -> None:
        counts = ReceiptCounts.create(
            total=plan.deduped_total,
            completed=plan.reused_completed + progress.counts.completed,
            failed=progress.counts.failed,
        )
        if options.receipt:
            receipt_status = (
                ReceiptStatus.FAILED
                if progress.status == ReceiptStatus.FAILED
                else ReceiptStatus.RUNNING
            )
            options.receipt.write(
                _payload(
                    options,
                    plan,
                    receipt_status,
                    started=started,
                    counts=counts,
                    phase=progress.status,
                )
            )
        if options.on_progress:
            options.on_progress(ResearchProgress(progress.status, counts))

    params = ResearchRunParams(
        output_dir=options.out_dir,
        rows=plan.pending,
        processor=options.processor,
        manifest=options.manifest_path,
        on_progress=provider_progress,
        db=options.db,
        owns_receipt=False,  # this coordinator already drives the receipt via provider_progress
    )

    owner_block = owner_background(options.db)

    def heartbeat(done: int, total: int) -> None:
        if options.on_progress:
            options.on_progress(JudgingProgress(done, total))
        if options.receipt:
            options.receipt.write(
                _payload(
                    options,
                    plan,
                    ReceiptStatus.RUNNING,
                    started=started,
                    phase="judging_retargets",
                    done=done,
                    total=total,
                    counts=ReceiptCounts.create(
                        total=plan.deduped_total,
                        completed=plan.reused_completed,
                    ),
                )
            )

    def propose() -> RetargetRunResult:
        return propose_retargets(
            plan.eligible,
            db=options.db,
            owner_block=owner_block,
            model=options.model or "",
            effort=options.reasoning_effort or "medium",
            confirm_threshold=options.confirm_threshold,
            heartbeat=heartbeat,
        )

    # No worth='yes' parent currently qualifies (see the strict predicate at
    # selection.select_research) — nothing to research or judge.
    if not plan.eligible:
        return _payload(
            options,
            plan,
            ReceiptStatus.NOOP,
            started=started,
            reason="no effective-Yes contacts need enrichment",
        )
    if options.dry_run:
        return _payload(
            options,
            plan,
            ReceiptStatus.DRY_RUN,
            started=started,
            completed=plan.reused_completed,
        )
    # Approval precedes every paid follow-up, including the cache-reuse branch:
    # propose() may hydrate a missing LinkedIn profile and call the identity
    # judge even when Parallel itself has nothing new to submit. The numeric
    # estimate is deliberately scoped to Parallel because this package has no
    # authoritative RapidAPI/OpenAI price model; do not invent one here.
    if not options.approve or plan.estimated_usd > options.budget:
        return _payload(
            options,
            plan,
            ReceiptStatus.NEEDS_APPROVAL,
            started=started,
            message=(
                f"deep research has {len(plan.pending)} net-new Parallel subject(s) "
                f"(~${plan.estimated_usd:.2f}; {plan.reused_completed} completed reused, "
                f"{plan.duplicate_handles} duplicates skipped). Approval also covers "
                "cache-first LinkedIn profile hydration and identity judging for available "
                "research results; those provider costs are not included in this Parallel-only "
                f"estimate. Re-run with --approve and --budget >= ${plan.estimated_usd:.2f} "
                f"(current ${options.budget:.2f})."
            ),
            completed=plan.reused_completed,
        )
    if not plan.pending:
        # Every eligible row already has a completed, fingerprint-matching research
        # artifact. Parallel is free here, but the approved follow-up may still
        # hydrate or judge a result that has not yet produced a reusable verdict.
        proposals = propose()
        if proposals.judge_errors:
            error = f"identity judge returned no verdict for {proposals.judge_errors} proposal(s)"
            return _payload(
                options,
                plan,
                ReceiptStatus.FAILED,
                started=started,
                error=error,
                completed=plan.deduped_total,
                output_dir=str(options.out_dir),
                research_error=error,
                research_errors=(error,),
                proposals=proposals,
            )
        return _payload(
            options,
            plan,
            ReceiptStatus.REUSED,
            started=started,
            completed=plan.deduped_total,
            output_dir=str(options.out_dir),
            reason="all eligible people already have completed Parallel research",
            proposals=proposals,
        )
    if options.receipt:
        options.receipt.write(
            _payload(
                options,
                plan,
                ReceiptStatus.RUNNING,
                started=started,
                completed=plan.reused_completed,
            )
        )
    print(
        f"[deep-research] researching {len(plan.pending)} net-new people via "
        f"Parallel.ai ({options.processor}); this can take several minutes — live "
        "progress below:",
        file=sys.stderr,
        flush=True,
    )
    try:
        # Downgrades any provider crash (including a SystemExit bubbling up from a
        # nested CLI-shaped call) to ReceiptStatus.FAILED instead of raising. Safe to
        # retry: any per-row artifact run_research already projected before the
        # failure stays projected, so the next execute_reconcile call only
        # resubmits rows that never got projected (filter_already_done).
        research = driver.run_research(params)
    except SystemExit as exc:
        research = ResearchRunResult.failed(f"SystemExit: {exc}")
    except Exception as exc:
        research = ResearchRunResult.failed(f"{type(exc).__name__}: {exc}")
    print(
        f"[deep-research] research finished ({research.status}).",
        file=sys.stderr,
        flush=True,
    )
    research_status = research.status
    research_usable = research_status in RESEARCH_OK_STATUSES
    # A failed pass has nothing new to judge; return a zeroed result instead of
    # calling propose() so the payload shape stays stable either way.
    proposals = (
        propose()
        if research_usable
        else RetargetRunResult(
            path="",
            proposed=0,
            preserved_user_rows=0,
            total_rows=0,
            judge_calls=0,
            cached_verdicts=0,
            grandfathered=0,
        )
    )
    research_errors = research.errors
    if proposals.judge_errors:
        research_errors = (*research_errors, f"identity judge returned no verdict for {proposals.judge_errors} proposal(s)")
    complete = research_status in {"completed", "no_work"} and not research_errors
    error = "; ".join(research_errors) or None
    # Count only results actually projected by the driver. A stream failure can
    # leave pending rows distinct from explicit provider failures.
    completed = (
        plan.reused_completed + research.completed
        if research_usable
        else plan.reused_completed
    )
    failed = min(
        len(research.errors) if research_usable else len(plan.pending),
        plan.deduped_total - completed,
    )
    return _payload(
        options,
        plan,
        ReceiptStatus.RAN if complete else ReceiptStatus.FAILED,
        started=started,
        error=error if not complete else None,
        completed=completed,
        failed=failed,
        output_dir=str(options.out_dir),
        research_status=research_status,
        research_error=error,
        research_errors=research_errors,
        progress="streamed live to stderr",
        proposals=proposals,
    )
