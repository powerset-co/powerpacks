"""Apply the fixed gates and callbacks around one typed research selection."""

from __future__ import annotations

import math
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.dossier_evidence import owner_background
from packs.ingestion.primitives.deep_context.deep_research_contacts import (
    ResearchRunParams,
    run_research,
)
from packs.ingestion.primitives.deep_context.enrichment_contract import (
    STATUS_DRY_RUN,
    STATUS_FAILED,
    STATUS_INVALID_BUDGET,
    STATUS_NEEDS_APPROVAL,
    STATUS_NOOP,
    STATUS_RAN,
    STATUS_RESEARCH_COMPLETE,
    STATUS_REUSED,
    STATUS_RUNNING,
)
from packs.ingestion.primitives.deep_context.enrichment_receipt import (
    EnrichmentReceipt,
    enrichment_counts,
)
from packs.ingestion.primitives.deep_context.parallel_research.models import (
    ResearchProgress,
    ResearchRunResult,
)
from packs.ingestion.primitives.deep_context.research_reconcile.judging import (
    RetargetRunResult,
    propose_retargets,
)
from packs.ingestion.primitives.deep_context.research_reconcile.selection import (
    select_research,
    write_queue,
)

RESEARCH_OK_STATUSES = frozenset({"no_work", "completed"})


@dataclass(frozen=True)
class ReconcileOptions:
    out_dir: Path
    queue_csv: Path
    manifest_path: Path | None
    processor: str
    confirm_threshold: float
    budget: float
    approve: bool
    dry_run: bool
    include_plausibly_absent: bool
    include_candidates: bool
    no_llm: bool
    model: str
    reasoning_effort: str
    on_progress: Callable[[dict[str, Any]], None] | None
    db: Db
    receipt: EnrichmentReceipt | None


def _receipt_body(
    options: ReconcileOptions,
    plan: Any,
    status: str,
    result_status: str,
    *,
    result_error: str = "",
    completed: int = 0,
    failed: int = 0,
) -> dict[str, Any]:
    return {
        "source": options.manifest_path.parent.name if options.manifest_path else None,
        "stage": "enrich",
        "status": status,
        "counts": enrichment_counts(
            total=len(plan.queue), completed=completed, failed=failed,
        ),
        "selection": asdict(plan.fingerprint),
        "eligible": len(plan.eligible),
        "would_submit": len(plan.pending),
        "reused_completed": plan.reused_completed,
        "duplicate_handles": plan.duplicate_handles,
        "processor": plan.processor,
        "estimated_usd": plan.estimated_usd,
        "budget_usd": options.budget,
        "result_status": result_status,
        "error": result_error if status == STATUS_FAILED else None,
    }


def execute_reconcile(
    options: ReconcileOptions,
) -> tuple[dict[str, Any], dict[str, Any]]:
    started = time.monotonic()
    if not math.isfinite(options.budget) or options.budget < 0:
        message = "--budget must be a finite, non-negative USD amount"
        result = {
            "source": "reconcile_deep_research",
            "status": STATUS_INVALID_BUDGET,
            "budget_usd": options.budget,
            "message": message,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "updated_at": now_iso(),
        }
        return result, {
            "source": (
                options.manifest_path.parent.name if options.manifest_path else None
            ),
            "status": STATUS_FAILED, "counts": enrichment_counts(total=0, failed=0),
            "error": message,
        }

    plan = select_research(
        options.db,
        processor=options.processor,
        confirm_threshold=options.confirm_threshold,
        include_plausibly_absent=options.include_plausibly_absent,
        include_candidates=options.include_candidates,
    )
    base = plan.result_base(options.budget)
    options.out_dir.mkdir(parents=True, exist_ok=True)
    write_queue(options.queue_csv, plan.queue)

    def make_result(status: str, **values: Any) -> dict[str, Any]:
        return {
            **base, "status": status, "queue_csv": str(options.queue_csv), **values,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }

    def provider_progress(progress: ResearchProgress) -> None:
        if options.receipt:
            body = _receipt_body(options, plan, progress.status, progress.status)
            body["counts"] = asdict(progress.counts)
            options.receipt.write(body)
        if options.on_progress:
            options.on_progress(asdict(progress))

    params = ResearchRunParams(
        output_dir=options.out_dir,
        rows=plan.queue,
        processor=options.processor,
        selection_fingerprint=plan.fingerprint.fingerprint,
        manifest=str(options.manifest_path) if options.manifest_path else "",
        on_progress=provider_progress,
        db=options.db,
        owns_receipt=False,
    )
    def finish(
        result: dict[str, Any],
        status: str,
        *,
        result_status: str,
        result_error: str = "",
        completed: int = 0,
        failed: int = 0,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return result, _receipt_body(
            options,
            plan,
            status,
            result_status,
            result_error=result_error,
            completed=completed,
            failed=failed,
        )

    owner_block = owner_background(canonical_snapshot(options.db))

    def heartbeat(done: int, total: int) -> None:
        if options.on_progress:
            options.on_progress(
                {
                    "status": "running",
                    "phase": "judging_retargets",
                    "counts": {"done": done, "total": total},
                }
            )
        if options.receipt:
            options.receipt.write({
                "stage": "enrich", "status": STATUS_RUNNING,
                "phase": "judging_retargets", "done": done, "total": total,
                "counts": enrichment_counts(
                    total=len(plan.queue), completed=plan.reused_completed,
                ),
            })

    def propose() -> RetargetRunResult:
        return propose_retargets(
            plan.eligible,
            db=options.db,
            use_llm=not options.no_llm,
            owner_block=owner_block,
            model=options.model or "",
            effort=options.reasoning_effort or "medium",
            confirm_threshold=options.confirm_threshold,
            heartbeat=heartbeat,
        )

    def proposal_values(proposals: RetargetRunResult) -> dict[str, int]:
        return {
            "retargets_proposed": proposals.proposed,
            "judge_calls": proposals.judge_calls,
            "cached_verdicts": proposals.cached_verdicts,
            "grandfathered": proposals.grandfathered,
        }

    if not plan.eligible:
        return finish(
            make_result(STATUS_NOOP, reason="no effective-Yes contacts need enrichment"),
            STATUS_RESEARCH_COMPLETE,
            result_status=STATUS_NOOP,
        )
    if options.dry_run:
        return finish(
            make_result(STATUS_DRY_RUN),
            STATUS_NEEDS_APPROVAL,
            result_status=STATUS_DRY_RUN,
            completed=plan.reused_completed,
        )
    if not plan.pending:
        proposals = propose()
        return finish(
            make_result(
                STATUS_REUSED, output_dir=str(options.out_dir),
                reason="all eligible people already have completed Parallel research",
                **proposal_values(proposals),
            ),
            STATUS_RESEARCH_COMPLETE,
            result_status=STATUS_REUSED,
            completed=len(plan.queue),
        )
    if not options.approve or plan.estimated_usd > options.budget:
        return finish(
            make_result(
                STATUS_NEEDS_APPROVAL,
                message=(
                    f"deep research for {len(plan.pending)} net-new people is "
                    f"~${plan.estimated_usd:.2f} ({plan.reused_completed} completed "
                    f"reused, {plan.duplicate_handles} duplicates skipped); get explicit "
                    "approval, then re-run with --approve and an approved --budget at "
                    f"or above the estimate (current ${options.budget:.2f})"
                ),
            ),
            STATUS_NEEDS_APPROVAL,
            result_status=STATUS_NEEDS_APPROVAL,
            completed=plan.reused_completed,
        )

    if options.receipt:
        options.receipt.write(
            _receipt_body(
                options, plan, STATUS_RUNNING, STATUS_RUNNING,
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
        research = run_research(params)
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
        STATUS_RAN if research_ok else STATUS_FAILED,
        output_dir=str(options.out_dir), research_status=research_status,
        research_error=research.error, progress="streamed live to stderr",
        **proposal_values(proposals),
    )
    return finish(
        final,
        STATUS_RESEARCH_COMPLETE if research_ok else STATUS_FAILED,
        result_status=STATUS_RAN if research_ok else STATUS_FAILED,
        result_error=research.error,
        completed=len(plan.queue) if research_ok else plan.reused_completed,
        failed=0 if research_ok else len(plan.pending),
    )
