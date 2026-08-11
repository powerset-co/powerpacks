"""The one status vocabulary for Deep Context enrichment receipts."""

from __future__ import annotations

from enum import StrEnum


class ReceiptStatus(StrEnum):
    """Status vocabulary for enrichment job and stage receipts.

    Produced by the enrich-stage writers (research_reconcile.coordinator's
    execute_reconcile mints every member; parallel_research.driver and
    synthetic.assemble emit the running/research_complete/failed subset) and
    read in two places: reconcile_deep_research.py's CLI JSON on stdout, and
    the fixed manifest.json (written via EnrichmentReceipt) for anyone polling
    progress on disk. That manifest is observability metadata only: queue
    selection, spend approval, resume behavior, and workflow routing never
    read it. The review web UI's enrichment panel (review/enrichment.py,
    review/rendering.py) speaks the same vocabulary for its view statuses,
    but what it actually routes on is db.models.JobStatus
    (QUEUED/RUNNING/APPLIED/FAILED) — a separate enum that lives in SQLite,
    not here.

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
