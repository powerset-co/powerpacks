#!/usr/bin/env python3
"""wacli BINARY CLIENT CLI for WhatsApp metadata sync (openclaw/wacli).

The standalone lifecycle CLI over the `wacli/` package, which is the lower layer
of the WhatsApp discovery vertical (parallels `gmail/msgvault/`): download the
pinned fork, authenticate + render the login QR, run one metadata sync, deepen
recent shallow history in paced batches, refresh contacts/group info, and read
raw metadata rows out of the local wacli SQLite store. The client is invoked
in-process by `extract_whatsapp.WhatsAppExtractor` (the extractor composes it);
the wacli binary itself is still a subprocess (external tool).

The store lives under `.powerpacks/messages/wacli` (wacli keeps its own sync
state there). Every SQLite read selects only local metadata columns; it never
selects message body columns.

Stdlib-only.

Usage (the standalone lifecycle subcommands skills/tests invoke by path):
    whatsapp_wacli.py status       # show install/auth/store state
    whatsapp_wacli.py auth         # authenticate WhatsApp without syncing/exporting
    whatsapp_wacli.py ensure-wacli # download/refresh the pinned wacli binary
    whatsapp_wacli.py logout       # invalidate the session (re-link flow)

Flow: parse -> build the subcommand's payload from the package -> emit one
envelope -> map status to the exit code (20 blocked, 1 failed, else 0 —
an unlinked `status` reports and exits 0; `auth` ending unlinked exits 20).

The discovery `run`/`export` entry points (install → auth → sync → deepen →
export contacts) live in `extract_whatsapp.py`, which imports the same package.

Changelog:
- 2026-07-30 (wacli split): the 2,156-line client became the `wacli/` package
  (`paths`, `runtime`, `util`, `payloads`, `binary`, `store_db`, `qr`,
  `pairing`, `auth`, `sync`, `backfill`, `depth`, `depth_results`) and this
  file kept only the
  CLI: argparse, the `status` snapshot, dispatch, and the exit-code mapping.
  wacli's JSON is now parsed once into frozen dataclasses at the boundary
  (`wacli/payloads.py`). No re-export shim: consumers import the module that
  defines what they need. CLI flags, payloads, and exit codes are unchanged.
- 2026-07-26 (binary integrity + honest install flag): the pinned release
  download is verified against per-asset sha256 pins after download and BEFORE
  the binary is made executable or run. `ensure_wacli_installed(install=False)`
  now means it: `status` and `logout` report an existing binary as-is (even a
  stale pin) and a missing one raises `PrimitiveBlocked` naming the install path
  instead of silently pulling the ~33MB asset. The `status` exit code reflects
  the payload's `status` ("ok" -> 0) instead of exiting 1 for a
  healthy-but-unpaired install.
- 2026-07-24 (dedup): the local `parse_last_json` fork was deleted in favor of
  `common/jsonio.parse_last_json`. `run_command` is PINNED as deliberately
  divergent from `common/proc.run_cmd` (see the reasons at its definition). The
  CLI lost its `set_defaults(func=...)`/`args.func(args)` dispatcher: the four
  subcommands are payload-returning functions dispatched inline by `main()`.
- 2026-07-23 (extractor split): the `Contact` dataclass, the store→CSV/JSONL
  parse/write logic, the `WhatsAppWacli` orchestrator (now `WhatsAppExtractor`),
  and the `run`/`export` CLI subcommands moved to `extract_whatsapp.py`. This
  module keeps the wacli binary lifecycle and its standalone
  `status`/`auth`/`ensure-wacli`/`logout` subcommands. Import is
  one-directional: `extract_whatsapp` → the wacli client.
- 2026-07-23 (in-process): the outer `run` entry moved onto a class the WhatsApp
  channel calls in-process instead of spawning this file. The wacli GO BINARY is
  still invoked as a subprocess (external tool).
- 2026-07-23: whatsapp_wacli.README.md sidecar folded into this docstring.
- 2026-07-23: The isolated WhatsApp wrapper skill was retired; user-facing
  rerun hints now point at $import-messages and the status/User-Agent
  identifiers name this primitive directly.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import emit  # noqa: E402
from packs.ingestion.primitives.discover.messages.wacli import auth, binary, pairing, sync  # noqa: E402
from packs.ingestion.primitives.discover.messages.wacli.auth import (  # noqa: E402
    DEFAULT_AUTH_TIMEOUT,
    DEFAULT_IDLE_EXIT,
)
from packs.ingestion.primitives.discover.messages.wacli.paths import DEFAULT_STORE  # noqa: E402
from packs.ingestion.primitives.discover.messages.wacli.runtime import PrimitiveBlocked  # noqa: E402


def status_report(store: Path) -> dict[str, Any]:
    """Install / auth / pairing / doctor / store-size snapshot for one store."""
    wacli_info = binary.ensure_wacli_installed(install=False)
    status = auth.auth_status(store, include_linked_jid=True)
    doctor = binary.wacli_json(store, ["doctor"], timeout=60)
    stats = sync.store_stats(store)
    return {
        "status": "ok",
        "wacli": wacli_info,
        "auth": status,
        "pairing": pairing.pairing_full_sync_status(store, authenticated=bool(status.get("authenticated"))),
        "doctor": doctor,
        "store_stats": stats,
    }


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store", default=str(DEFAULT_STORE), help="wacli store directory")


def build_parser() -> argparse.ArgumentParser:
    """The standalone lifecycle CLI surface: status / ensure-wacli / auth /
    logout. Every subcommand except `ensure-wacli` takes `--store` (installing
    the binary never touches a store)."""
    parser = argparse.ArgumentParser(description="Manage the openclaw/wacli WhatsApp client (install, auth, status, logout)")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="show wacli install/auth/store status")
    add_common_args(status)

    sub.add_parser("ensure-wacli", help="download/refresh the pinned wacli binary to the current pin (idempotent)")

    auth_parser = sub.add_parser("auth", help="authenticate WhatsApp without syncing or exporting metadata")
    add_common_args(auth_parser)
    auth_parser.add_argument("--idle-exit", default=DEFAULT_IDLE_EXIT)
    auth_parser.add_argument("--auth-timeout", type=int, default=DEFAULT_AUTH_TIMEOUT)
    auth_parser.add_argument("--no-install", action="store_true", help="never download the pinned wacli binary; use the installed one, or block if none is present")
    auth_parser.add_argument("--no-open-qr-page", action="store_true", help="render QR artifacts without opening the local browser page")

    logout = sub.add_parser("logout", help="invalidate the WhatsApp session (re-link flow); next discovery shows a fresh QR")
    add_common_args(logout)
    return parser


def main() -> int:
    """The wacli client's standalone lifecycle CLI (status/auth/ensure-wacli/
    logout): parse, build the subcommand's payload, emit it, map status to the
    exit code (20 blocked, 1 failed or not-linked-on-status, else 0). One
    envelope and one error mapping for every subcommand. The discovery
    `run`/`export` entry points live in `extract_whatsapp.py`."""
    args = build_parser().parse_args()
    envelope: dict[str, Any] = {"primitive": "messages/whatsapp_wacli", "command": args.command}
    store = Path(args.store) if hasattr(args, "store") else None
    if store is not None:
        envelope["store"] = str(store)
    try:
        if args.command == "status":
            payload = status_report(store)
            emit({**envelope, **payload})
            # The exit code reflects the PAYLOAD status: a healthy-but-unpaired
            # install says "ok" and exits 0 (pairing state is in the payload).
            return 0 if payload.get("status") == "ok" else 1
        if args.command == "ensure-wacli":
            emit({**envelope, **binary.ensure_wacli_report()})
            return 0
        if args.command == "auth":
            payload = auth.auth_report(
                store,
                idle_exit=args.idle_exit,
                auth_timeout=args.auth_timeout,
                install=not args.no_install,
                open_qr_page=not args.no_open_qr_page,
            )
            emit({**envelope, **payload})
            return 0 if payload["status"] == "linked" else 20
        if args.command == "logout":
            emit({**envelope, **auth.logout_report(store)})
            return 0
        return 2
    except PrimitiveBlocked as exc:
        emit({**envelope, **exc.payload})
        return exc.code
    except Exception as exc:
        emit({**envelope, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
