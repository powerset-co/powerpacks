#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from packs.indexing.lib.artifacts import build_company_corpus
from packs.indexing.lib.contracts import count_defaulted_numeric, load_search_contract, normalize_record_for_contract, validate_record
from packs.indexing.lib.io import read_jsonl, write_json, write_jsonl
from packs.indexing.lib.people import flatten_people


def run(people_csv: Path, flattened: Path, output_corpus: Path, output_records: Path, stats: Path, default_operator_id: str | None):
    people = read_jsonl(flattened) if flattened.exists() else flatten_people(people_csv)
    corpus = build_company_corpus(people, default_operator_id)
    contract = load_search_contract("turbopuffer/companies.namespace.json")
    records = [normalize_record_for_contract(row, contract) for row in corpus]
    errors = [validate_record(row, contract) for row in records if not validate_record(row, contract)["ok"]]
    if errors:
        raise SystemExit(json.dumps({"errors": errors}))
    write_jsonl(output_corpus, corpus)
    write_jsonl(output_records, records)
    write_json(
        stats,
        {"companies": len(records), "defaulted_numeric_fields": {"companies": count_defaulted_numeric(corpus, contract)}},
    )
    return {"companies": len(records)}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    run_parser = sub.add_parser("run")
    for name in ["people-csv", "flattened", "output-corpus", "output-records", "stats"]:
        run_parser.add_argument("--" + name, required=True)
    run_parser.add_argument("--default-operator-id", default=None)
    args = parser.parse_args()
    if args.cmd != "run":
        parser.error("subcommand required: run")
    print(
        json.dumps(
            run(
                Path(args.people_csv),
                Path(args.flattened),
                Path(args.output_corpus),
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
