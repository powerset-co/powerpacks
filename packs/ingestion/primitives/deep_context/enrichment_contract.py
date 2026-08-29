"""Status vocabulary for Deep Context enrichment job and stage receipts.

The fixed ``manifest.json`` written by enrichment is observability metadata:
counts, timing, and any error from the most recent invocation. Queue selection,
spend approval, resume behavior, and workflow routing never read that file.
"""
from __future__ import annotations


STATUS_INVALID_BUDGET = "invalid_budget"
STATUS_NOOP = "noop"
STATUS_DRY_RUN = "dry_run"
STATUS_NEEDS_APPROVAL = "needs_approval"
STATUS_REUSED = "reused"
STATUS_RUNNING = "running"
STATUS_RAN = "ran"
STATUS_RESEARCH_COMPLETE = "research_complete"
STATUS_FAILED = "failed"
