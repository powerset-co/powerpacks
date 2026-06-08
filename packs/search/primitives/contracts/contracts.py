#!/usr/bin/env python3
"""Powerpacks contract diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PRIMITIVES_DIR = Path(__file__).resolve().parents[1]
LIB_DIR = PRIMITIVES_DIR / "lib"
SHARED_DIR = PRIMITIVES_DIR / "shared"
LOCAL_DIR = PRIMITIVES_DIR / "local"
TURBOPUFFER_DIR = PRIMITIVES_DIR / "turbopuffer"
for _path in [LIB_DIR, SHARED_DIR, LOCAL_DIR, TURBOPUFFER_DIR]:
    sys.path.insert(0, str(_path))

from postgres_client import check_required_postgres_columns, live_table_columns  # noqa: E402
from powerpacks_contracts import contracts_dir, load_contract  # noqa: E402


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def list_contracts(_args: argparse.Namespace) -> None:
    root = contracts_dir()
    rows = []
    for path in sorted(root.rglob("*.json")):
        rel = path.relative_to(root)
        try:
            contract = json.loads(path.read_text())
        except json.JSONDecodeError:
            contract = {}
        rows.append({
            "path": str(rel),
            "name": contract.get("name") or path.stem,
            "type": contract.get("type") or contract.get("title"),
            "version": contract.get("version"),
        })
    print_json({"contracts_dir": str(root), "contracts": rows})


def show_contract(args: argparse.Namespace) -> None:
    print_json(load_contract(args.path))


def check_postgres(args: argparse.Namespace) -> None:
    env_file = Path(args.env_file) if args.env_file else None
    result = check_required_postgres_columns(env_file=env_file)
    result["checked_at"] = now_iso()
    result["env_file"] = str(env_file) if env_file else None
    print_json(result)
    if not result.get("ok"):
        raise SystemExit(1)


def dump_postgres(args: argparse.Namespace) -> None:
    env_file = Path(args.env_file) if args.env_file else None
    dump = {
        "dumped_at": now_iso(),
        "env_file": str(env_file) if env_file else None,
        "tables": live_table_columns(env_file=env_file),
        "note": "Diagnostic live schema dump. Do not replace checked-in contracts without human review.",
    }
    if args.out:
        write_json(Path(args.out), dump)
    print_json(dump)


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect and validate Powerpacks data contracts")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List checked-in contracts")
    list_parser.set_defaults(func=list_contracts)

    show_parser = subparsers.add_parser("show", help="Show a checked-in contract")
    show_parser.add_argument("path", help="Path relative to powerpacks/contracts, e.g. postgres/persons.table.json")
    show_parser.set_defaults(func=show_contract)

    check_parser = subparsers.add_parser("check-postgres", help="Check live Postgres required columns")
    check_parser.add_argument("--env-file", default=".env")
    check_parser.set_defaults(func=check_postgres)

    dump_parser = subparsers.add_parser("dump-postgres", help="Dump live Postgres schema for contract review")
    dump_parser.add_argument("--env-file", default=".env")
    dump_parser.add_argument("--out")
    dump_parser.set_defaults(func=dump_postgres)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
