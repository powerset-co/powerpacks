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
DEFAULT_MESSAGES_CONTACTS_CSV = Path(".powerpacks/messages/contacts.csv")
DEFAULT_WACLI_STORE = Path(".powerpacks/messages/wacli")
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
            "what_it_needs": "iMessage/Contacts permission readiness and optional WhatsApp device link status.",
            "next_action": "Run the scoped Messages readiness check; link WhatsApp with QR if desired. No message import, WhatsApp sync, research, or upload runs during onboarding.",
            "command": "uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py step",
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


def onboarding_step_command(args: argparse.Namespace, *, placeholders: bool = False, authorized_emails: list[str] | None = None) -> str:
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
        if getattr(args, "skip_messages_whatsapp", False):
            cmd.append("--skip-messages-whatsapp")
        if getattr(args, "twitter_handle", ""):
            cmd.extend(["--twitter-handle", args.twitter_handle])
    for email in authorized_emails or []:
        cmd.extend(["--gmail-authorized-email", email])
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


def messages_open_privacy_command() -> str:
    return shell_join([
        "uv", "run", "--project", ".", "python",
        "packs/messages/primitives/extract_imessage_contacts/extract_imessage_contacts.py",
        "open-privacy-settings",
        "--target", "both",
    ])


def messages_whatsapp_auth_command() -> str:
    return shell_join([
        "uv", "run", "--project", ".", "python",
        "packs/messages/primitives/import_whatsapp_wacli/import_whatsapp_wacli.py",
        "auth",
        "--store", str(DEFAULT_WACLI_STORE),
    ])


def messages_link_status(args: argparse.Namespace) -> dict[str, Any]:
    imessage_cmd = [
        sys.executable,
        "packs/messages/primitives/extract_imessage_contacts/extract_imessage_contacts.py",
        "check",
        "--strict",
    ]
    imessage_code, imessage_payload, imessage_stderr = run_command(imessage_cmd, timeout=args.import_timeout)
    whatsapp_cmd = [
        sys.executable,
        "packs/messages/primitives/import_whatsapp_wacli/import_whatsapp_wacli.py",
        "status",
        "--store", str(DEFAULT_WACLI_STORE),
    ]
    whatsapp_code, whatsapp_payload, whatsapp_stderr = run_command(whatsapp_cmd, timeout=args.import_timeout)
    imessage_ready = imessage_code == 0 and bool((imessage_payload or {}).get("chat_db", {}).get("readable"))
    whatsapp_auth = bool((whatsapp_payload or {}).get("auth", {}).get("authenticated"))
    if whatsapp_payload and whatsapp_payload.get("status") == "ok" and not whatsapp_auth:
        whatsapp_status = "needs_auth"
    elif whatsapp_payload and whatsapp_payload.get("status"):
        whatsapp_status = str(whatsapp_payload.get("status"))
    else:
        whatsapp_status = "failed" if whatsapp_code else "unknown"
    return {
        "imessage": {
            "status": "ready" if imessage_ready else "blocked_user_action",
            "command": shell_join(["uv", "run", "--project", ".", "python", *imessage_cmd[1:]]),
            "payload": imessage_payload or {},
            "stderr": imessage_stderr,
        },
        "whatsapp": {
            "status": "linked" if whatsapp_auth else whatsapp_status,
            "authenticated": whatsapp_auth,
            "command": shell_join(["uv", "run", "--project", ".", "python", *whatsapp_cmd[1:]]),
            "auth_command": messages_whatsapp_auth_command(),
            "payload": whatsapp_payload or {},
            "stderr": whatsapp_stderr,
        },
        "privacy": {
            "reads_message_bodies": False,
            "syncs_whatsapp": False,
            "exports_contacts": False,
        },
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
        commands.append({
            "label": "rerun_onboarding",
            "command": onboarding_step_command(args, authorized_emails=emails),
            "description": "Rerun onboarding after Gmail authorization succeeds. Import-time workers own msgvault sync.",
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
    commands.append({
        "label": "rerun_onboarding",
        "command": onboarding_step_command(args, authorized_emails=emails),
        "description": "Rerun onboarding after Gmail authorization succeeds. Import-time workers own msgvault sync.",
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
    gmail_action_requested = bool(args.gmail_account or args.gmail_all or args.gmail_add_email or getattr(args, "gmail_authorized_email", []))
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
        try:
            authorized_emails = list(dict.fromkeys(normalize_email(email) for email in (getattr(args, "gmail_authorized_email", []) or [])))
        except ValueError as exc:
            emit({
                "status": "needs_input",
                "channel": "gmail",
                "message": str(exc),
                "repeat_command": onboarding_step_command(args),
            })
            return 20
        if authorized_emails:
            gmail_cfg = gmail_record.get("config", {}) if isinstance(gmail_record.get("config"), dict) else {}
            pending_accounts = list(gmail_cfg.get("pending_accounts") or [])
            unknown_authorized = sorted(set(authorized_emails) - set(pending_accounts))
            if unknown_authorized:
                emit({
                    "status": "needs_input",
                    "channel": "gmail",
                    "message": "Cannot confirm Gmail authorization for account(s) that are not pending from a prior --gmail-add-email request.",
                    "unknown_authorized_accounts": unknown_authorized,
                    "pending_accounts": pending_accounts,
                    "repeat_command": onboarding_step_command(args),
                })
                return 20
            registry = load_registry(path)
            rec = registry["accounts"]["gmail"]
            rec_cfg = rec.setdefault("config", {})
            rec_cfg["msgvault_db"] = str(Path(args.gmail_db).expanduser())
            rec_cfg["account_emails"] = list(dict.fromkeys([*(gmail_cfg.get("account_emails") or []), *authorized_emails]))
            rec_cfg["selected_accounts"] = list(dict.fromkeys([*(gmail_cfg.get("selected_accounts") or []), *authorized_emails]))
            rec_cfg["oauth_test_users"] = list(dict.fromkeys([*(gmail_cfg.get("oauth_test_users") or []), *authorized_emails]))
            rec_cfg["pending_accounts"] = [email for email in pending_accounts if email not in authorized_emails]
            rec["usernames"] = list(dict.fromkeys([*(rec.get("usernames") or []), *authorized_emails]))
            rec["linked"] = True
            rec["skipped"] = False
            rec["last_checked_at"] = now_iso()
            rec["last_success_at"] = rec["last_checked_at"]
            rec["notes"] = "Gmail account authorization confirmed and recorded for import. No Gmail network import or msgvault sync was run during onboarding."
            save_registry(registry, path)
            emit({
                "status": "progressed",
                "channel": "gmail",
                "message": "Recorded authorized Gmail account(s) in accounts.json. No Gmail network import or msgvault sync was run during onboarding.",
                "linked_accounts": authorized_emails,
                "accounts_path": args.accounts,
                "updates": updates,
                "registry": registry,
                "next_command": onboarding_step_command(args),
            })
            return 0
        if extra_emails:
            gmail_cfg = gmail_record.get("config", {}) if isinstance(gmail_record.get("config"), dict) else {}
            registry = load_registry(path)
            rec = registry["accounts"]["gmail"]
            rec_cfg = rec.setdefault("config", {})
            rec_cfg["msgvault_db"] = str(Path(args.gmail_db).expanduser())
            rec_cfg["oauth_test_users"] = list(dict.fromkeys([*(gmail_cfg.get("oauth_test_users") or []), *extra_emails]))
            rec_cfg["pending_accounts"] = list(dict.fromkeys([*(gmail_cfg.get("pending_accounts") or []), *extra_emails]))
            rec["skipped"] = False
            rec["last_checked_at"] = now_iso()
            rec["notes"] = "Gmail account authorization requested; run user-action setup commands, then confirm the account after msgvault authorization succeeds. No Gmail network import or msgvault sync was run during onboarding."
            save_registry(registry, path)
            emit({
                "status": "needs_agent_action",
                "channel": "gmail",
                "message": "Adding these Gmail accounts requires browser automation and msgvault authorization. These are user-action/linking commands only; no Gmail network import or msgvault sync is run during onboarding. The account stays pending until authorization is confirmed.",
                "action_type": "user-action/linking",
                "emails": extra_emails,
                "commands": gmail_add_email_commands(args, extra_emails),
                "repeat_command_after_authorization": onboarding_step_command(args, authorized_emails=extra_emails),
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
                "prompt": "Which Gmail address should we link first? Do not infer it from gcloud, Powerset login, or local machine state. After the user provides an email, rerun with --gmail-add-email <email>. Onboarding only authorizes and records the account; mailbox update happens later in the import phase.",
                "question": "Which Gmail address should we link first?",
                "email_source": "user_provided",
                "msgvault_db": str(db_path),
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
                "message": "No Gmail source accounts found in msgvault yet. If this is a newly authorized account, rerun onboarding after authorization is visible; the import phase runs msgvault sync after the account is linked.",
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
                "prompt": "Confirm discovered Gmail accounts to link with --gmail-account <email> or --gmail-all. Message counts are local checkpoints from prior syncs. Tell me any other Gmail addresses you want to add; use --gmail-add-email <email> so onboarding can add OAuth test users and authorize them as user-action/linking. Import workers own new msgvault sync work.",
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
        pending = [email for email in (existing.get("pending_accounts") or []) if email not in selected]
        registry = save_link_config(
            "gmail",
            path,
            config={
                "msgvault_db": str(db_path),
                "account_emails": account_emails,
                "available_accounts": available,
                "selected_accounts": selected,
                "pending_accounts": pending,
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
        if args.messages_contacts_csv:
            contacts_csv = Path(args.messages_contacts_csv).expanduser()
            if not contacts_csv.exists():
                emit({"status": "waiting", "channel": "messages", "message": f"Waiting for contacts CSV at {contacts_csv}.", "repeat_command": onboarding_step_command(args)})
                return 20
            registry = save_link_config(
                "messages",
                path,
                config={"contacts_csv": str(contacts_csv), "planned_contacts_csv": str(DEFAULT_MESSAGES_CONTACTS_CSV)},
                artifacts=[str(contacts_csv)],
                notes="Linked existing messages contacts CSV; no messages import/research run during onboarding.",
            )
            emit({"status": "progressed", "channel": "messages", "message": "Recorded existing messages contacts CSV source link. No messages import/research was run.", "registry": registry, "next_command": onboarding_step_command(args)})
            return 0

        readiness = messages_link_status(args)
        if readiness["imessage"]["status"] != "ready":
            emit({
                "status": "blocked_user_action",
                "channel": "messages",
                "message": "iMessage/Contacts permission check did not pass. Enable Full Disk Access and Contacts access for this terminal/Codex host, then rerun setup.",
                "readiness": readiness,
                "commands": [{
                    "label": "open_privacy_settings",
                    "command": messages_open_privacy_command(),
                    "description": "Open macOS privacy settings for Messages chat.db and Contacts access.",
                }],
                "repeat_command": onboarding_step_command(args),
                "skip_command": onboarding_step_command(args) + " --skip-source messages",
                "accounts_path": args.accounts,
                "updates": updates,
            })
            return 20

        whatsapp_linked = bool(readiness["whatsapp"].get("authenticated"))
        if not whatsapp_linked and not args.skip_messages_whatsapp:
            emit({
                "status": "needs_agent_action",
                "channel": "messages",
                "message": "iMessage is ready. WhatsApp is not linked yet. Run the auth-only WhatsApp link command, or rerun with --skip-messages-whatsapp to continue without WhatsApp.",
                "readiness": readiness,
                "commands": [{
                    "label": "authorize_whatsapp",
                    "command": readiness["whatsapp"]["auth_command"],
                    "description": "Authenticate WhatsApp with QR/device linking only. This does not sync or export messages.",
                }],
                "repeat_command": onboarding_step_command(args),
                "skip_whatsapp_command": onboarding_step_command(args) + " --skip-messages-whatsapp",
                "accounts_path": args.accounts,
                "updates": updates,
            })
            return 20

        config = {
            "contacts_csv": "",
            "planned_contacts_csv": str(DEFAULT_MESSAGES_CONTACTS_CSV),
            "imessage": {
                "status": "ready",
                "chat_db": (readiness["imessage"]["payload"].get("chat_db") or {}).get("path", ""),
                "addressbook_matches": readiness["imessage"]["payload"].get("addressbook_matches", 0),
            },
            "whatsapp": {
                "status": "linked" if whatsapp_linked else "skipped",
                "store": str(DEFAULT_WACLI_STORE),
                "authenticated": whatsapp_linked,
            },
        }
        registry = save_link_config(
            "messages",
            path,
            config=config,
            usernames=["imessage", *(["whatsapp"] if whatsapp_linked else [])],
            artifacts=[],
            notes="Linked Messages readiness. Onboarding checked iMessage permissions and WhatsApp auth only; no iMessage extraction, WhatsApp sync, research, or upload ran.",
        )
        emit({
            "status": "progressed",
            "channel": "messages",
            "message": "Recorded Messages readiness. No iMessage extraction, WhatsApp sync, research, or upload ran.",
            "readiness": readiness,
            "registry": registry,
            "next_command": onboarding_step_command(args),
        })
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
    step.add_argument("--gmail-account", action="append", default=[], help="Gmail source account email to link from msgvault; repeat for multiple accounts")
    step.add_argument("--gmail-add-email", action="append", default=[], help="Additional Gmail address to add as an OAuth test user and authorize in msgvault")
    step.add_argument("--gmail-authorized-email", action="append", default=[], help="Record Gmail address after returned msgvault add-account authorization succeeds; does not run sync")
    step.add_argument("--gmail-all", action="store_true", help="Link all Gmail source accounts discovered in msgvault")
    step.add_argument("--skip-gmail", action="store_true", help="Skip Gmail/msgvault onboarding (alias for --skip-source gmail)")
    step.add_argument("--skip-source", action="append", choices=["messages", "gmail", "linkedin_csv", "twitter"], default=[], help="Mark an onboarding source skipped; repeat as needed")
    step.add_argument("--gmail-output-dir", default=str(DEFAULT_NETWORK_IMPORT_DIR))
    step.add_argument("--linkedin-csv", default="", help="Path to LinkedIn Connections.csv export when available")
    step.add_argument("--linkedin-source-user", default="", help="Non-secret label for the LinkedIn export owner/source")
    step.add_argument("--messages-contacts-csv", default="", help="Path to an existing messages contacts CSV to link without importing")
    step.add_argument("--skip-messages-whatsapp", action="store_true", help="Record iMessage readiness and skip WhatsApp linking for this setup run")
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
