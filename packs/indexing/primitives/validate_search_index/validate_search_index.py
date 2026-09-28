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

Per person: everyone with work history in merged/people.csv must have
position rows in the index (Gmail-resolved people once reached it with none,
while the table-level counts still passed). Any miss fails.

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
# Skills run this file by path; `packs.*` needs the repo root importable.
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from packs.shared.csv_io import CsvIO  # noqa: E402
DEFAULT_DB = REPO / ".powerpacks/search-index/local-search.duckdb"
DEFAULT_PEOPLE_CSV = REPO / ".powerpacks/network-import/merged/people.csv"

# Either name is accepted as the person-profile table (store contract).
PROFILE_TABLE_CANDIDATES = ("local_person_profiles", "local_people_profiles")

# Must exist AND be non-empty for the index to be searchable.
REQUIRED_TABLES = (
    "local_people_positions",
    "local_summaries",
    "local_companies",
)

# Expected to exist but may be empty on sparse networks (no education listed).
# Empty => warning, not failure.
OPTIONAL_TABLES = (
    "local_people_education",
    "local_education",
)

# Reported for visibility but never warns/fails: this flow does not populate
# company signals, so an empty local_company_signals is expected and ignored.
INFO_TABLES = (
    "local_company_signals",
)


def existing_tables(con: duckdb.DuckDBPyConnection) -> set[str]:
    rows = con.execute(
        "select table_name from information_schema.tables"
    ).fetchall()
    return {r[0] for r in rows}


def row_count(con: duckdb.DuckDBPyConnection, table: str) -> int:
    return int(con.execute(f'select count(*) from "{table}"').fetchone()[0])


def people_missing_positions(con: duckdb.DuckDBPyConnection, people_csv: Path) -> tuple[int, int]:
    """(people with work history in the CSV, of those with no position rows)."""
    with_history = [row["id"] for row in CsvIO.read_dict_rows(people_csv)
                    if json.loads(row["work_experiences"] or "[]")]
    if not with_history:
        return 0, 0
    indexed = {r[0] for r in con.execute(
        "select distinct base_id from local_people_positions where base_id = any(?)", [with_history]).fetchall()}
    return len(with_history), len(set(with_history) - indexed)


def validate(db_path: Path, people_csv: Path | None = None) -> dict:
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

        for table in INFO_TABLES:
            exists = table in present
            rows = row_count(con, table) if exists else 0
            payload["tables"].append(
                {"name": table, "tier": "info", "exists": exists, "rows": rows}
            )
            # Informational only: never warns or fails (expected empty here).

        if people_csv is not None and people_csv.exists() and "local_people_positions" in present:
            with_history, missing = people_missing_positions(con, people_csv)
            payload["people_missing_positions"] = missing
            if missing:
                payload["errors"].append(
                    f"{missing} of {with_history} people with work history have no positions in the index")
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
    parser.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV),
                        help="the merged people the index was built from")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    payload = validate(Path(args.db), Path(args.people_csv))
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    sys.exit(0 if payload["status"] == "ok" else 1)


if __name__ == "__main__":
    main()
