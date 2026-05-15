#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from packs.indexing.lib.io import read_jsonl, write_json, write_jsonl
from packs.indexing.lib.text import dense_text


def title_hash(title: str, desc: str) -> str:
    return hashlib.sha256(f"{title.strip().lower()}|{desc.strip().lower()}".encode()).hexdigest()[:16]


def run(flattened: Path, output_dir: Path, stats: Path):
    people = read_jsonl(flattened)
    by_hash: dict[str, dict] = {}
    for person in people:
        for exp in person.get("work_experiences") or []:
            if not isinstance(exp, dict):
                continue
            title = (exp.get("title") or exp.get("position_title") or exp.get("role") or "").strip()
            if not title:
                continue
            desc = (exp.get("description") or exp.get("summary") or "").strip()
            key = title_hash(title, desc)
            row = by_hash.setdefault(
                key,
                {
                    "title_hash": key,
                    "raw_title": title,
                    "description": desc,
                    "expanded_title": title,
                    "role_ids": [],
                    "seniority_band": "",
                    "role_track": "",
                    "doc2query": [],
                    "inferred_skills": [],
                    "dense_text": "",
                },
            )
            row["dense_text"] = dense_text(
                [row["raw_title"], row["description"], exp.get("company_name") or exp.get("company"), person.get("headline")]
            )
    rows = [by_hash[key] for key in sorted(by_hash)]
    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(
        output_dir / "raw_titles.jsonl",
        [{"title_hash": row["title_hash"], "raw_title": row["raw_title"], "description": row["description"]} for row in rows],
    )
    with (output_dir / "role_mapping.csv").open("w", newline="", encoding="utf-8") as handle:
        fieldnames = ["title_hash", "raw_title", "expanded_title", "seniority_band", "role_track"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row[key] for key in fieldnames})
    write_jsonl(output_dir / "roles_with_dense_text.jsonl", rows)
    write_json(stats, {"roles": len(rows)})
    return {
        "roles": len(rows),
        "outputs": {
            "raw_titles": str(output_dir / "raw_titles.jsonl"),
            "role_mapping": str(output_dir / "role_mapping.csv"),
            "roles_with_dense_text": str(output_dir / "roles_with_dense_text.jsonl"),
        },
    }


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--flattened", required=True)
    run_parser.add_argument("--output-dir", required=True)
    run_parser.add_argument("--stats", required=True)
    args = parser.parse_args()
    if args.cmd != "run":
        parser.error("subcommand required: run")
    print(json.dumps(run(Path(args.flattened), Path(args.output_dir), Path(args.stats)), sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
