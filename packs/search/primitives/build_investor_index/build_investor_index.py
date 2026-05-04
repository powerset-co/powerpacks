#!/usr/bin/env python3
"""Build a TurboPuffer investor resolver namespace from a portable CSV."""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from pathlib import Path
from typing import Any


LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB_DIR))

from turbopuffer_client import load_env_file, namespace, namespace_name  # noqa: E402


TOKEN_RE = re.compile(r"[a-z0-9]+")


ALIASES = {
    "a16z": "Andreessen Horowitz",
    "yc": "Y Combinator",
    "sequoia": "Sequoia Capital",
}


def normalize_tokens(value: str) -> str:
    return " ".join(TOKEN_RE.findall(value.lower()))


def read_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        missing = {"urn", "name", "type", "investment_count"} - set(reader.fieldnames or [])
        if missing:
            raise RuntimeError(f"investor CSV missing required columns: {sorted(missing)}")
        for raw in reader:
            urn = (raw.get("urn") or "").strip()
            name = (raw.get("name") or "").strip()
            if not urn or not name:
                continue
            rows.append({
                "id": urn,
                "investor_name": name,
                "investor_name_tokens": normalize_tokens(name),
                "investor_type": (raw.get("type") or "").strip() or None,
                "investment_count": int(float(raw.get("investment_count") or 0)),
            })

    by_name = {row["investor_name"].lower(): row for row in rows}
    for alias, canonical in ALIASES.items():
        target = by_name.get(canonical.lower())
        if not target:
            continue
        rows.append({
            **target,
            "id": f"{target['id']}#alias:{alias}",
            "canonical_urn": target["id"],
            "investor_name": alias,
            "investor_name_tokens": normalize_tokens(alias),
        })
    return rows


def canonical_urn(row: dict[str, Any]) -> str:
    return str(row.get("canonical_urn") or row["id"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Powerpacks investor TurboPuffer namespace")
    parser.add_argument("--csv", required=True, help="Path to investors_full.csv-style file")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()

    load_env_file(Path(args.env_file) if args.env_file else None)
    rows = read_rows(Path(args.csv).expanduser())
    ns = namespace("investors")
    schema = {
        "investor_name": {"type": "string", "full_text_search": {"stemming": False}, "filterable": True},
        "investor_name_tokens": {"type": "string", "full_text_search": True},
        "investor_type": {"type": "string", "filterable": True},
        "investment_count": {"type": "uint"},
        "canonical_urn": {"type": "string"},
    }

    uploaded = 0
    for idx in range(0, len(rows), args.batch_size):
        batch = rows[idx : idx + args.batch_size]
        ns.write(upsert_rows=batch, schema=schema)
        uploaded += len(batch)
        print(f"uploaded {uploaded}/{len(rows)}", flush=True)

    print({
        "namespace": namespace_name("investors"),
        "rows": len(rows),
        "canonical_rows": len({canonical_urn(row) for row in rows}),
        "region": os.getenv("TURBOPUFFER_REGION", "gcp-us-central1"),
    })


if __name__ == "__main__":
    main()
