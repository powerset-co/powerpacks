"""Stable coordinator and CLI for cost-gated identity research."""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Any, Callable

from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.ingestion.primitives.deep_context.common import (
    CANONICAL_DB,
    emit,
    ENRICH_MANIFEST,
)
from packs.ingestion.primitives.deep_context.db.models import (
    RESEARCH_CONFIRM_THRESHOLD,
)
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.enrichment_receipt import (
    EnrichmentReceipt,
)
from packs.ingestion.primitives.deep_context.parallel_research import config
from packs.ingestion.primitives.deep_context.research_reconcile import (
    coordinator,
    selection,
)
from packs.ingestion.primitives.deep_context.research_reconcile.models import (
    ResearchProgressEvent,
)


DEFAULT_BUDGET = 0.0
class ReconcileDeepResearch:
    """Stable constructor over the typed research-reconcile services."""

    def __init__(
        self,
        *,
        manifest: str | Path | None = None,
        processor: str = selection.DEFAULT_PROCESSOR,
        confirm_threshold: float = RESEARCH_CONFIRM_THRESHOLD,
        budget: float = DEFAULT_BUDGET,
        approve: bool = False,
        dry_run: bool = False,
        include_plausibly_absent: bool = False,
        include_candidates: bool = False,
        no_llm: bool = False,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = "medium",
        out_dir: Path | None = None,
        queue_csv: Path | None = None,
        on_progress: Callable[[ResearchProgressEvent], None] | None = None,
        db: Db,
    ) -> None:
        manifest_text = (
            str(ENRICH_MANIFEST) if manifest is None else str(manifest).strip()
        )
        manifest_path: Path | None = Path(manifest_text) if manifest_text else None
        receipt: EnrichmentReceipt | None = (
            EnrichmentReceipt(manifest_path) if manifest_path else None
        )
        self.options = coordinator.ReconcileOptions(
            out_dir=Path(out_dir or selection.DR_OUT_DIR),
            queue_csv=Path(queue_csv or selection.QUEUE_CSV),
            manifest_path=manifest_path, processor=processor,
            confirm_threshold=confirm_threshold, budget=budget, approve=approve,
            dry_run=dry_run, include_plausibly_absent=include_plausibly_absent,
            include_candidates=include_candidates, no_llm=no_llm, model=model,
            reasoning_effort=reasoning_effort, on_progress=on_progress, db=db,
            receipt=receipt,
        )

    def run_with_result(self) -> tuple[dict[str, Any], dict[str, Any]]:
        result, payload = coordinator.execute_reconcile(self.options)
        if self.options.receipt:
            self.options.receipt.write(payload)
        return result, payload

    def run(self) -> dict[str, Any]:
        return self.run_with_result()[1]


def _finite_non_negative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a finite, non-negative number")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deep-research the correct identity for wrong_person detaches (cost-gated)."
    )
    paths = {
        "db": CANONICAL_DB,
        "manifest": ENRICH_MANIFEST,
    }
    for flag, default in paths.items():
        parser.add_argument(f"--{flag}", default=str(default))
    parser.add_argument(
        "--processor",
        default=selection.DEFAULT_PROCESSOR,
        choices=sorted(config.PROCESSOR_PRICING_USD),
    )
    parser.add_argument("--confirm-threshold", type=float, default=RESEARCH_CONFIRM_THRESHOLD)
    parser.add_argument(
        "--budget",
        type=_finite_non_negative_float,
        default=DEFAULT_BUDGET,
        help="Maximum explicitly approved spend (finite, non-negative USD)",
    )
    for flag in ("approve", "dry-run", "include-plausibly-absent", "include-candidates", "no-llm"):
        parser.add_argument(f"--{flag}", action="store_true")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--reasoning-effort",
        default="medium",
        choices=["minimal", "low", "medium", "high"],
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = open_existing_db(args.db)
    node = ReconcileDeepResearch(
        manifest=args.manifest,
        processor=args.processor,
        confirm_threshold=args.confirm_threshold,
        budget=args.budget,
        approve=args.approve,
        dry_run=args.dry_run,
        include_plausibly_absent=args.include_plausibly_absent,
        include_candidates=args.include_candidates,
        no_llm=args.no_llm,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        db=db,
    )
    result, _ = node.run_with_result()
    emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
