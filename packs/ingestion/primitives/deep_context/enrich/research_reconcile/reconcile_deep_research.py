"""Thin CLI for the typed cost-gated research stage."""

from __future__ import annotations

import argparse
import math
import sys

from packs.ingestion.primitives.common.gates import EXIT_NEEDS_APPROVAL
from packs.ingestion.primitives.deep_context.db.readiness import CANONICAL_DB
from packs.ingestion.primitives.deep_context.shared.common import (
    emit,
)
from packs.ingestion.primitives.deep_context.db.store import open_existing_db
from packs.ingestion.primitives.deep_context.manifests.receipt_status import (
    RECONCILE_SUCCESS_STATUSES,
    ReceiptStatus,
)
from packs.ingestion.primitives.deep_context.enrich.parallel_research import config
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.coordinator import (
    ReconcileDeepResearch,
)


DEFAULT_BUDGET = 0.0


def _finite_non_negative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a finite, non-negative number")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Research worth-Yes people without LinkedIn or completed research (cost-gated)."
    )
    parser.add_argument("--db", default=str(CANONICAL_DB))
    parser.add_argument(
        "--processor",
        default=config.DEFAULT_PROCESSOR,
        choices=sorted(config.PROCESSOR_PRICING_USD),
    )
    parser.add_argument(
        "--budget",
        type=_finite_non_negative_float,
        default=DEFAULT_BUDGET,
        help="Maximum explicitly approved spend (finite, non-negative USD)",
    )
    # --approve plus --budget at/above the plan's estimate is the whole spend
    # gate: execute_reconcile refuses to call Parallel.ai otherwise, returning
    # ReceiptStatus.NEEDS_APPROVAL with the estimate instead of raising.
    for flag in ("approve", "dry-run"):
        parser.add_argument(f"--{flag}", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = open_existing_db(args.db)
    node = ReconcileDeepResearch(
        processor=args.processor,
        budget=args.budget,
        approve=args.approve,
        dry_run=args.dry_run,
        db=db,
    )
    result = node.run()
    # The enrichment pipeline owns the one web-visible manifest; this CLI
    # reports on stdout only so a manual run can never stomp it.
    payload = result.to_payload()
    emit(payload)
    if result.status == ReceiptStatus.NEEDS_APPROVAL:
        return EXIT_NEEDS_APPROVAL
    if result.status == ReceiptStatus.DRY_RUN or result.status.value in RECONCILE_SUCCESS_STATUSES:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
