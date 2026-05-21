#!/usr/bin/env python3
"""Guided ingestion onboarding.

Shows link/export status for each supported network source and gives the next
command or user action. Persists non-secret account state to
`.powerpacks/ingestion/accounts.json`.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from packs.ingestion.accounts import DEFAULT_ACCOUNTS_PATH, load_registry, now_iso, save_registry, update_channel
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.accounts import DEFAULT_ACCOUNTS_PATH, load_registry, now_iso, save_registry, update_channel


DEFAULT_MSGVAULT_DB = Path(os.environ.get("MSGVAULT_HOME", str(Path.home() / ".msgvault"))) / "msgvault.db"
DEFAULT_NETWORK_IMPORT_DIR = Path(".powerpacks/network-import")
ONBOARDING_SOURCE_ORDER = ["gmail", "linkedin_csv", "messages", "twitter"]


def emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def run_command(cmd: list[str], *, timeout: int = 90) -> tuple[int, dict[str, Any] | None, str]:
    try:
        completed = subprocess.run(cmd, cwd=repo_root(), capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return 1, None, str(exc)
    payload = None
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = None
    return completed.returncode, payload, completed.stderr


def run_json(cmd: list[str]) -> dict[str, Any] | None:
    code, payload, _ = run_command(cmd)
    if code not in (0, 20):
        return None
    return payload


def artifact_exists(path: str) -> bool:
    return bool(path and Path(path).exists())


def csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        return list(csv.DictReader(handle))


def json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def set_channel_from_artifacts(
    channel: str,
    *,
    path: Path,
    linked: bool,
    usernames: list[str] | None = None,
    artifacts: list[str] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    registry = load_registry(path)
    rec = registry["accounts"][channel]
    rec["linked"] = linked
    if linked:
        rec["skipped"] = False
    rec["usernames"] = list(dict.fromkeys(usernames or []))
    rec["artifacts"] = list(dict.fromkeys(artifacts or []))
    rec["notes"] = notes
    rec["last_checked_at"] = now_iso()
    rec["last_success_at"] = rec["last_checked_at"] if linked else ""
    save_registry(registry, path)
    return registry


def refresh_registry(path: Path) -> tuple[dict[str, Any], list[str]]:
    registry = load_registry(path)
    # Task 2 onboarding is link-only. Do not infer linked state from child import
    # artifacts or continue child ledgers here; only explicit source-link config
    # written by this command/account_registry counts.
    return registry, []


def build_steps(registry: dict[str, Any]) -> list[dict[str, Any]]:
    acct = registry.get("accounts", {})
    steps_by_channel = {
        "gmail": {
            "channel": "gmail",
            "linked": acct.get("gmail", {}).get("linked", False),
            "skipped": acct.get("gmail", {}).get("skipped", False),
            "what_it_needs": "Local msgvault SQLite archive with Gmail metadata, usually ~/.msgvault/msgvault.db.",
            "next_action": "Link local msgvault.db and choose one or more Gmail source accounts. No Gmail network import runs during onboarding.",
            "command": "uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py step --gmail-db ~/.msgvault/msgvault.db",
        },
        "linkedin_csv": {
            "channel": "linkedin_csv",
            "linked": acct.get("linkedin_csv", {}).get("linked", False),
            "skipped": acct.get("linkedin_csv", {}).get("skipped", False),
            "what_it_needs": "LinkedIn Connections.csv export from LinkedIn settings.",
            "next_action": "Export Connections.csv, then record it with onboarding --linkedin-csv <path> --linkedin-source-user <label>.",
            "command": "uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py step --linkedin-csv <Connections.csv> --linkedin-source-user <label>",
        },
        "messages": {
            "channel": "messages",
            "linked": acct.get("messages", {}).get("linked", False),
            "skipped": acct.get("messages", {}).get("skipped", False),
            "what_it_needs": "Existing messages contacts CSV path, if you want message/contact metadata included later.",
            "next_action": "Record an existing contacts CSV with onboarding --messages-contacts-csv <path>, or skip.",
            "command": "uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py step --messages-contacts-csv <contacts.csv>",
        },
        "twitter": {
            "channel": "twitter",
            "linked": acct.get("twitter", {}).get("linked", False),
            "skipped": acct.get("twitter", {}).get("skipped", False),
            "what_it_needs": "Operator Twitter/X handle.",
            "next_action": "Record handle with onboarding --twitter-handle <handle>. No crawl runs during onboarding.",
            "command": "uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py step --twitter-handle <handle>",
        },
    }
    return [steps_by_channel[channel] for channel in ONBOARDING_SOURCE_ORDER]


def cmd_status(args: argparse.Namespace) -> int:
    registry = load_registry(Path(args.accounts))
    emit({"status": "ok", "accounts_path": args.accounts, "registry": registry, "steps": build_steps(registry)})
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    path = Path(args.accounts)
    registry, updates = refresh_registry(path)
    emit({"status": "checked", "accounts_path": args.accounts, "updates": updates, "registry": registry, "steps": build_steps(registry)})
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    registry = load_registry(Path(args.accounts))
    steps = build_steps(registry)
    todo = [step for step in steps if args.all or (not step["linked"] and not step.get("skipped"))]
    emit({"status": "plan", "accounts_path": args.accounts, "todo": todo, "already_linked": [s for s in steps if s["linked"]], "skipped": [s for s in steps if s.get("skipped")]})
    return 0


def shell_join(parts: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def onboarding_step_command(args: argparse.Namespace, *, placeholders: bool = False) -> str:
    cmd = [
        "uv", "run", "--project", ".", "python",
        "packs/ingestion/primitives/onboarding/onboarding.py", "step",
        "--accounts", args.accounts,
        "--gmail-db", args.gmail_db,
        "--gmail-output-dir", args.gmail_output_dir,
    ]
    if placeholders:
        cmd.extend(["--linkedin-csv", "<Connections.csv>", "--linkedin-source-user", "<label>"])
    else:
        if args.linkedin_csv:
            cmd.extend(["--linkedin-csv", args.linkedin_csv])
        if args.linkedin_source_user:
            cmd.extend(["--linkedin-source-user", args.linkedin_source_user])
        if getattr(args, "messages_contacts_csv", ""):
            cmd.extend(["--messages-contacts-csv", args.messages_contacts_csv])
        if getattr(args, "twitter_handle", ""):
            cmd.extend(["--twitter-handle", args.twitter_handle])
    return shell_join(cmd)


def setup_handoff_command(args: argparse.Namespace) -> str:
    return shell_join([
        "uv", "run", "--project", ".", "python",
        "packs/ingestion/primitives/setup/setup.py", "handoff",
        "--operator-id", args.operator_id,
        "--accounts", args.accounts,
        "--setup-ledger", ".powerpacks/setup/setup-run.json",
    ])


def onboarding_handoff(args: argparse.Namespace, registry: dict[str, Any]) -> dict[str, Any]:
    steps = build_steps(registry)
    linked = [step["channel"] for step in steps if step["linked"]]
    skipped = [step["channel"] for step in steps if step.get("skipped")]
    return {
        "status": "ready_for_import",
        "accounts_path": args.accounts,
        "source_order": ONBOARDING_SOURCE_ORDER,
        "linked_sources": linked,
        "skipped_sources": skipped,
        "handoff_command": setup_handoff_command(args),
        "confirmation_prompt": (
            "Your sources are connected. I can now import them, combine the results into one local network, "
            "and prepare the files needed for local search. Large mailboxes or networks can take a while. "
            "I won't upload anything automatically, and I'll only ask again if a login, QR/device link, "
            "overwrite, or paid provider step needs approval. Continue?"
        ),
        "codex_orchestration": {
            "main_thread": "Handle account linking, browser/login actions, user confirmations, and worker handoffs.",
            "flow": "Run handoff_command next. The setup handoff owns import worker planning, approval gates, fan-in, and indexing readiness.",
            "user_summary": "Import connected sources in parallel where possible, combine them into one local network, then prepare local search. Do not describe ledgers/fan-in/fan-out to normal users.",
            "worker_policy": "Use setup.py handoff worker_groups to dispatch import workers. Do not use legacy direct onboarding worker phases.",
        },
    }


def msgvault_setup_py() -> str:
    return "packs/ingestion/primitives/msgvault_setup/msgvault_setup.py"


def msgvault_home_from_args(args: argparse.Namespace) -> Path:
    return Path(args.gmail_db).expanduser().parent


def msgvault_config_path(args: argparse.Namespace) -> Path:
    return msgvault_home_from_args(args) / "config.toml"


def msgvault_oauth_configured(args: argparse.Namespace) -> bool:
    path = msgvault_config_path(args)
    if not path.exists():
        return False
    try:
        return "client_secrets" in path.read_text(encoding="utf-8")
    except OSError:
        return False


def msgvault_home_args(args: argparse.Namespace) -> list[str]:
    home = msgvault_home_from_args(args)
    default_home = Path("~/.msgvault").expanduser()
    return ["--home", str(home)] if home != default_home else []


def discover_msgvault_accounts(args: argparse.Namespace) -> tuple[int, dict[str, Any] | None, str]:
    db = Path(args.gmail_db).expanduser()
    try:
        with sqlite3.connect(db) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT identifier AS account_email, display_name, COUNT(messages.id) AS message_count
                FROM sources
                LEFT JOIN messages ON messages.source_id = sources.id
                WHERE lower(source_type) = 'gmail' AND identifier IS NOT NULL AND identifier != ''
                GROUP BY sources.id, identifier, display_name
                ORDER BY identifier
                """
            ).fetchall()
    except Exception as exc:
        return 1, None, str(exc)
    accounts = []
    for row in rows:
        email = str(row["account_email"]).strip().lower()
        if email:
            accounts.append({"account_email": email, "display_name": row["display_name"] or "", "message_count": row["message_count"] or 0})
    return 0, {"status": "ok", "db": str(db), "accounts": accounts}, ""


def run_gmail_accounts_command(args: argparse.Namespace) -> str:
    return onboarding_step_command(args)


def normalize_email(value: str) -> str:
    email = (value or "").strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise ValueError(f"invalid email: {value}")
    return email


def save_link_config(channel: str, path: Path, *, config: dict[str, Any], usernames: list[str] | None = None, artifacts: list[str] | None = None, notes: str = "") -> dict[str, Any]:
    registry = load_registry(path)
    rec = registry["accounts"][channel]
    rec.setdefault("config", {}).update(config)
    if usernames is not None:
        rec["usernames"] = list(dict.fromkeys([u for u in usernames if u]))
    if artifacts is not None:
        rec["artifacts"] = list(dict.fromkeys([a for a in artifacts if a]))
    rec["linked"] = True
    rec["skipped"] = False
    rec["notes"] = notes
    rec["last_checked_at"] = now_iso()
    rec["last_success_at"] = rec["last_checked_at"]
    save_registry(registry, path)
    return registry


def email_slug(email: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", email.lower()).strip("-")
    return slug or "gmail"


def gmail_background_sync_command(email: str) -> dict[str, str]:
    slug = email_slug(email)
    session = f"powerpacks-msgvault-sync-{slug}"
    log_path = f".powerpacks/ingestion/logs/msgvault-sync-{slug}.log"
    pid_path = f".powerpacks/ingestion/pids/msgvault-sync-{slug}.pid"
    script = (
        "mkdir -p .powerpacks/ingestion/logs .powerpacks/ingestion/pids; "
        f"session={shlex.quote(session)}; "
        f"log={shlex.quote(log_path)}; "
        f"pid={shlex.quote(pid_path)}; "
        "if command -v tmux >/dev/null 2>&1; then "
        f"tmux has-session -t \"$session\" 2>/dev/null || tmux new-session -d -s \"$session\" \"cd '$PWD' && msgvault sync-full {shlex.quote(email)} >> '$PWD'/\"$log\" 2>&1\"; "
        "tmux display-message -p -t \"$session\" '#{pane_pid}' > \"$pid\"; "
        f"printf 'Started Gmail sync for {email} (tmux %s, pid %s, log %s)\\n' \"$session\" \"$(cat \"$pid\")\" \"$log\"; "
        "else "
        f"nohup msgvault sync-full {shlex.quote(email)} >> \"$log\" 2>&1 & "
        "echo $! > \"$pid\"; "
        f"printf 'Started Gmail sync for {email} (pid %s, log %s)\\n' \"$(cat \"$pid\")\" \"$log\"; "
        "fi"
    )
    return {
        "label": f"start_msgvault_sync_{email}",
        "command": shell_join(["sh", "-lc", script]),
        "description": (
            f"Start local Gmail metadata sync for {email} in the background. "
            "Large mailboxes can take a few hours; rerun onboarding after a checkpoint exists."
        ),
    }


def gmail_browser_setup_command(args: argparse.Namespace, email: str) -> dict[str, str]:
    cmd = [
        "uv", "run", "--project", ".", "python", msgvault_setup_py(), "browser-setup",
        "--email", email,
        "--add-account",
        *msgvault_home_args(args),
    ]
    return {
        "label": f"create_oauth_app_and_authorize_{email}",
        "command": shell_join(cmd),
        "description": f"Create the local-msg-vault Google OAuth app, configure msgvault, and authorize {email}.",
    }


def gmail_add_email_commands(args: argparse.Namespace, emails: list[str]) -> list[dict[str, str]]:
    if not msgvault_oauth_configured(args):
        first, rest = emails[0], emails[1:]
        commands = [gmail_browser_setup_command(args, first)]
        if rest:
            commands.append({
                "label": "add_oauth_test_users",
                "command": shell_join([
                    "uv", "run", "--project", ".", "python", msgvault_setup_py(), "add-test-users",
                    *rest,
                    *msgvault_home_args(args),
                ]),
                "description": "Add remaining Gmail addresses as Google OAuth test users with browser automation.",
            })
            for email in rest:
                commands.append({
                    "label": f"authorize_{email}",
                    "command": shell_join([
                        "uv", "run", "--project", ".", "python", msgvault_setup_py(), "add-account",
                        "--email", email,
                        *msgvault_home_args(args),
                    ]),
                    "description": f"Authorize {email} in msgvault.",
                })
        for email in emails:
            commands.append(gmail_background_sync_command(email))
        commands.append({
            "label": "rerun_onboarding",
            "command": onboarding_step_command(args),
            "description": "Rerun onboarding after msgvault has written a Gmail sync checkpoint.",
        })
        return commands

    add_users = [
        "uv", "run", "--project", ".", "python", msgvault_setup_py(), "add-test-users",
        *emails,
        *msgvault_home_args(args),
    ]
    commands = [{
        "label": "add_oauth_test_users",
        "command": shell_join(add_users),
        "description": "Add the Gmail addresses as Google OAuth test users with browser automation.",
    }]
    for email in emails:
        commands.append({
            "label": f"authorize_{email}",
            "command": shell_join([
                "uv", "run", "--project", ".", "python", msgvault_setup_py(), "add-account",
                "--email", email,
                *msgvault_home_args(args),
            ]),
            "description": f"Authorize {email} in msgvault.",
        })
    for email in emails:
        commands.append(gmail_background_sync_command(email))
    commands.append({
        "label": "rerun_onboarding",
        "command": onboarding_step_command(args),
        "description": "Rerun onboarding after msgvault has written a Gmail sync checkpoint.",
    })
    return commands


def cmd_step(args: argparse.Namespace) -> int:
    """Idempotent one-step onboarding loop for CLI/harness use.

    The command is safe to run repeatedly. It refreshes accounts.json from local
    artifacts first, then returns the next missing action. LinkedIn CSV is
    recorded as linked input here; import-network owns the later parse/enrich
    work and any paid provider approvals.
    """
    path = Path(args.accounts)
    registry, updates = refresh_registry(path)
    accounts = registry.get("accounts", {})

    skip_sources = set(args.skip_source or [])
    if args.skip_gmail:
        skip_sources.add("gmail")
    if skip_sources:
        skipped: list[str] = []
        for channel in sorted(skip_sources):
            record = accounts.get(channel, {})
            if not record.get("linked", False):
                registry = update_channel(channel, path=path, linked=False, skipped=True, notes="Skipped by operator during onboarding")
                skipped.append(channel)
        if skipped:
            emit({
                "status": "skipped",
                "channels": skipped,
                "message": "Skipped selected onboarding sources.",
                "accounts_path": args.accounts,
                "registry": registry,
                "next_command": onboarding_step_command(args),
            })
            return 0

    gmail_record = accounts.get("gmail", {})
    gmail_action_requested = bool(args.gmail_account or args.gmail_all or args.gmail_add_email)
    if (not gmail_record.get("linked", False) and not gmail_record.get("skipped", False)) or gmail_action_requested:
        try:
            extra_emails = list(dict.fromkeys(normalize_email(email) for email in (args.gmail_add_email or [])))
        except ValueError as exc:
            emit({
                "status": "needs_input",
                "channel": "gmail",
                "message": str(exc),
                "repeat_command": onboarding_step_command(args),
            })
            return 20
        if extra_emails:
            gmail_cfg = gmail_record.get("config", {}) if isinstance(gmail_record.get("config"), dict) else {}
            registry = load_registry(path)
            rec = registry["accounts"]["gmail"]
            rec.setdefault("config", {})["msgvault_db"] = str(Path(args.gmail_db).expanduser())
            rec["config"]["account_emails"] = list(dict.fromkeys([*(gmail_cfg.get("account_emails") or []), *extra_emails]))
            rec["config"]["oauth_test_users"] = list(dict.fromkeys([*(gmail_cfg.get("oauth_test_users") or []), *extra_emails]))
            rec["last_checked_at"] = now_iso()
            rec["notes"] = "Gmail account linking requested; run user-action setup commands. No Gmail network import was run."
            save_registry(registry, path)
            emit({
                "status": "needs_agent_action",
                "channel": "gmail",
                "message": "Adding these Gmail accounts requires browser automation and msgvault authorization. These are user-action/linking commands only; no Gmail network import is run during onboarding.",
                "action_type": "user-action/linking",
                "emails": extra_emails,
                "commands": gmail_add_email_commands(args, extra_emails),
                "repeat_command_after_sync": onboarding_step_command(args),
                "accounts_path": args.accounts,
                "updates": updates,
                "registry": registry,
            })
            return 20

        db_path = Path(args.gmail_db).expanduser()
        if not db_path.exists():
            emit({
                "status": "needs_input",
                "channel": "gmail",
                "prompt": "Which Gmail address should we link first? Do not infer it from gcloud, Powerset login, or local machine state. After the user provides an email, rerun with --gmail-add-email <email>. Large mailboxes can take a few hours to fully sync, so Codex should start the sync while the user is here.",
                "question": "Which Gmail address should we link first?",
                "email_source": "user_provided",
                "msgvault_db": str(db_path),
                "example_sync": "msgvault sync-full <email>",
                "next_command": onboarding_step_command(args),
                "first_gmail_command": onboarding_step_command(args) + " --gmail-add-email EMAIL",
                "add_other_email_command": onboarding_step_command(args) + " --gmail-add-email EMAIL",
                "skip_command": onboarding_step_command(args) + " --skip-gmail",
                "accounts_path": args.accounts,
                "updates": updates,
            })
            return 20

        code, discovered, stderr = discover_msgvault_accounts(args)
        if code != 0 or not discovered:
            emit({
                "status": "blocked",
                "channel": "gmail",
                "message": "Could not list Gmail accounts from msgvault.",
                "accounts_command": run_gmail_accounts_command(args),
                "stderr": stderr,
                "repeat_command": onboarding_step_command(args),
            })
            return code or 1
        discovered_accounts = [row.get("account_email", "") for row in discovered.get("accounts", []) if row.get("account_email")]
        if not discovered_accounts:
            emit({
                "status": "waiting",
                "channel": "gmail",
                "message": "No Gmail source accounts found in msgvault yet. Start msgvault sync for the linked Gmail account, keep it running while the user is present, then rerun onboarding after a checkpoint exists.",
                "accounts_command": run_gmail_accounts_command(args),
                "repeat_command": onboarding_step_command(args),
                "skip_command": onboarding_step_command(args) + " --skip-gmail",
            })
            return 20

        selected = discovered_accounts if args.gmail_all else [email.strip().lower() for email in args.gmail_account if email.strip()]
        if not selected:
            emit({
                "status": "needs_input",
                "channel": "gmail",
                "prompt": "Confirm discovered Gmail accounts to link with --gmail-account <email> or --gmail-all. Message counts are local sync checkpoints; large mailboxes can keep syncing in the background for a few hours. Tell me any other Gmail addresses you want to add; use --gmail-add-email <email> so onboarding can add OAuth test users and authorize them as user-action/linking.",
                "discovered_accounts": discovered.get("accounts", []),
                "add_commands": [onboarding_step_command(args) + f" --gmail-account {shlex.quote(email)}" for email in discovered_accounts],
                "all_command": onboarding_step_command(args) + " --gmail-all",
                "add_other_email_command": onboarding_step_command(args) + " --gmail-add-email EMAIL",
                "skip_command": onboarding_step_command(args) + " --skip-gmail",
                "accounts_path": args.accounts,
                "updates": updates,
            })
            return 20

        unknown = sorted(set(selected) - set(discovered_accounts))
        if unknown:
            emit({
                "status": "needs_input",
                "channel": "gmail",
                "message": "Some requested Gmail accounts were not found in msgvault.",
                "unknown_accounts": unknown,
                "discovered_accounts": discovered.get("accounts", []),
                "repeat_command": onboarding_step_command(args),
            })
            return 20

        available = [row.get("account_email", "") for row in discovered.get("accounts", []) if row.get("account_email")]
        existing = accounts.get("gmail", {}).get("config", {})
        account_emails = list(dict.fromkeys([*(existing.get("account_emails") or []), *available, *selected]))
        registry = save_link_config(
            "gmail",
            path,
            config={
                "msgvault_db": str(db_path),
                "account_emails": account_emails,
                "available_accounts": available,
                "selected_accounts": selected,
            },
            usernames=selected,
            artifacts=[],
            notes="Linked local msgvault Gmail source accounts; no network import run during onboarding.",
        )
        emit({
            "status": "progressed",
            "channel": "gmail",
            "message": "Recorded selected Gmail msgvault source accounts in accounts.json. No Gmail network import was run.",
            "linked_accounts": selected,
            "discovered_accounts": discovered.get("accounts", []),
            "accounts_path": args.accounts,
            "registry": registry,
            "next_command": onboarding_step_command(args),
        })
        return 0

    if not accounts.get("linkedin_csv", {}).get("linked", False) and not accounts.get("linkedin_csv", {}).get("skipped", False):
        if not args.linkedin_csv:
            emit({
                "status": "needs_input",
                "channel": "linkedin_csv",
                "prompt": "Export LinkedIn Connections.csv, then rerun with --linkedin-csv <path> --linkedin-source-user <label>.",
                "next_command": onboarding_step_command(args, placeholders=True),
                "accounts_path": args.accounts,
                "updates": updates,
            })
            return 20
        csv_path = Path(args.linkedin_csv).expanduser()
        if not csv_path.exists():
            emit({
                "status": "waiting",
                "channel": "linkedin_csv",
                "message": f"Waiting for LinkedIn Connections.csv at {csv_path}.",
                "repeat_command": onboarding_step_command(args),
                "accounts_path": args.accounts,
                "updates": updates,
            })
            return 20
        if not args.linkedin_source_user:
            emit({
                "status": "needs_input",
                "channel": "linkedin_csv",
                "prompt": "Provide a non-secret source label for this LinkedIn export with --linkedin-source-user <label>.",
                "next_command": onboarding_step_command(args, placeholders=True),
                "accounts_path": args.accounts,
                "updates": updates,
            })
            return 20

        registry = save_link_config(
            "linkedin_csv",
            path,
            config={"csv_path": str(csv_path), "source_label": args.linkedin_source_user},
            usernames=[args.linkedin_source_user],
            artifacts=[str(csv_path)],
            notes="Linked LinkedIn Connections.csv export; no LinkedIn import run during onboarding.",
        )
        emit({
            "status": "progressed",
            "channel": "linkedin_csv",
            "message": "Recorded LinkedIn CSV source link in accounts.json. No LinkedIn network import was run.",
            "updates": updates,
            "registry": registry,
            "next_command": onboarding_step_command(args),
        })
        return 0

    if not accounts.get("messages", {}).get("linked", False) and not accounts.get("messages", {}).get("skipped", False):
        if not args.messages_contacts_csv:
            emit({
                "status": "needs_input",
                "channel": "messages",
                "prompt": "Provide an existing contacts CSV with --messages-contacts-csv <path>, or skip messages. Onboarding will only record the source link.",
                "next_command": onboarding_step_command(args) + " --messages-contacts-csv <contacts.csv>",
                "skip_command": onboarding_step_command(args) + " --skip-source messages",
                "accounts_path": args.accounts,
                "updates": updates,
            })
            return 20
        contacts_csv = Path(args.messages_contacts_csv).expanduser()
        if not contacts_csv.exists():
            emit({"status": "waiting", "channel": "messages", "message": f"Waiting for contacts CSV at {contacts_csv}.", "repeat_command": onboarding_step_command(args)})
            return 20
        registry = save_link_config("messages", path, config={"contacts_csv": str(contacts_csv)}, artifacts=[str(contacts_csv)], notes="Linked messages contacts CSV; no messages import/research run during onboarding.")
        emit({"status": "progressed", "channel": "messages", "message": "Recorded messages contacts CSV source link. No messages import/research was run.", "registry": registry, "next_command": onboarding_step_command(args)})
        return 0

    if not accounts.get("twitter", {}).get("linked", False) and not accounts.get("twitter", {}).get("skipped", False):
        handle = (args.twitter_handle or "").strip().lstrip("@")
        if not handle:
            emit({
                "status": "needs_input",
                "channel": "twitter",
                "prompt": "Provide your Twitter/X handle with --twitter-handle <handle>, or skip Twitter. Onboarding will only record the handle.",
                "next_command": onboarding_step_command(args) + " --twitter-handle <handle>",
                "skip_command": onboarding_step_command(args) + " --skip-source twitter",
                "accounts_path": args.accounts,
                "updates": updates,
            })
            return 20
        registry = save_link_config("twitter", path, config={"handle": handle}, usernames=[handle], notes="Linked Twitter/X handle; no Twitter crawl run during onboarding.")
        emit({"status": "progressed", "channel": "twitter", "message": "Recorded Twitter/X handle. No Twitter crawl was run.", "registry": registry, "next_command": onboarding_step_command(args)})
        return 0

    steps = build_steps(registry)
    non_optional_todo = [step for step in steps if not step["linked"] and not step.get("skipped")]
    if non_optional_todo:
        emit({
            "status": "next_action",
            "accounts_path": args.accounts,
            "updates": updates,
            "next": non_optional_todo[0],
            "todo": non_optional_todo,
            "repeat_command": onboarding_step_command(args),
        })
        return 20

    emit({
        "status": "completed",
        "accounts_path": args.accounts,
        "updates": updates,
        "registry": registry,
        "handoff": onboarding_handoff(args, registry),
        "message": "Required onboarding sources are linked or skipped.",
    })
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

    step = sub.add_parser("step", parents=[common], help="Idempotent CLI onboarding loop; rerun until completed or blocked for approval/input")
    step.add_argument("--gmail-db", default=str(DEFAULT_MSGVAULT_DB), help="Path to msgvault.db for Gmail metadata onboarding")
    step.add_argument("--gmail-account", action="append", default=[], help="Gmail source account email to import from msgvault; repeat for multiple accounts")
    step.add_argument("--gmail-add-email", action="append", default=[], help="Additional Gmail address to add as an OAuth test user and authorize in msgvault")
    step.add_argument("--gmail-all", action="store_true", help="Import all Gmail source accounts discovered in msgvault")
    step.add_argument("--skip-gmail", action="store_true", help="Skip Gmail/msgvault onboarding (alias for --skip-source gmail)")
    step.add_argument("--skip-source", action="append", choices=["messages", "gmail", "linkedin_csv", "twitter"], default=[], help="Mark an onboarding source skipped; repeat as needed")
    step.add_argument("--gmail-output-dir", default=str(DEFAULT_NETWORK_IMPORT_DIR))
    step.add_argument("--linkedin-csv", default="", help="Path to LinkedIn Connections.csv export when available")
    step.add_argument("--linkedin-source-user", default="", help="Non-secret label for the LinkedIn export owner/source")
    step.add_argument("--messages-contacts-csv", default="", help="Path to an existing messages contacts CSV to link without importing")
    step.add_argument("--twitter-handle", default="", help="Twitter/X handle to record without crawling")
    step.add_argument("--operator-id", default="local")
    step.add_argument("--linkedin-ledger", default=".powerpacks/network-import/linkedin/import-run.json", help=argparse.SUPPRESS)
    step.add_argument("--import-timeout", type=int, default=90)
    step.set_defaults(func=cmd_step)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
