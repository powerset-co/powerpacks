#!/usr/bin/env python3
"""Load local network/contact CSV artifacts into DuckDB.

Inputs are produced by ``merge_network_sources.py``:
- people.csv
- network_contacts.csv
- network_contact_sources.csv
- network_companies.csv

The resulting DuckDB is intended for local "My Contacts / Network" UI and
inspection flows. It keeps the profile/search shape (people) separate from the
source-evidence tables (contacts + contact_sources), mirroring the
network-search-api split between profile rows and operator_person_sources.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_NETWORK_DIR = Path(".powerpacks/network-import/merged")
DEFAULT_OUTPUT_DIR = Path(".powerpacks/network-import/duckdb")

TABLE_INPUTS = {
    "local_network_people": "people.csv",
    "local_network_contacts": "network_contacts.csv",
    "local_network_contact_sources": "network_contact_sources.csv",
    "local_network_companies": "network_companies.csv",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def load_csv_table(con: Any, table: str, path: Path) -> int:
    if not path.exists():
        raise SystemExit(f"missing required network artifact: {path}")
    con.execute(
        f"CREATE OR REPLACE TABLE {table} AS "
        "SELECT * FROM read_csv_auto(?, header=true, all_varchar=true, ignore_errors=false)",
        [str(path)],
    )
    return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def create_views_and_indexes(con: Any) -> None:
    con.execute("CREATE OR REPLACE VIEW network_people AS SELECT * FROM local_network_people")
    con.execute("CREATE OR REPLACE VIEW network_contacts AS SELECT * FROM local_network_contacts")
    con.execute("CREATE OR REPLACE VIEW network_contact_sources AS SELECT * FROM local_network_contact_sources")
    con.execute("CREATE OR REPLACE VIEW network_companies AS SELECT * FROM local_network_companies")
    # DuckDB indexes are opportunistic here: useful for local UI lookups, harmless
    # for tiny artifacts, and not part of the external contract.
    for sql in [
        "CREATE INDEX IF NOT EXISTS idx_network_contacts_merge_key ON local_network_contacts(merge_key)",
        "CREATE INDEX IF NOT EXISTS idx_network_contacts_public_identifier ON local_network_contacts(public_identifier)",
        "CREATE INDEX IF NOT EXISTS idx_network_contact_sources_contact ON local_network_contact_sources(contact_id)",
        "CREATE INDEX IF NOT EXISTS idx_network_contact_sources_channel ON local_network_contact_sources(source_channel)",
        "CREATE INDEX IF NOT EXISTS idx_network_companies_key ON local_network_companies(company_key)",
        "CREATE INDEX IF NOT EXISTS idx_network_companies_name ON local_network_companies(company_name)",
    ]:
        try:
            con.execute(sql)
        except Exception:
            pass


def build(args: argparse.Namespace) -> dict[str, Any]:
    try:
        import duckdb  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit("duckdb is required; run through `uv run --project . python ...`") from exc

    network_dir = Path(args.network_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    db_path = output_dir / f"network.{args.flavor}.duckdb"
    if db_path.exists():
        if args.force:
            db_path.unlink()
        else:
            raise SystemExit(f"DuckDB already exists: {db_path}. Use --force to replace it.")

    con = duckdb.connect(str(db_path))
    counts: dict[str, int] = {}
    try:
        for table, filename in TABLE_INPUTS.items():
            counts[table] = load_csv_table(con, table, network_dir / filename)
        create_views_and_indexes(con)
        con.execute("CHECKPOINT")
    finally:
        con.close()

    manifest = {
        "status": "ok",
        "created_at": now_iso(),
        "network_dir": str(network_dir),
        "output_dir": str(output_dir),
        "duckdb": str(db_path),
        "tables": counts,
        "views": ["network_people", "network_contacts", "network_contact_sources", "network_companies"],
        "source_contract": {
            "people": str(network_dir / "people.csv"),
            "contacts": str(network_dir / "network_contacts.csv"),
            "contact_sources": str(network_dir / "network_contact_sources.csv"),
            "companies": str(network_dir / "network_companies.csv"),
        },
    }
    manifest_path = output_dir / f"manifest.{args.flavor}.json"
    manifest["manifest"] = str(manifest_path)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network-dir", default=str(DEFAULT_NETWORK_DIR), help="Directory containing merged network CSV artifacts")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for DuckDB + manifest")
    parser.add_argument("--flavor", default="local", help="Output DB flavor suffix")
    parser.add_argument("--force", action="store_true")
    return parser


def main() -> None:
    emit(build(build_parser().parse_args()))


if __name__ == "__main__":
    main()
