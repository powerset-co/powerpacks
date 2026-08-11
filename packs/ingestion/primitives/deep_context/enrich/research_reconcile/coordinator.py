"""Apply the fixed gates and callbacks around one typed research selection."""

from __future__ import annotations

import math
import sys
import time
from dataclasses import replace
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
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
    ReconcileOutput,
    JudgingProgress,
    ResearchSelection,
    RetargetRunResult,
)
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.selection import (
    select_research,
    write_queue,
)


def _receipt_body(
    options: ReconcileOptions,
    plan: ResearchSelection,
    status: str,
    result_status: str,
    *,
    result_error: str | None = None,
    completed: int = 0,
    failed: int = 0,
) -> ResearchReceiptBody:
    """Render one receipt-file snapshot.

    ``status`` and ``result_status`` diverge on purpose: ``status`` is the coarse
    bucket a polling UI switches on (needs_approval/running/research_complete/
    failed); ``result_status`` is the exact value this call returns to its caller
    (e.g. a dry run collapses into a "needs_approval"-flavored receipt but keeps
    its own precise ``dry_run`` result_status).
    """
    return ResearchReceiptBody(
        source=options.manifest_path.parent.name if options.manifest_path else None,
        status=status,
        counts=ReceiptCounts.create(
            # plan.deduped_total, not len(plan.queue): duplicate handles are
            # never queued or billed, so the receipt never counts them — the
            # same reused + pending basis the driver's mid-run counts use.
            total=plan.deduped_total,
            completed=completed,
            failed=failed,
        ),
        selection=plan.fingerprint,
        eligible=len(plan.eligible),
        would_submit=len(plan.pending),
        reused_completed=plan.reused_completed,
        duplicate_handles=plan.duplicate_handles,
        processor=plan.processor,
        estimated_usd=plan.estimated_usd,
        budget_usd=options.budget,
        result_status=result_status,
        error=result_error if status == ReceiptStatus.FAILED else None,
    )


def execute_reconcile(
    options: ReconcileOptions,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one select -> spend-gate -> research -> judge pass.

    Returns (result payload for the caller, receipt-file payload) — see
    _receipt_body for why their status fields can diverge.
    """
    started = time.monotonic()
    if not math.isfinite(options.budget) or options.budget < 0:
        message = "--budget must be a finite, non-negative USD amount"
        result = {
            "source": "reconcile_deep_research",
            "status": ReceiptStatus.INVALID_BUDGET,
            "budget_usd": options.budget,
            "message": message,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "updated_at": now_iso(),
        }
        receipt = ResearchReceiptBody(
            source=(options.manifest_path.parent.name if options.manifest_path else None),
            status=ReceiptStatus.FAILED,
            counts=ReceiptCounts.create(total=0),
            error=message,
        )
        return result, receipt.to_payload()

    plan = select_research(
        options.db,
        processor=options.processor,
        confirm_threshold=options.confirm_threshold,
        include_plausibly_absent=options.include_plausibly_absent,
        include_candidates=options.include_candidates,
    )
    options.out_dir.mkdir(parents=True, exist_ok=True)
    write_queue(options.queue_csv, plan.queue)

    def make_result(
        status: str,
        *,
        reason: str | None = None,
        message: str | None = None,
        output_dir: str | None = None,
        research_status: str | None = None,
        research_error: str | None = None,
        progress: str | None = None,
        proposals: RetargetRunResult | None = None,
    ) -> ReconcileOutput:
        return ReconcileOutput(
            plan,
            options.budget,
            status,
            str(options.queue_csv),
            int((time.monotonic() - started) * 1000),
            reason,
            message,
            output_dir,
            research_status,
            research_error,
            progress,
            proposals.proposed if proposals else None,
            proposals.judge_calls if proposals else None,
            proposals.cached_verdicts if proposals else None,
            proposals.grandfathered if proposals else None,
        )

    def provider_progress(progress: ResearchProgress) -> None:
        if options.receipt:
            # The driver's counts pass through as-is — not rebuilt via
            # ReceiptCounts.create, whose clamps would second-guess the
            # driver's own arithmetic. Its total is already the same deduped
            # reused + todo basis as plan.deduped_total.
            body = _receipt_body(options, plan, progress.status, progress.status)
            options.receipt.write(replace(body, counts=progress.counts).to_payload())
        if options.on_progress:
            options.on_progress(progress)

    params = ResearchRunParams(
        output_dir=options.out_dir,
        rows=plan.queue,
        processor=options.processor,
        selection_fingerprint=plan.fingerprint.fingerprint,
        manifest=options.manifest_path,
        on_progress=provider_progress,
        db=options.db,
        owns_receipt=False,  # this coordinator already drives the receipt via provider_progress
    )

    def finish(
        output: ReconcileOutput,
        status: str,
        *,
        result_status: str,
        result_error: str | None = None,
        completed: int = 0,
        failed: int = 0,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return output.to_payload(), _receipt_body(
            options,
            plan,
            status,
            result_status,
            result_error=result_error,
            completed=completed,
            failed=failed,
        ).to_payload()

    owner_block = owner_background(options.db)

    def heartbeat(done: int, total: int) -> None:
        if options.on_progress:
            options.on_progress(JudgingProgress(done, total))
        if options.receipt:
            options.receipt.write(
                ResearchReceiptBody(
                    source=(options.manifest_path.parent.name if options.manifest_path else None),
                    status=ReceiptStatus.RUNNING,
                    phase="judging_retargets",
                    done=done,
                    total=total,
                    counts=ReceiptCounts.create(
                        total=plan.deduped_total,
                        completed=plan.reused_completed,
                    ),
                ).to_payload()
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
        return finish(
            make_result(ReceiptStatus.NOOP, reason="no effective-Yes contacts need enrichment"),
            ReceiptStatus.RESEARCH_COMPLETE,
            result_status=ReceiptStatus.NOOP,
        )
    if options.dry_run:
        return finish(
            make_result(ReceiptStatus.DRY_RUN),
            ReceiptStatus.NEEDS_APPROVAL,
            result_status=ReceiptStatus.DRY_RUN,
            completed=plan.reused_completed,
        )
    if not plan.pending:
        # Every eligible row already has a completed, fingerprint-matching research
        # artifact — no new spend — but still (re)judge: a completed result may not
        # yet have been proposed as a retarget. prepare_research_proposal's own
        # fingerprint cache (the "cached" disposition), not this branch, is what
        # keeps re-judging free when nothing has actually changed.
        proposals = propose()
        return finish(
            make_result(
                ReceiptStatus.REUSED,
                output_dir=str(options.out_dir),
                reason="all eligible people already have completed Parallel research",
                proposals=proposals,
            ),
            ReceiptStatus.RESEARCH_COMPLETE,
            result_status=ReceiptStatus.REUSED,
            completed=plan.deduped_total,
        )
    # The spend gate: an explicit --approve plus a budget at or above the estimate,
    # checked before any paid call. Signals purely via the returned
    # ReceiptStatus.NEEDS_APPROVAL string, not common/gates.py's EXIT_NEEDS_APPROVAL
    # (exit 20) — the CLI wrapper (reconcile_deep_research.py) never maps this status
    # to a process exit code, so a caller that only checks the exit code, not the
    # JSON payload's "status" field, will not see the gate.
    if not options.approve or plan.estimated_usd > options.budget:
        return finish(
            make_result(
                ReceiptStatus.NEEDS_APPROVAL,
                message=(
                    f"deep research for {len(plan.pending)} net-new people is "
                    f"~${plan.estimated_usd:.2f} ({plan.reused_completed} completed "
                    f"reused, {plan.duplicate_handles} duplicates skipped); get explicit "
                    "approval, then re-run with --approve and an approved --budget at "
                    f"or above the estimate (current ${options.budget:.2f})"
                ),
            ),
            ReceiptStatus.NEEDS_APPROVAL,
            result_status=ReceiptStatus.NEEDS_APPROVAL,
            completed=plan.reused_completed,
        )

    if options.receipt:
        options.receipt.write(
            _receipt_body(
                options,
                plan,
                ReceiptStatus.RUNNING,
                ReceiptStatus.RUNNING,
                completed=plan.reused_completed,
            ).to_payload()
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
    research_ok = research_status in RESEARCH_OK_STATUSES
    # A failed pass has nothing new to judge; return a zeroed result instead of
    # calling propose() so the payload shape stays stable either way.
    proposals = (
        propose()
        if research_ok
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
    final = make_result(
        ReceiptStatus.RAN if research_ok else ReceiptStatus.FAILED,
        output_dir=str(options.out_dir),
        research_status=research_status,
        research_error=research.error,
        progress="streamed live to stderr",
        proposals=proposals,
    )
    # research_ok (completed/completed_with_errors) counts real per-handle
    # errors, not the whole pending batch — a completed_with_errors run still
    # billed and completed most of plan.pending; only a genuine top-level
    # failure (nothing ran at all) means every pending row failed. `completed`
    # is trimmed by the same error count so the two stay disjoint — otherwise
    # ReceiptCounts.create's total-completed clamp would zero `failed` right
    # back out for a deduped queue where everything is nominally "completed".
    completed = (
        plan.deduped_total - len(research.errors) if research_ok else plan.reused_completed
    )
    failed = len(research.errors) if research_ok else len(plan.pending)
    return finish(
        final,
        ReceiptStatus.RESEARCH_COMPLETE if research_ok else ReceiptStatus.FAILED,
        result_status=ReceiptStatus.RAN if research_ok else ReceiptStatus.FAILED,
        result_error=research.error,
        completed=completed,
        failed=failed,
    )
