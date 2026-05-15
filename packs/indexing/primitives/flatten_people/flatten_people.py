#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from packs.indexing.lib.io import write_json, write_jsonl
from packs.indexing.lib.people import flatten_people, local_operator_ids


def run(input_path: Path, output: Path, stats: Path, limit: int | None, default_operator_id: str | None):
    rows = flatten_people(input_path)
    if limit is not None:
        rows = rows[:limit]
    for row in rows:
        row["allowed_operator_ids"] = local_operator_ids(row.get("raw", row), default_operator_id)
    write_jsonl(output, rows)
    default = default_operator_id or "local:user"
    write_json(
        stats,
        {
            "people": len(rows),
            "allowed_operator_ids_defaulted": sum(1 for row in rows if row.get("allowed_operator_ids") == [default]),
        },
    )
    return {"people": len(rows), "output": str(output)}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--input", required=True)
    run_parser.add_argument("--output", required=True)
    run_parser.add_argument("--stats", required=True)
    run_parser.add_argument("--limit", type=int)
    run_parser.add_argument("--default-operator-id", default=None)
    args = parser.parse_args()
    if args.cmd != "run":
        parser.error("subcommand required: run")
    print(
        json.dumps(
            run(Path(args.input), Path(args.output), Path(args.stats), args.limit, args.default_operator_id),
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
