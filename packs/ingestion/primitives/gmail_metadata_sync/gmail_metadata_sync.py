#!/usr/bin/env python3
"""Deprecated Gmail metadata sync entrypoint.

Powerpacks Gmail import is now msgvault-backed. This primitive intentionally no
longer calls Powerset-hosted Gmail OAuth/sync endpoints.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

MSGVAULT_COMMAND = "uv run --project . python packs/ingestion/primitives/gmail_network_import/gmail_network_import.py msgvault --db ~/.msgvault/msgvault.db --account-email <gmail-account-email>"


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def deprecated(args: argparse.Namespace) -> int:
    emit({
        "status": "deprecated",
        "primitive": "gmail_metadata_sync",
        "message": "Powerset-hosted Gmail metadata sync is disabled. Use local msgvault sync/import instead.",
        "replacement_command": MSGVAULT_COMMAND,
        "ledger": str(Path(getattr(args, "ledger", ".powerpacks/network-import/gmail/sync-run.json"))),
    })
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deprecated; use gmail_network_import.py msgvault")
    sub = parser.add_subparsers(dest="command")
    for name in ("run", "approve", "continue", "status"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--ledger", default=".powerpacks/network-import/gmail/sync-run.json")
        cmd.add_argument("--account-email", default="")
        cmd.add_argument("--api-url", default="")
        cmd.add_argument("--sync-path", default="")
        cmd.add_argument("--remote", action="store_true")
        cmd.set_defaults(func=deprecated)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not hasattr(args, "func"):
        emit({"status": "deprecated", "replacement_command": MSGVAULT_COMMAND})
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
