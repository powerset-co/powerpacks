#!/usr/bin/env python3
"""Manage the local ingestion account registry.

No secrets are stored. The registry tracks usernames, linked/export state,
artifact paths, and setup notes for future runs/onboarding.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    from packs.ingestion.accounts import CHANNELS, DEFAULT_ACCOUNTS_PATH, default_registry, load_registry, save_registry, update_channel
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.accounts import CHANNELS, DEFAULT_ACCOUNTS_PATH, default_registry, load_registry, save_registry, update_channel


def emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if path.exists() and not args.force:
        emit({"status": "exists", "path": str(path), "registry": load_registry(path)})
        return 0
    registry = default_registry()
    save_registry(registry, path)
    emit({"status": "initialized", "path": str(path), "registry": registry})
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    path = Path(args.path)
    registry = load_registry(path)
    emit({"status": "ok", "path": str(path), "registry": registry})
    return 0


def cmd_mark(args: argparse.Namespace) -> int:
    registry = update_channel(
        args.channel,
        path=Path(args.path),
        linked=args.linked,
        skipped=args.skipped,
        username=args.username,
        artifact=args.artifact,
        notes=args.notes,
        success=args.success,
    )
    emit({"status": "updated", "path": args.path, "channel": args.channel, "registry": registry})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage .powerpacks/ingestion/accounts.json")
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--path", default=str(DEFAULT_ACCOUNTS_PATH))

    init = sub.add_parser("init", parents=[common])
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    status = sub.add_parser("status", parents=[common])
    status.set_defaults(func=cmd_status)

    mark = sub.add_parser("mark", parents=[common])
    mark.add_argument("--channel", required=True, choices=CHANNELS)
    mark.add_argument("--linked", action=argparse.BooleanOptionalAction, default=None)
    mark.add_argument("--success", action="store_true", help="Mark linked and record last_success_at")
    mark.add_argument("--skipped", action=argparse.BooleanOptionalAction, default=None, help="Mark source skipped/not skipped for onboarding")
    mark.add_argument("--username", default="")
    mark.add_argument("--artifact", default="")
    mark.add_argument("--notes", default=None)
    mark.set_defaults(func=cmd_mark)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
