#!/usr/bin/env python3
"""Validate that a local search index DuckDB is actually searchable.

The Modal pipeline downloads `local-search.duckdb` + `manifest.json` into
`.powerpacks/search-index/`. Checking the files exist (an `ls`) only proves the
download landed -- a truncated or empty-but-present DuckDB would pass. This
primitive opens the DuckDB read-only and verifies the tables the local search
backend actually reads exist and carry rows.

Table contract mirrors `packs/search/primitives/local/local_duckdb_store.py`
(`PERSON_PROFILE_TABLES` + `NAMESPACE_TABLES`) and the builder
`scripts/build-local-duckdb-shim.py` (`LOCAL_TABLES`). Two tiers:

  required -- no usable search without rows here: the person-profile table plus
              positions, summaries, and companies. Missing or empty => fail.
  optional -- legitimately sparse for some networks (education, schools,
              company signals). Missing or empty => warning, not failure.

Output is JSON on stdout. Exit code: 0 = ok (possibly with warnings),
1 = fail/missing DuckDB.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[4]
DEFAULT_DB = REPO / ".powerpacks/search-index/local-search.duckdb"

# Either name is accepted as the person-profile table (store contract).
PROFILE_TABLE_CANDIDATES = ("local_person_profiles", "local_people_profiles")

# Must exist AND be non-empty for the index to be searchable.
REQUIRED_TABLES = (
    "local_people_positions",
    "local_summaries",
    "local_companies",
)

# Expected to exist but may be empty on sparse networks (no education listed,
# no derived company signals). Empty => warning, not failure.
OPTIONAL_TABLES = (
    "local_people_education",
    "local_education",
    "local_company_signals",
)


def existing_tables(con: duckdb.DuckDBPyConnection) -> set[str]:
    rows = con.execute(
        "select table_name from information_schema.tables"
    ).fetchall()
    return {r[0] for r in rows}


def row_count(con: duckdb.DuckDBPyConnection, table: str) -> int:
    return int(con.execute(f'select count(*) from "{table}"').fetchone()[0])


def validate(db_path: Path) -> dict:
    payload: dict = {
        "primitive": "validate_search_index",
        "db": str(db_path),
        "profile_table": None,
        "tables": [],
        "errors": [],
        "warnings": [],
        "total_people": 0,
    }

    if not db_path.exists():
        payload["status"] = "missing"
        payload["errors"].append(f"DuckDB not found at {db_path}")
        payload["summary"] = f"Search index missing: {db_path} does not exist."
        return payload

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        present = existing_tables(con)

        # Person-profile table: accept either canonical name.
        profile_table = next((t for t in PROFILE_TABLE_CANDIDATES if t in present), None)
        payload["profile_table"] = profile_table
        if profile_table is None:
            payload["tables"].append(
                {"name": "/".join(PROFILE_TABLE_CANDIDATES), "tier": "required", "exists": False, "rows": 0}
            )
            payload["errors"].append(
                "no person-profile table (looked for "
                + " or ".join(PROFILE_TABLE_CANDIDATES) + ")"
            )
        else:
            rows = row_count(con, profile_table)
            payload["total_people"] = rows
            payload["tables"].append(
                {"name": profile_table, "tier": "required", "exists": True, "rows": rows}
            )
            if rows == 0:
                payload["errors"].append(f"{profile_table} has 0 rows")

        for table in REQUIRED_TABLES:
            exists = table in present
            rows = row_count(con, table) if exists else 0
            payload["tables"].append(
                {"name": table, "tier": "required", "exists": exists, "rows": rows}
            )
            if not exists:
                payload["errors"].append(f"required table {table} is missing")
            elif rows == 0:
                payload["errors"].append(f"required table {table} has 0 rows")

        for table in OPTIONAL_TABLES:
            exists = table in present
            rows = row_count(con, table) if exists else 0
            payload["tables"].append(
                {"name": table, "tier": "optional", "exists": exists, "rows": rows}
            )
            if not exists:
                payload["warnings"].append(f"optional table {table} is missing")
            elif rows == 0:
                payload["warnings"].append(f"optional table {table} is empty")
    finally:
        con.close()

    if payload["errors"]:
        payload["status"] = "fail"
        payload["summary"] = "Search index NOT ready: " + "; ".join(payload["errors"])
    elif payload["warnings"]:
        payload["status"] = "ok"
        payload["summary"] = (
            f"Search index ready: {payload['total_people']} people searchable "
            f"({len(payload['warnings'])} non-fatal warning(s))."
        )
    else:
        payload["status"] = "ok"
        payload["summary"] = f"Search index ready: {payload['total_people']} people searchable."
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB),
        help="path to local-search.duckdb (default: .powerpacks/search-index/local-search.duckdb)",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    payload = validate(Path(args.db))
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    sys.exit(0 if payload["status"] == "ok" else 1)


if __name__ == "__main__":
    main()
