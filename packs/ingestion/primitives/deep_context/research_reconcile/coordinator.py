"""Apply the fixed gates and callbacks around one typed research selection."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.common import (
    load_owner,
    owner_background_block,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.deep_research_contacts import (
    ResearchRunParams,
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
    EnrichmentReceiptBody,
    enrichment_counts,
)
from packs.ingestion.primitives.deep_context.research_reconcile.judging import (
    propose_retargets_from_output,
)
from packs.ingestion.primitives.deep_context.research_reconcile.provider import (
    RESEARCH_OK_STATUSES,
    run_provider,
)
from packs.ingestion.primitives.deep_context.research_reconcile.receipts import (
    ReceiptPolicy,
)
from packs.ingestion.primitives.deep_context.research_reconcile.selection import (
    select_research,
    write_queue,
)


@dataclass(frozen=True)
class ReconcileOptions:
    overrides_csv: Path
    facts_dir: Path
    raw_dir: Path
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


def execute_reconcile(
    options: ReconcileOptions,
) -> tuple[dict[str, Any], EnrichmentReceiptBody]:
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
        return result, EnrichmentReceiptBody(
            source=(
                options.manifest_path.parent.name if options.manifest_path else None
            ),
            status=STATUS_FAILED,
            counts=enrichment_counts(total=0, failed=0),
            error=message,
        )

    plan = select_research(
        options.db,
        facts_dir=options.facts_dir,
        raw_dir=options.raw_dir,
        out_dir=options.out_dir,
        processor=options.processor,
        confirm_threshold=options.confirm_threshold,
        include_plausibly_absent=options.include_plausibly_absent,
        include_candidates=options.include_candidates,
    )
    base = plan.result_base(options.budget)
    options.out_dir.mkdir(parents=True, exist_ok=True)
    write_queue(options.queue_csv, plan.queue)

    policy: ReceiptPolicy

    def provider_progress(progress: dict[str, Any]) -> None:
        if options.receipt:
            status = str(progress.get("status") or STATUS_RUNNING)
            body = policy.body(status, {**base, "status": status})
            body["counts"] = dict(progress.get("counts") or {})
            policy.write(body)
        if options.on_progress:
            options.on_progress(progress)

    params = ResearchRunParams(
        input_csv=options.queue_csv,
        output_dir=options.out_dir,
        processor=options.processor,
        manifest=str(options.manifest_path) if options.manifest_path else "",
        on_progress=provider_progress,
        db=options.db,
        owns_receipt=False,
    )
    policy = ReceiptPolicy(
        options.receipt,
        options.manifest_path,
        plan,
        params,
        options.overrides_csv,
        options.facts_dir,
        options.queue_csv,
        options.out_dir,
        options.budget,
    )

    def finish(
        result: dict[str, Any],
        status: str,
        *,
        completed: int = 0,
        failed: int = 0,
    ) -> tuple[dict[str, Any], EnrichmentReceiptBody]:
        return result, policy.terminal(
            result, status, completed=completed, failed=failed
        )

    owner = load_owner()
    owner_block = owner_background_block(owner) if owner else ""

    def heartbeat(done: int, total: int) -> None:
        if options.on_progress:
            options.on_progress(
                {
                    "status": "running",
                    "phase": "judging_retargets",
                    "counts": {"done": done, "total": total},
                }
            )
        if options.manifest_path:
            policy.write(policy.judging(done, total))

    def propose() -> dict[str, Any]:
        return propose_retargets_from_output(
            options.out_dir,
            plan.eligible,
            options.overrides_csv,
            db=options.db,
            facts_dir=options.facts_dir,
            raw_dir=options.raw_dir,
            use_llm=not options.no_llm,
            owner_block=owner_block,
            model=options.model or "",
            effort=options.reasoning_effort or "medium",
            confirm_threshold=options.confirm_threshold,
            heartbeat=heartbeat,
        )

    if not plan.eligible:
        return finish(
            {
                **base,
                "status": STATUS_NOOP,
                "queue_csv": str(options.queue_csv),
                "reason": "no effective-Yes contacts need enrichment",
            },
            STATUS_RESEARCH_COMPLETE,
        )
    if options.dry_run:
        return finish(
            {
                **base,
                "status": STATUS_DRY_RUN,
                "queue_csv": str(options.queue_csv),
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            },
            STATUS_NEEDS_APPROVAL,
            completed=plan.reused_completed,
        )
    if not plan.pending:
        proposals = propose()
        return finish(
            {
                **base,
                "status": STATUS_REUSED,
                "queue_csv": str(options.queue_csv),
                "output_dir": str(options.out_dir),
                "retargets_proposed": proposals.get("proposed", 0),
                "judge_calls": proposals.get("judge_calls", 0),
                "cached_verdicts": proposals.get("cached_verdicts", 0),
                "grandfathered": proposals.get("grandfathered", 0),
                "reason": "all eligible people already have completed Parallel research",
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            },
            STATUS_RESEARCH_COMPLETE,
            completed=len(plan.queue),
        )
    if not options.approve or plan.estimated_usd > options.budget:
        return finish(
            {
                **base,
                "status": STATUS_NEEDS_APPROVAL,
                "queue_csv": str(options.queue_csv),
                "message": (
                    f"deep research for {len(plan.pending)} net-new people is "
                    f"~${plan.estimated_usd:.2f} ({plan.reused_completed} completed "
                    f"reused, {plan.duplicate_handles} duplicates skipped); get explicit "
                    "approval, then re-run with --approve and an approved --budget at "
                    f"or above the estimate (current ${options.budget:.2f})"
                ),
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            },
            STATUS_NEEDS_APPROVAL,
            completed=plan.reused_completed,
        )

    if options.manifest_path:
        policy.write(
            policy.body(
                STATUS_RUNNING,
                {**base, "status": STATUS_RUNNING},
                completed=plan.reused_completed,
            )
        )
    research = run_provider(
        params, pending_count=len(plan.pending), processor=options.processor
    )
    research_status = str(research.get("status") or "failed")
    research_ok = research_status in RESEARCH_OK_STATUSES
    proposals = propose() if research_ok else {"proposed": 0}
    result = {
        **base,
        "status": STATUS_RAN if research_ok else STATUS_FAILED,
        "queue_csv": str(options.queue_csv),
        "output_dir": str(options.out_dir),
        "retargets_proposed": proposals.get("proposed", 0),
        "judge_calls": proposals.get("judge_calls", 0),
        "cached_verdicts": proposals.get("cached_verdicts", 0),
        "grandfathered": proposals.get("grandfathered", 0),
        "research_status": research_status,
        "research_error": research.get("error", ""),
        "progress": "streamed live to stderr",
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    return finish(
        result,
        STATUS_RESEARCH_COMPLETE if research_ok else STATUS_FAILED,
        completed=len(plan.queue) if research_ok else plan.reused_completed,
        failed=0 if research_ok else len(plan.pending),
    )
