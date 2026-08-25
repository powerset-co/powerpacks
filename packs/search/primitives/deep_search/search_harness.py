#!/usr/bin/env python3
"""Editable result-driven search harness built from the ordinary search pipeline.

The reviewed plan and initial queries are the one pre-search checkpoint. After
approval, each pond is query -> compiled payload -> reviewed payload -> run ->
one diagnosis and next move. Score bands are display-only and the loop is capped
at four ponds.

This file is the CLI entry only; the stages live in `harness/`:
plan_review, pond, next_move, plus their shared artifacts/summary/annotate
helpers.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

try:  # direct script execution
    from harness.artifacts import ROOT
    from harness.next_move import decide
    from harness.plan_review import update_pending_query
    from harness.pond import compile_pond, reannotate_saved, review_payload, run_pond
    from harness.prompts import NEXT_SEARCH_DIAGNOSES
    from harness.retrieval import DEFAULT_LOCAL_DB
except ImportError:  # pragma: no cover - module execution
    from .harness.artifacts import ROOT
    from .harness.next_move import decide
    from .harness.plan_review import update_pending_query
    from .harness.pond import compile_pond, reannotate_saved, review_payload, run_pond
    from .harness.prompts import NEXT_SEARCH_DIAGNOSES
    from .harness.retrieval import DEFAULT_LOCAL_DB


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("set-query", "compile-pond", "review-payload", "run-pond", "decide",
                 "reannotate-saved"):
        command = sub.add_parser(name)
        command.add_argument("--run-dir", required=True)
        if name in {"compile-pond", "run-pond", "reannotate-saved"}:
            command.add_argument("--env-file", default=str(ROOT / ".env"))
            if name in {"compile-pond", "run-pond"}:
                command.add_argument("--backend", choices=("powerset", "local"))
                command.add_argument("--db", default=str(ROOT / DEFAULT_LOCAL_DB))
            elif name == "reannotate-saved":
                command.add_argument("--pond", type=int)
        elif name == "set-query":
            command.add_argument("--query", required=True)
        elif name == "review-payload":
            command.add_argument("--payload-json")
            command.add_argument("--rerank-exclusion", action="append", default=[])
            command.add_argument("--human-reviewed", action="store_true")
        else:
            command.add_argument("--autonomous", action="store_true")
            command.add_argument("--choice", type=int, choices=(2, 3))
            command.add_argument("--diagnosis", choices=NEXT_SEARCH_DIAGNOSES)
            command.add_argument("--note", default="")
            command.add_argument("--model", default="gpt-5.6-luna")
            command.add_argument("--reasoning-effort", default="medium")
    args = parser.parse_args()
    run_dir = Path(args.run_dir).resolve()
    if args.command == "set-query":
        path = update_pending_query(run_dir=run_dir, query=args.query)
    elif args.command == "compile-pond":
        path = compile_pond(run_dir=run_dir, env_file=args.env_file,
                            backend=args.backend, db=args.db)
    elif args.command == "review-payload":
        path = review_payload(run_dir=run_dir,
                              payload_path=Path(args.payload_json) if args.payload_json else None,
                              rerank_exclusions=args.rerank_exclusion,
                              human_reviewed=args.human_reviewed)
    elif args.command == "run-pond":
        path = run_pond(run_dir=run_dir, env_file=args.env_file,
                        backend=args.backend, db=args.db)
    elif args.command == "reannotate-saved":
        path = reannotate_saved(run_dir=run_dir, env_file=args.env_file, pond=args.pond)
    else:
        path = decide(run_dir=run_dir, choice=args.choice, diagnosis=args.diagnosis,
                      note=args.note, autonomous=args.autonomous, model=args.model,
                      reasoning_effort=args.reasoning_effort)
    print(json.dumps({"status": "completed", "results": str(path)}, indent=2))


if __name__ == "__main__":
    main()
