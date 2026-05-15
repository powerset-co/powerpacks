#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from packs.indexing.lib.contracts import count_defaulted_numeric, load_search_contract, normalize_record_for_contract, validate_record
from packs.indexing.lib.io import read_jsonl, write_json, write_jsonl
from packs.indexing.lib.people import build_people_records


def run(flattened: Path, roles: Path, output: Path, stats: Path, default_operator_id: str | None):
    people = read_jsonl(flattened)
    records = build_people_records(people, default_operator_id=default_operator_id)
    contract = load_search_contract("turbopuffer/people.namespace.json")
    records = [normalize_record_for_contract(row, contract) for row in records]
    errors = [validate_record(row, contract) for row in records if not validate_record(row, contract)["ok"]]
    if errors:
        raise SystemExit(json.dumps({"errors": errors}))
    write_jsonl(output, records)
    write_json(
        stats,
        {
            "people_records": len(records),
            "defaulted_numeric_fields": {"people": count_defaulted_numeric(records, contract)},
            "allowed_operator_ids_default": default_operator_id or "local:user",
        },
    )
    return {"people_records": len(records), "output": str(output)}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--flattened", required=True)
    run_parser.add_argument("--roles", required=True)
    run_parser.add_argument("--output", required=True)
    run_parser.add_argument("--stats", required=True)
    run_parser.add_argument("--default-operator-id", default=None)
    args = parser.parse_args()
    if args.cmd != "run":
        parser.error("subcommand required: run")
    print(
        json.dumps(
            run(Path(args.flattened), Path(args.roles), Path(args.output), Path(args.stats), args.default_operator_id),
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
