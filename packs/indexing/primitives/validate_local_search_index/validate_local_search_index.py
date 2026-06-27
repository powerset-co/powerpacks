#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from packs.indexing.lib.duckdb_artifacts import validate_local_search_index  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a local Powerpacks DuckDB search index")
    parser.add_argument("run", nargs="?")
    parser.add_argument("--db", required=True)
    args = parser.parse_args()
    print(json.dumps(validate_local_search_index(args.db), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
