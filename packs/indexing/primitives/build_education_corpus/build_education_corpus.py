#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from packs.indexing.lib.artifacts import build_education_corpus
from packs.indexing.lib.contracts import count_defaulted_numeric, load_search_contract, normalize_record_for_contract, validate_record
from packs.indexing.lib.io import write_json, write_jsonl
from packs.indexing.lib.people import flatten_people


def run(
    people_csv: Path,
    output_schools: Path,
    output_people_education: Path,
    output_school_records: Path,
    output_education_records: Path,
    stats: Path,
    default_operator_id: str | None,
):
    result = build_education_corpus(flatten_people(people_csv), default_operator_id)
    school_contract = load_search_contract("turbopuffer/schools.namespace.json")
    education_contract = load_search_contract("turbopuffer/education.namespace.json")
    school_records = [normalize_record_for_contract(row, school_contract) for row in result["schools"]]
    education_records = [normalize_record_for_contract(row, education_contract) | {"id": row["id"]} for row in result["education"]]
    errors = [row for row in (validate_record(record, school_contract) for record in school_records) if not row["ok"]]
    errors += [row for row in (validate_record(record, education_contract) for record in education_records) if not row["ok"]]
    if errors:
        raise SystemExit(json.dumps({"errors": errors}))
    write_jsonl(output_schools, result["schools"])
    write_jsonl(output_people_education, result["education"])
    write_jsonl(output_school_records, school_records)
    write_jsonl(output_education_records, education_records)
    write_json(
        stats,
        {
            "schools": len(school_records),
            "education": len(education_records),
            "defaulted_numeric_fields": {
                "schools": count_defaulted_numeric(result["schools"], school_contract),
                "education": count_defaulted_numeric(result["education"], education_contract),
            },
        },
    )
    return {"schools": len(school_records), "education": len(education_records)}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    run_parser = sub.add_parser("run")
    for name in ["people-csv", "output-schools", "output-people-education", "output-school-records", "output-education-records", "stats"]:
        run_parser.add_argument("--" + name, required=True)
    run_parser.add_argument("--default-operator-id", default=None)
    args = parser.parse_args()
    if args.cmd != "run":
        parser.error("subcommand required: run")
    print(
        json.dumps(
            run(
                Path(args.people_csv),
                Path(args.output_schools),
                Path(args.output_people_education),
                Path(args.output_school_records),
                Path(args.output_education_records),
                Path(args.stats),
                args.default_operator_id,
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
