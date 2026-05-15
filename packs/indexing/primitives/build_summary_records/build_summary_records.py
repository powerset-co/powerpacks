#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from packs.indexing.lib.artifacts import build_summary_records
from packs.indexing.lib.contracts import count_defaulted_numeric, load_search_contract, normalize_record_for_contract, validate_record
from packs.indexing.lib.io import read_jsonl, write_json, write_jsonl


def run(profiles_jsonl: Path, output_internal: Path, output_records: Path, stats: Path, default_operator_id: str | None):
    profiles = read_jsonl(profiles_jsonl)
    result = build_summary_records(profiles, default_operator_id)
    contract = load_search_contract("turbopuffer/summaries.namespace.json")
    records = [normalize_record_for_contract(row, contract) for row in result["summaries"]]
    errors = [validate_record(row, contract) for row in records if not validate_record(row, contract)["ok"]]
    if errors:
        raise SystemExit(json.dumps({"errors": errors}))
    write_jsonl(output_internal, result["internal_text"])
    write_jsonl(output_records, records)
    write_json(
        stats,
        {"summaries": len(records), "defaulted_numeric_fields": {"summaries": count_defaulted_numeric(result["summaries"], contract)}},
    )
    return {"summaries": len(records)}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    run_parser = sub.add_parser("run")
    for name in ["profiles-jsonl", "output-internal", "output-records", "stats"]:
        run_parser.add_argument("--" + name, required=True)
    run_parser.add_argument("--default-operator-id", default=None)
    args = parser.parse_args()
    if args.cmd != "run":
        parser.error("subcommand required: run")
    print(
        json.dumps(
            run(
                Path(args.profiles_jsonl),
                Path(args.output_internal),
                Path(args.output_records),
                Path(args.stats),
                args.default_operator_id,
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
