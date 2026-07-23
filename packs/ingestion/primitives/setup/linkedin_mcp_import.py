#!/usr/bin/env python3
"""LinkedIn MCP onboarding/import wrapper.

This does not vendor or import the WIP MCP server. It records local account
state and gives MCP install/login instructions for
https://github.com/stickerdaniel/linkedin-mcp-server.

Connection export is WIP upstream, so this primitive is currently a setup/status
surface plus optional artifact registration. When that MCP exposes a connection
export tool, add an export/apply step here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    from packs.ingestion.accounts import DEFAULT_ACCOUNTS_PATH, load_registry, update_channel
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.accounts import DEFAULT_ACCOUNTS_PATH, load_registry, update_channel

MCP_REPO = "https://github.com/stickerdaniel/linkedin-mcp-server"
MCP_PACKAGE = "linkedin-scraper-mcp@latest"


def emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def mcp_config() -> dict[str, Any]:
    return {
        "mcpServers": {
            "linkedin": {
                "command": "uvx",
                "args": [MCP_PACKAGE],
                "env": {"UV_HTTP_TIMEOUT": "300"},
            }
        }
    }


def cmd_instructions(args: argparse.Namespace) -> int:
    payload = {
        "status": "instructions",
        "repo": MCP_REPO,
        "package": MCP_PACKAGE,
        "notes": [
            "This MCP controls a real browser session; LinkedIn automation has ToS/account-risk considerations.",
            "Connection export is WIP upstream; current useful tools include get_my_profile, get_person_profile, search_people, get_company_employees.",
            "No LinkedIn credentials are stored in Powerpacks accounts.json.",
        ],
        "mcp_config": mcp_config(),
        "login_command": "uvx linkedin-scraper-mcp@latest --login",
        "record_command": f"uv run --project . python packs/ingestion/primitives/setup/linkedin_mcp_import.py mark-linked --username <linkedin-username-or-profile-url>",
    }
    emit(payload)
    return 0


def cmd_mark_linked(args: argparse.Namespace) -> int:
    registry = update_channel(
        "linkedin_mcp",
        path=Path(args.accounts),
        username=args.username,
        artifact=args.artifact,
        notes=args.notes or f"Configured via {MCP_REPO}",
        success=True,
    )
    emit({"status": "linked", "channel": "linkedin_mcp", "accounts": args.accounts, "registry": registry})
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    registry = load_registry(Path(args.accounts))
    emit({"status": "ok", "channel": "linkedin_mcp", "accounts": args.accounts, "record": registry.get("accounts", {}).get("linkedin_mcp", {})})
    return 0


def cmd_register_artifact(args: argparse.Namespace) -> int:
    registry = update_channel(
        "linkedin_mcp",
        path=Path(args.accounts),
        artifact=args.artifact,
        username=args.username,
        notes=args.notes,
        success=True,
    )
    emit({"status": "artifact_registered", "artifact": args.artifact, "registry": registry})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LinkedIn MCP setup/status wrapper")
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--accounts", default=str(DEFAULT_ACCOUNTS_PATH))
    inst = sub.add_parser("instructions", parents=[common])
    inst.set_defaults(func=cmd_instructions)
    mark = sub.add_parser("mark-linked", parents=[common])
    mark.add_argument("--username", required=True)
    mark.add_argument("--artifact", default="")
    mark.add_argument("--notes", default="")
    mark.set_defaults(func=cmd_mark_linked)
    status = sub.add_parser("status", parents=[common])
    status.set_defaults(func=cmd_status)
    artifact = sub.add_parser("register-artifact", parents=[common])
    artifact.add_argument("--artifact", required=True)
    artifact.add_argument("--username", default="")
    artifact.add_argument("--notes", default="")
    artifact.set_defaults(func=cmd_register_artifact)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
