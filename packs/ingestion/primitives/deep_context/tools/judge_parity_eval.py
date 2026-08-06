#!/usr/bin/env python3
"""Compare the unified identity judge with historical and human decisions.

The tool copies the required artifacts into a temporary directory before
parsing them. Its default mode is free and only reports the historical
baseline. ``--replay`` prints an estimate first and exits 20 unless the caller
also passes ``--approve-spend``. Replay uses cached facts and profiles only;
it never calls RapidAPI or Parallel.
"""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.indexing.lib.openai_responses import reasoning_effort
from packs.ingestion.primitives.common.gates import EXIT_NEEDS_APPROVAL
from packs.ingestion.primitives.deep_context.common import emit
from packs.ingestion.primitives.deep_context.tools.judge_parity_data import load_install
from packs.ingestion.primitives.deep_context.tools.judge_parity_replay import estimate, replay


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deep-context", action="append", type=Path, required=True)
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--approve-spend", action="store_true")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning-effort", default="high")
    parser.add_argument("--concurrency", type=int, default=64)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-retries", type=int, default=2)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="judge-parity-") as temporary:
        stage = Path(temporary)
        installs = [
            load_install(source, stage / str(index), source.parent.parent.name)
            for index, source in enumerate(args.deep_context)
        ]
        baseline = {
            "status": "completed",
            "mode": "historical_baseline",
            "installs": [
                {"install": install.label, **install.baseline}
                for install in installs
            ],
        }
        if not args.replay:
            emit(baseline)
            return 0
        effort = reasoning_effort(args.reasoning_effort)
        dry_run = estimate(installs, args.model, effort)
        emit({**dry_run, "baseline": baseline["installs"]})
        if not args.approve_spend:
            emit(
                {
                    "status": "needs_approval",
                    "message": "rerun with --approve-spend to replay the unified judge",
                }
            )
            return EXIT_NEEDS_APPROVAL
        emit(
            replay(
                installs,
                model=args.model,
                effort=effort,
                concurrency=args.concurrency,
                timeout=args.timeout,
                max_retries=args.max_retries,
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
