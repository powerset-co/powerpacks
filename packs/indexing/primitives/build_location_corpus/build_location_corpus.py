#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from packs.indexing.lib.artifacts import build_location_corpus
from packs.indexing.lib.io import read_jsonl, write_json, write_jsonl
from packs.indexing.lib.people import flatten_people


def run(people_csv: Path, companies_corpus: Path, output_corpus: Path, stats: Path):
    rows = build_location_corpus(flatten_people(people_csv))
    if companies_corpus.exists():
        rows.extend(build_location_corpus(read_jsonl(companies_corpus)))
    by_id = {row["id"]: row for row in rows}
    rows = [by_id[key] for key in sorted(by_id)]
    write_jsonl(output_corpus, rows)
    write_json(stats, {"locations": len(rows), "internal_only": True})
    return {"locations": len(rows), "internal_only": True}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    run_parser = sub.add_parser("run")
    for name in ["people-csv", "companies-corpus", "output-corpus", "stats"]:
        run_parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    if args.cmd != "run":
        parser.error("subcommand required: run")
    print(
        json.dumps(
            run(Path(args.people_csv), Path(args.companies_corpus), Path(args.output_corpus), Path(args.stats)),
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
