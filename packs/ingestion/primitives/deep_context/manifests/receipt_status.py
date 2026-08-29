"""The one status vocabulary for Deep Context enrichment receipts."""

from __future__ import annotations

from enum import StrEnum


class ReceiptStatus(StrEnum):
    """Status vocabulary for enrichment stage receipts.

    Produced by the enrichment pipeline — its manifest is the one on-disk
    writer. research_reconcile.coordinator emits one exact terminal outcome;
    parallel_research.driver also uses research_complete for its lower-level
    provider progress receipt. Read in two places: the reconcile CLI's JSON
    on stdout, and the fixed manifest.json the pipeline writes for anyone
    polling progress on disk. That manifest is observability metadata only:
    queue selection, spend approval, resume behavior, and workflow routing
    never read it. The review server ignores a stale running receipt unless
    its own process-local enrichment flag is active.

    Serializes as the plain string value (StrEnum), so receipts and CLI JSON
    are byte-identical to the pre-enum literals.
    """

    INVALID_BUDGET = "invalid_budget"
    NOOP = "noop"
    DRY_RUN = "dry_run"
    NEEDS_APPROVAL = "needs_approval"
    REUSED = "reused"
    RUNNING = "running"
    RAN = "ran"
    RESEARCH_COMPLETE = "research_complete"
    FAILED = "failed"


RECONCILE_SUCCESS_STATUSES = frozenset({
    ReceiptStatus.NOOP.value,
    ReceiptStatus.REUSED.value,
    ReceiptStatus.RAN.value,
})
