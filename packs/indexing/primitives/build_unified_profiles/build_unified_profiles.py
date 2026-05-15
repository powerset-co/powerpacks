#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from packs.indexing.lib.io import read_jsonl, write_json, write_jsonl
from packs.indexing.lib.people import build_unified_profiles


def run(input_path: Path, flattened: Path, roles: Path, companies: Path, output_csv: Path, profiles_jsonl: Path, stats: Path):
    profiles = build_unified_profiles(read_jsonl(flattened))
    write_jsonl(profiles_jsonl, profiles)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    fields = ["id", "full_name", "linkedin_url", "headline", "location_raw"]
    with output_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for profile in profiles:
            writer.writerow(
                {
                    "id": profile["id"],
                    "full_name": profile.get("name", ""),
                    "linkedin_url": profile.get("linkedin_url") or "",
                    "headline": profile.get("headline") or "",
                    "location_raw": profile.get("location") or "",
                }
            )
    write_json(stats, {"profiles": len(profiles), "unified_people": len(profiles)})
    return {"profiles": len(profiles), "output_csv": str(output_csv), "profiles_jsonl": str(profiles_jsonl)}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    run_parser = sub.add_parser("run")
    for name in ["input", "flattened", "roles", "companies", "output-csv", "profiles-jsonl", "stats"]:
        run_parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    if args.cmd != "run":
        parser.error("subcommand required: run")
    print(
        json.dumps(
            run(
                Path(args.input),
                Path(args.flattened),
                Path(args.roles),
                Path(args.companies),
                Path(args.output_csv),
                Path(args.profiles_jsonl),
                Path(args.stats),
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
