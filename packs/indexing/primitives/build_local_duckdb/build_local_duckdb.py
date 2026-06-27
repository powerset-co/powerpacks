#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from packs.indexing.lib.duckdb_artifacts import build_local_duckdb  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Build local-search.duckdb from a Powerpacks search-index run directory")
    parser.add_argument("run", nargs="?")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--db")
    args = parser.parse_args()
    print(json.dumps(build_local_duckdb(args.run_dir, args.db), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
