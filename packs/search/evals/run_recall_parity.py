#!/usr/bin/env python3
"""Run deterministic recall cases through the typed layered search engine."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

if __package__ in {None, ""}:
    repository = Path(__file__).resolve().parents[3]
    if str(repository) not in sys.path:
        sys.path.insert(0, str(repository))

from packs.search.evals.search_spec_factory import (
    RESULT_LIMIT_CAP,
    build_search_spec,
    case_id,
    list_payload,
    print_json,
    run_case,
    select_cases,
    write_report,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RECALL_DIR = REPO_ROOT / "tests" / "recall"


def main(
    argv: Sequence[str] | None = None, *, default_bucket: str | None = None, output_name: str = "recall-parity"
) -> None:
    parser = argparse.ArgumentParser(description="Run typed Powerpacks recall parity")
    parser.add_argument("--recall-dir", type=Path, default=DEFAULT_RECALL_DIR)
    parser.add_argument("--bucket", default=default_bucket)
    parser.add_argument("--case-glob")
    parser.add_argument("--include-staging", action="store_true")
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--limit-cap", type=int, default=RESULT_LIMIT_CAP)
    parser.add_argument("--backend", choices=("local", "powerset"), default="powerset")
    parser.add_argument("--db-path")
    parser.add_argument("--set-id")
    parser.add_argument("--operator-id", action="append", default=[])
    parser.add_argument("--output-root", type=Path, default=REPO_ROOT / ".powerpacks" / "search-runs" / output_name)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    cases = select_cases(args.recall_dir, args.bucket, args.case_glob, args.include_staging)
    if args.max_cases:
        cases = cases[: args.max_cases]
    if args.list:
        print_json(list_payload(cases))
        return
    if args.dry_run:
        rows = []
        for case in cases:
            try:
                spec = build_search_spec(
                    case,
                    backend=args.backend,
                    db_path=args.db_path,
                    set_id=args.set_id,
                    operator_ids=args.operator_id,
                    limit_cap=args.limit_cap,
                )
                rows.append({"id": case_id(case), "status": "ready", "search_spec": spec.to_dict()})
            except Exception as exc:
                rows.append({"id": case_id(case), "status": "unsupported_case", "reason": str(exc)})
        print_json({"mode": "dry-run", "cases": rows})
        return

    allowed_output_root = (REPO_ROOT / ".powerpacks" / "search-runs").resolve()
    resolved_output_root = args.output_root.resolve()
    if resolved_output_root != allowed_output_root and allowed_output_root not in resolved_output_root.parents:
        parser.error("--output-root must be under .powerpacks/search-runs")

    results = []
    for case in cases:
        print(f"running {case.relpath}...", flush=True)
        try:
            results.append(
                run_case(
                    case,
                    output_root=args.output_root,
                    backend=args.backend,
                    db_path=args.db_path,
                    set_id=args.set_id,
                    operator_ids=args.operator_id,
                    limit_cap=args.limit_cap,
                )
            )
        except Exception as exc:
            results.append(
                {
                    "id": case_id(case),
                    "source": case.relpath,
                    "bucket": case.bucket,
                    "status": "fail",
                    "reason": str(exc),
                }
            )
    report = args.output_root / "report.md"
    write_report(results, report, "Recall Parity")
    print_json({"report": str(report), "results": results})
    if any(row["status"] not in {"pass", "ignored"} for row in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
