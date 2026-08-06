#!/usr/bin/env python3
"""Stable CLI and in-process API for SQLite-selected Parallel research."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from packs.ingestion.primitives.deep_context.common import CANONICAL_DB
from packs.ingestion.primitives.deep_context.db.models import RESEARCH_CONFIRM_THRESHOLD
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.parallel_research import (
    config,
    driver,
)
from packs.ingestion.primitives.deep_context.research_reconcile import (
    selection,
)


@dataclass(frozen=True)
class ResearchRunParams:
    """One explicit configuration door for an in-process research pass."""

    output_dir: Path
    rows: tuple[dict[str, str], ...] = ()
    processor: str = config.DEFAULT_PROCESSOR
    selection_fingerprint: str = ""
    manifest: str = ""
    api_key: str | None = None
    base_url: str = config.DEFAULT_BASE_URL
    beta_header: str = config.DEFAULT_BETA_HEADER
    batch_size: int = config.DEFAULT_BATCH_SIZE
    limit: int | None = None
    poll_interval: int = config.DEFAULT_POLL_INTERVAL
    max_wait: int = config.DEFAULT_MAX_WAIT
    api_timeout: int = 60
    on_progress: Callable[[dict[str, Any]], None] | None = None
    db: Db | None = None
    owns_receipt: bool = True


run_research = driver.run_research


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Deep-research contact dossiers via Parallel.ai"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("estimate", "run"):
        child = subparsers.add_parser(command)
        child.add_argument("--input", required=True)
        child.add_argument("--db", default=str(CANONICAL_DB), type=Path)
        child.add_argument("--output-dir", default=config.DEFAULT_OUTPUT_DIR, type=Path)
        child.add_argument("--processor", default=config.DEFAULT_PROCESSOR,
                           choices=sorted(config.ALLOWED_PROCESSORS))
        child.add_argument("--limit", type=int)
        if command == "run":
            child.add_argument("--api-key")
            child.add_argument("--base-url", default=config.DEFAULT_BASE_URL)
            child.add_argument("--beta-header", default=config.DEFAULT_BETA_HEADER)
            child.add_argument("--manifest")
            for flag, default in (
                ("batch-size", config.DEFAULT_BATCH_SIZE),
                ("poll-interval", config.DEFAULT_POLL_INTERVAL),
                ("max-wait", config.DEFAULT_MAX_WAIT),
                ("workers", config.DEFAULT_RESULT_WORKERS), ("api-timeout", 60),
            ):
                child.add_argument(f"--{flag}", type=int, default=default)
    args = parser.parse_args(argv)
    db = Db(args.db)
    plan = selection.select_research(
        db,
        processor=args.processor,
        confirm_threshold=RESEARCH_CONFIRM_THRESHOLD,
        include_plausibly_absent=True,
        include_candidates=True,
    )
    if args.command == "estimate":
        processor = config.validate_processor(args.processor)
        rows, todo = list(plan.queue), list(plan.pending)
        reused = plan.reused_completed
        todo = todo[: args.limit] if args.limit is not None else todo
        per_task, wall_clock = config.PROCESSOR_LATENCY[processor]
        payload = {
            "primitive": "deep_research_contacts",
            "command": "estimate",
            "input": str(args.input),
            "output_dir": str(args.output_dir),
            "queue_rows": len(rows),
            "skipped_already_done": reused,
            "would_submit": len(todo),
            "processor": processor,
            "estimated_usd": round(len(todo) * config.PROCESSOR_PRICING_USD[processor], 4),
            "estimated_latency": {
                "processor": processor,
                "per_task": per_task,
                "rough_wall_clock": "no paid Parallel work" if not todo else wall_clock,
                "basis": "Parallel Task API processor docs; task-group runs are submitted together.",
            },
        }
    else:
        payload = run_research(ResearchRunParams(
            output_dir=Path(args.output_dir),
            rows=plan.queue,
            processor=args.processor,
            selection_fingerprint=str(plan.fingerprint.get("fingerprint") or ""),
            manifest=str(args.manifest or ""),
            api_key=args.api_key,
            base_url=args.base_url,
            beta_header=args.beta_header,
            batch_size=args.batch_size,
            limit=args.limit,
            poll_interval=args.poll_interval,
            max_wait=args.max_wait,
            api_timeout=args.api_timeout,
            db=db,
        ))
    print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
    raise SystemExit({"completed_with_errors": 2, "failed": 1}.get(str(payload.get("status")), 0))


if __name__ == "__main__":
    main()
