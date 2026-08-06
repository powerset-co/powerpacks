#!/usr/bin/env python3
"""Stable CLI and in-process API for file-first Parallel contact research.

Flow:
    research_queue.csv -> Parallel task group -> raw/normalized person files
    -> callback; standalone CLI -> one fixed manifest.json

Internal queue, SDK, normalization, artifact, and driver policies live under
``parallel_research`` and are not re-exported here.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.deep_context.db.store import Db  # noqa: E402
from packs.ingestion.primitives.deep_context.parallel_research import (  # noqa: E402
    config,
    driver,
    queue,
)


@dataclass(frozen=True)
class ResearchRunParams:
    """One explicit configuration door for an in-process research pass."""

    input_csv: Path
    output_dir: Path
    processor: str = config.DEFAULT_PROCESSOR
    manifest: str = ""
    api_key: str | None = None
    base_url: str = config.DEFAULT_BASE_URL
    beta_header: str = config.DEFAULT_BETA_HEADER
    batch_size: int = config.DEFAULT_BATCH_SIZE
    limit: int | None = None
    poll_interval: int = config.DEFAULT_POLL_INTERVAL
    max_wait: int = config.DEFAULT_MAX_WAIT
    workers: int = config.DEFAULT_RESULT_WORKERS
    api_timeout: int = 60
    on_progress: Callable[[dict[str, Any]], None] | None = None
    db: Db | None = None
    owns_receipt: bool = True


def run_research(params: ResearchRunParams) -> dict[str, Any]:
    """Run one synchronous provider pass through the concrete driver."""
    return driver.run_research(params)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Deep-research contact dossiers via Parallel.ai"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("estimate", "run"):
        child = subparsers.add_parser(command)
        child.add_argument("--input", required=True)
        child.add_argument("--output-dir", default=config.DEFAULT_OUTPUT_DIR, type=Path)
        child.add_argument(
            "--processor",
            default=config.DEFAULT_PROCESSOR,
            choices=sorted(config.ALLOWED_PROCESSORS),
        )
        child.add_argument("--limit", type=int)
        if command == "run":
            child.add_argument("--api-key")
            child.add_argument("--base-url", default=config.DEFAULT_BASE_URL)
            child.add_argument("--beta-header", default=config.DEFAULT_BETA_HEADER)
            child.add_argument("--manifest")
            child.add_argument("--batch-size", type=int, default=config.DEFAULT_BATCH_SIZE)
            child.add_argument("--poll-interval", type=int, default=config.DEFAULT_POLL_INTERVAL)
            child.add_argument("--max-wait", type=int, default=config.DEFAULT_MAX_WAIT)
            child.add_argument("--workers", type=int, default=config.DEFAULT_RESULT_WORKERS)
            child.add_argument("--api-timeout", type=int, default=60)
    args = parser.parse_args(argv)
    if args.command == "estimate":
        processor = config.validate_processor(args.processor)
        rows = queue.load_queue(Path(args.input))
        todo, reused = queue.filter_already_done(rows, Path(args.output_dir))
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
            "estimated_usd": round(
                len(todo) * config.PROCESSOR_PRICING_USD[processor], 4
            ),
            "estimated_latency": {
                "processor": processor,
                "per_task": per_task,
                "rough_wall_clock": (
                    "no paid Parallel work" if not todo else wall_clock
                ),
                "basis": (
                    "Parallel Task API processor docs; task-group runs are "
                    "submitted together."
                ),
            },
        }
    else:
        payload = run_research(ResearchRunParams(
            input_csv=Path(args.input),
            output_dir=Path(args.output_dir),
            processor=args.processor,
            manifest=str(args.manifest or ""),
            api_key=args.api_key,
            base_url=args.base_url,
            beta_header=args.beta_header,
            batch_size=args.batch_size,
            limit=args.limit,
            poll_interval=args.poll_interval,
            max_wait=args.max_wait,
            workers=args.workers,
            api_timeout=args.api_timeout,
        ))
    print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
    code = {"completed_with_errors": 2, "failed": 1}.get(
        str(payload.get("status")), 0
    )
    raise SystemExit(code)


if __name__ == "__main__":
    main()
