#!/usr/bin/env python3
"""Guided ingestion onboarding.

Shows link/export status for each supported network source and gives the next
command or user action. Persists non-secret account state to
`.powerpacks/ingestion/accounts.json`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from packs.ingestion.accounts import DEFAULT_ACCOUNTS_PATH, load_registry, update_channel
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.accounts import DEFAULT_ACCOUNTS_PATH, load_registry, update_channel


def emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def run_json(cmd: list[str]) -> dict[str, Any] | None:
    try:
        completed = subprocess.run(cmd, cwd=repo_root(), capture_output=True, text=True, timeout=90)
    except Exception:
        return None
    if completed.returncode not in (0, 20):
        return None
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None


def artifact_exists(path: str) -> bool:
    return bool(path and Path(path).exists())


def build_steps(registry: dict[str, Any]) -> list[dict[str, Any]]:
    acct = registry.get("accounts", {})
    return [
        {
            "channel": "messages",
            "linked": acct.get("messages", {}).get("linked", False),
            "what_it_needs": "Full Disk Access for iMessage and/or WAHA for WhatsApp, then messages import.",
            "next_action": "Run the import-contacts workflow if you want message/contact metadata.",
            "command": "uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py status",
        },
        {
            "channel": "gmail",
            "linked": acct.get("gmail", {}).get("linked", False),
            "what_it_needs": "Powerset Gmail OAuth connection. Sync itself is backend-side metadata only.",
            "next_action": "Connect at https://search.powerset.dev/gmail, then run gmail_network_import accounts.",
            "command": "uv run --project . python packs/ingestion/primitives/gmail_network_import/gmail_network_import.py accounts",
        },
        {
            "channel": "linkedin_csv",
            "linked": acct.get("linkedin_csv", {}).get("linked", False),
            "what_it_needs": "LinkedIn Connections.csv export from LinkedIn settings.",
            "next_action": "Export Connections.csv, then run linkedin_network_import run --csv <path> --source-user <label>.",
            "command": "uv run --project . python packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py run --csv <Connections.csv> --source-user <label>",
        },
        {
            "channel": "linkedin_mcp",
            "linked": acct.get("linkedin_mcp", {}).get("linked", False),
            "what_it_needs": "Install/login to stickerdaniel/linkedin-mcp-server via uvx or MCP client.",
            "next_action": "Run linkedin_mcp_import instructions, add the MCP config, and login when browser opens.",
            "command": "uv run --project . python packs/ingestion/primitives/linkedin_mcp_import/linkedin_mcp_import.py instructions",
        },
        {
            "channel": "twitter",
            "linked": acct.get("twitter", {}).get("linked", False),
            "what_it_needs": "Operator Twitter/X handle plus RapidAPI key for crawl.",
            "next_action": "Record handle with account_registry mark, then run twitter_network_import when ready.",
            "command": "uv run --project . python packs/ingestion/primitives/twitter_network_import/twitter_network_import.py run --handle <handle>",
        },
    ]


def cmd_status(args: argparse.Namespace) -> int:
    registry = load_registry(Path(args.accounts))
    emit({"status": "ok", "accounts_path": args.accounts, "registry": registry, "steps": build_steps(registry)})
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    path = Path(args.accounts)
    registry = load_registry(path)
    updates: list[str] = []

    # Gmail: use existing local token/backend stats if available.
    gmail = run_json(["uv", "run", "--project", ".", "python", "packs/ingestion/primitives/gmail_network_import/gmail_network_import.py", "accounts"])
    if gmail and gmail.get("status") == "ok":
        accounts = gmail.get("accounts") or gmail.get("connected_accounts") or []
        for account in accounts:
            email = account.get("email") or account.get("account_email") if isinstance(account, dict) else ""
            if email:
                update_channel("gmail", path=path, username=email, success=True, artifact="gmail-stats")
                updates.append(f"gmail:{email}")

    # Messages: mark linked if local contacts artifact exists.
    if artifact_exists(".powerpacks/messages/contacts.csv"):
        update_channel("messages", path=path, success=True, artifact=".powerpacks/messages/contacts.csv")
        updates.append("messages:contacts.csv")

    # LinkedIn CSV / Twitter: infer from provider-neutral local import artifacts.
    for channel_dir, registry_channel in (("linkedin", "linkedin_csv"), ("twitter", "twitter")):
        for run_dir in Path(f".powerpacks/network-import/{channel_dir}").glob("*"):
            p = run_dir / "people.csv"
            if p.exists():
                update_channel(registry_channel, path=path, success=True, artifact=str(p))
                updates.append(f"{registry_channel}:{p}")

    registry = load_registry(path)
    emit({"status": "checked", "accounts_path": args.accounts, "updates": updates, "registry": registry, "steps": build_steps(registry)})
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    registry = load_registry(Path(args.accounts))
    steps = build_steps(registry)
    todo = [step for step in steps if args.all or not step["linked"]]
    emit({"status": "plan", "accounts_path": args.accounts, "todo": todo, "already_linked": [s for s in steps if s["linked"]]})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Guided onboarding for local network ingestion sources")
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--accounts", default=str(DEFAULT_ACCOUNTS_PATH))
    status = sub.add_parser("status", parents=[common])
    status.set_defaults(func=cmd_status)
    check = sub.add_parser("check", parents=[common])
    check.set_defaults(func=cmd_check)
    plan = sub.add_parser("plan", parents=[common])
    plan.add_argument("--all", action="store_true")
    plan.set_defaults(func=cmd_plan)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
