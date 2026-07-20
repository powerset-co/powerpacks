"""THE enrichment contract — statuses, receipt semantics, and state rules in one module.

Every writer (reconcile_deep_research stamps receipt statuses), every reader
(the review server's enrich page, linkedin gate, status API), and every test
imports THIS module. No other file may define or literal-compare an enrichment
status: the "reused" vs "completed" liveness bug happened because three readers
compared scattered string literals against a vocabulary no one owned.

The receipt: `ENRICH_MANIFEST` (deep-research/manifest.json) is a RECEIPT, never
the truth — any writer may scribble on it (external CLI runs, restarts, crashes)
without stranding a page, because state is re-derived from disk at every read.

The derived states (rules in `derive_enrichment_state`, in order):

  1. running        — the in-process pipeline job is alive; the receipt carries
                      its heartbeat progress (phase/done/total).
  2. needs_approval — net-new research > 0: continuing costs Parallel money and
                      ONLY the user's Approve click may start it.
  3. done           — zero net-new AND the receipt records a completed run for
                      exactly this worth selection.
  4. free_pending   — zero net-new but no current completed receipt: the $0
                      reuse/judge-cache/assemble/prefetch chain still needs to
                      run; rendering the enrich page triggers it.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.common import (
    ENRICH_MANIFEST,
    FACTS_DIR,
    LINKEDIN_OVERRIDES_CSV,
    VERDICTS_JSONL,
    read_jsonl,
)
from packs.ingestion.primitives.deep_context.review_store import load_override_rows

# --- receipt statuses (the full vocabulary the primitive may stamp) ----------
STATUS_INVALID_BUDGET = "invalid_budget"
STATUS_NOOP = "noop"
STATUS_DRY_RUN = "dry_run"
STATUS_NEEDS_APPROVAL = "needs_approval"
STATUS_REUSED = "reused"
STATUS_RUNNING = "running"
STATUS_SUBMITTED = "submitted"
STATUS_RAN = "ran"
STATUS_RESEARCH_COMPLETE = "research_complete"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
# reader-only statuses (never stamped by the primitive)
STATUS_NOT_STARTED = "not_started"
STATUS_STALE = "stale"

WRITER_STATUSES = frozenset({
    STATUS_INVALID_BUDGET, STATUS_NOOP, STATUS_DRY_RUN, STATUS_NEEDS_APPROVAL,
    STATUS_REUSED, STATUS_RUNNING, STATUS_SUBMITTED, STATUS_RAN,
    STATUS_RESEARCH_COMPLETE, STATUS_COMPLETED, STATUS_FAILED,
})
# A $0 all-reused pass IS a completed run: the primitive stamps "reused" as its
# terminal status, and every consumer keys on "completed". The reader below is
# the ONE place this equivalence is applied.
COMPLETED_EQUIVALENT = frozenset({STATUS_COMPLETED, STATUS_REUSED})
# Statuses meaning provider work is in flight (an approval click on one of
# these is an idempotent no-op, not an error).
IN_FLIGHT_STATUSES = frozenset({
    STATUS_RUNNING, STATUS_SUBMITTED, STATUS_RESEARCH_COMPLETE, STATUS_COMPLETED,
})

# --- derived states -----------------------------------------------------------
STATE_RUNNING = "running"
STATE_NEEDS_APPROVAL = "needs_approval"
STATE_DONE = "done"
STATE_FREE_PENDING = "free_pending"


def read_enrichment_manifest(path: Path = ENRICH_MANIFEST, *,
                             selection: dict[str, Any] | None = None) -> dict[str, Any]:
    """The receipt, normalized: `current` marks a selection match, "reused"
    reads as "completed" (COMPLETED_EQUIVALENT), non-current reads as "stale"."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"stage": "enrich", "status": STATUS_NOT_STARTED,
                "counts": {"total": 0, "completed": 0, "pending": 0, "failed": 0},
                "current": False, "approval_current": False}
    if not isinstance(value, dict):
        return {"stage": "enrich", "status": STATUS_NOT_STARTED, "counts": {},
                "current": False, "approval_current": False}
    recorded_selection = value.get("selection") if isinstance(value.get("selection"), dict) else {}
    current = bool(
        selection
        and recorded_selection.get("sha256") == selection.get("sha256")
        and recorded_selection.get("review_revision") == selection.get("review_revision")
        and bool(selection.get("review_revision"))
    )
    approval = value.get("approval") if isinstance(value.get("approval"), dict) else {}
    try:
        estimated = round(float(value.get("estimated_usd") or 0), 2)
        approved_estimate = round(float(approval.get("estimated_usd") or -1), 2)
        approved_budget = round(float(approval.get("approved_budget_usd") or -1), 2)
        approved_count = int(approval.get("would_submit") or -1)
        manifest_count = int(value.get("would_submit") or 0)
    except (TypeError, ValueError, OverflowError):
        estimated = approved_estimate = approved_budget = -1
        approved_count = manifest_count = -1
    approval_current = bool(
        current
        and value.get("status") == STATUS_NEEDS_APPROVAL
        and approval.get("status") == "approved"
        and approval.get("selection_sha256") == recorded_selection.get("sha256")
        and approval.get("review_revision") == recorded_selection.get("review_revision")
        and approved_count == manifest_count
        and math.isfinite(estimated)
        and math.isfinite(approved_estimate)
        and math.isfinite(approved_budget)
        and approved_estimate == estimated
        and approved_budget >= estimated
    )
    result = {**value, "current": current, "approval_current": approval_current}
    if result.get("status") in COMPLETED_EQUIVALENT:
        result["status"] = STATUS_COMPLETED
    if not current:
        result["status"] = STATUS_STALE
    return result


def derive_enrichment_state(selection: dict[str, Any], *,
                            verdicts_path: Path = VERDICTS_JSONL,
                            review_path: Path = LINKEDIN_OVERRIDES_CSV,
                            facts_dir: Path = FACTS_DIR,
                            manifest_path: Path = ENRICH_MANIFEST,
                            job_running: bool = False) -> dict[str, Any]:
    """THE enrichment state, derived from disk at every enrich-page render.

    The rules live in the module docstring (running > needs_approval > done >
    free_pending). Net-new is counted exactly the way the primitive counts it:
    the eligible worth selection versus the research artifacts beside the
    manifest (manifest_path.parent/<handle>/01_research_parallel.json), so this
    count can never disagree with the estimate the $0 run would stamp.
    """
    # Local import: reconcile_deep_research imports this package's server module.
    from packs.ingestion.primitives.deep_context import reconcile_deep_research as dr

    receipt = read_enrichment_manifest(manifest_path, selection=selection)
    if job_running:
        return {**receipt, "state": STATE_RUNNING}
    verdicts = list(read_jsonl(verdicts_path))
    overrides = load_override_rows(review_path)
    threshold = dr.build_parser().get_default("confirm_threshold")
    subset = dr.eligible_subset(verdicts, threshold, overrides,
                                include_plausibly_absent=True)
    subset += dr.candidate_subset(facts_dir, overrides)
    handles = {str(r.get("parent_slug") or "").strip() for r in subset} - {""}
    net_new = sum(1 for handle in handles
                  if not (manifest_path.parent / handle / "01_research_parallel.json").exists())
    cost_per = dr.PROCESSOR_PRICING_USD[dr.DEFAULT_PROCESSOR]
    state = {**receipt, "net_new": net_new,
             "net_new_estimated_usd": round(net_new * cost_per, 2)}
    if net_new:
        try:
            receipt_count = int(receipt.get("would_submit") or 0)
        except (TypeError, ValueError):
            receipt_count = -1
        approvable = bool(receipt.get("current")
                          and receipt.get("status") == STATUS_NEEDS_APPROVAL
                          and receipt_count == net_new)
        return {**state, "state": STATE_NEEDS_APPROVAL, "approvable": approvable}
    if receipt.get("current") and receipt.get("status") == STATUS_COMPLETED:
        return {**state, "state": STATE_DONE}
    return {**state, "state": STATE_FREE_PENDING}
