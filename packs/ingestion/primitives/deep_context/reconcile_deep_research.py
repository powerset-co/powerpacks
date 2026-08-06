"""Stable coordinator and CLI for cost-gated identity research."""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Any

from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV,
    emit,
    ENRICH_MANIFEST,
    FACTS_DIR,
    INDEX_JSON,
    LINKEDIN_OVERRIDES_CSV,
    RAW_DIR,
    ROOT,
    VERDICTS_JSONL,
)
from packs.ingestion.primitives.deep_context.db.models import (
    RESEARCH_CONFIRM_THRESHOLD,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.enrichment_receipt import (
    EnrichmentReceipt,
    EnrichmentReceiptBody,
)
from packs.ingestion.primitives.deep_context.parallel_research import config
from packs.ingestion.primitives.deep_context.research_reconcile import (
    coordinator,
    selection,
)


DEFAULT_BUDGET = 0.0
CANONICAL_DB = ROOT / "deep-context.sqlite"


class ReconcileDeepResearch:
    """Stable constructor over the typed research-reconcile services."""

    def __init__(
        self,
        *,
        verdicts_jsonl: Path | None = None,
        overrides_csv: Path | None = None,
        people_csv: Path | None = None,
        facts_dir: Path | None = None,
        index_json: Path | None = None,
        raw_dir: Path | None = None,
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
        on_progress: Any = None,
        db: Db,
    ) -> None:
        del verdicts_jsonl, people_csv, index_json
        self.overrides_csv = Path(overrides_csv or LINKEDIN_OVERRIDES_CSV)
        self.facts_dir = Path(facts_dir or FACTS_DIR)
        self.raw_dir = Path(raw_dir or RAW_DIR)
        self.out_dir = Path(out_dir or selection.DR_OUT_DIR)
        self.queue_csv = Path(queue_csv or selection.QUEUE_CSV)
        self.on_progress = on_progress
        self.db = db
        manifest_text = (
            str(ENRICH_MANIFEST) if manifest is None else str(manifest).strip()
        )
        self.manifest_path = Path(manifest_text) if manifest_text else None
        self.receipt = (
            EnrichmentReceipt(self.manifest_path, db) if self.manifest_path else None
        )
        self.processor = processor
        self.confirm_threshold = confirm_threshold
        self.budget = budget
        self.approve = approve
        self.dry_run = dry_run
        self.include_plausibly_absent = include_plausibly_absent
        self.include_candidates = include_candidates
        self.no_llm = no_llm
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.result: dict[str, Any] = {}

    def _options(self) -> coordinator.ReconcileOptions:
        return coordinator.ReconcileOptions(
            self.overrides_csv,
            self.facts_dir,
            self.raw_dir,
            self.out_dir,
            self.queue_csv,
            self.manifest_path,
            self.processor,
            self.confirm_threshold,
            self.budget,
            self.approve,
            self.dry_run,
            self.include_plausibly_absent,
            self.include_candidates,
            self.no_llm,
            self.model,
            self.reasoning_effort,
            self.on_progress,
            self.db,
            self.receipt,
        )

    def execute(self) -> EnrichmentReceiptBody:
        self.result, payload = coordinator.execute_reconcile(self._options())
        return payload

    def run(self) -> EnrichmentReceiptBody:
        payload = self.execute()
        if self.receipt:
            self.receipt.write(payload)
        return payload


def _finite_non_negative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a finite, non-negative number")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deep-research the correct identity for wrong_person detaches (cost-gated)."
    )
    parser.add_argument("--verdicts-jsonl", default=str(VERDICTS_JSONL))
    parser.add_argument("--overrides-csv", default=str(LINKEDIN_OVERRIDES_CSV))
    parser.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    parser.add_argument("--facts-dir", default=str(FACTS_DIR))
    parser.add_argument("--index-json", default=str(INDEX_JSON))
    parser.add_argument("--raw-dir", default=str(RAW_DIR))
    parser.add_argument(
        "--db", default=str(CANONICAL_DB), help="Canonical Deep Context SQLite database"
    )
    parser.add_argument(
        "--manifest",
        default=str(ENRICH_MANIFEST),
        help="Fixed Enrich Contacts progress manifest",
    )
    parser.add_argument(
        "--processor",
        default=selection.DEFAULT_PROCESSOR,
        choices=sorted(config.PROCESSOR_PRICING_USD),
    )
    parser.add_argument(
        "--confirm-threshold", type=float, default=RESEARCH_CONFIRM_THRESHOLD
    )
    parser.add_argument(
        "--budget",
        type=_finite_non_negative_float,
        default=DEFAULT_BUDGET,
        help="Maximum explicitly approved spend (finite, non-negative USD)",
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help="Confirm the user approved this run's displayed estimate",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the queue + estimate only; no Parallel.ai spend",
    )
    parser.add_argument(
        "--include-plausibly-absent",
        action="store_true",
        help="Also research people the judge flagged linkedin_plausibly_absent — the synthetic-profile candidates (synthetic-profiles-plan §5)",
    )
    parser.add_argument(
        "--include-candidates",
        action="store_true",
        help="Also research dossier-bearing import candidates (import/*/candidates.csv) — contacts with no resolved LinkedIn at all",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Judge proposed retargets deterministically (offline/tests) instead of the LLM",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="Model for the proposed-retarget identity judge",
    )
    parser.add_argument(
        "--reasoning-effort",
        default="medium",
        choices=["minimal", "low", "medium", "high"],
        help="Reasoning effort for the proposed-retarget identity judge",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = Path(args.db)
    if not db_path.is_file():
        raise SystemExit(
            f"Deep Context database is missing: {db_path}; "
            "run the explicit legacy import first"
        )
    try:
        db = Db(db_path)
    except StoreError as exc:
        raise SystemExit(
            f"Deep Context database is unsupported: {db_path}: {exc}"
        ) from exc
    node = ReconcileDeepResearch(
        verdicts_jsonl=Path(args.verdicts_jsonl),
        overrides_csv=Path(args.overrides_csv),
        people_csv=Path(args.people_csv),
        facts_dir=Path(args.facts_dir),
        index_json=Path(args.index_json),
        raw_dir=Path(args.raw_dir),
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
    node.run()
    emit(node.result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
