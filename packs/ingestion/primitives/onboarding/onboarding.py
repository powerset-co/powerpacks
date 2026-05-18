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


def msgvault_gmail_run(run_dir: Path) -> tuple[bool, list[str], str]:
    """Return whether run_dir is an active Gmail msgvault import for onboarding.

    Auto-detection is intentionally narrower than "any Gmail artifact": the
    onboarding step imports Gmail with the default `msgvault-*` run id. Test,
    smoke, and harness runs may have valid-looking manifests but should not
    mark a user's Gmail onboarding as complete.
    """

    if not run_dir.is_dir() or not run_dir.name.startswith("msgvault-"):
        return False, [], ""
    manifest = json_object(run_dir / "manifest.json")
    if (
        manifest.get("status") != "completed"
        or manifest.get("source") != "msgvault"
        or manifest.get("task") != "import_gmail_network_msgvault"
    ):
        return False, [], ""
    artifact = run_dir / "people.csv"
    artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}
    if artifacts.get("people_csv"):
        artifact = Path(str(artifacts["people_csv"]))
        if not artifact.is_absolute() and not artifact.exists():
            artifact = run_dir / artifact
    if not artifact.exists():
        return False, [], ""

    emails: list[str] = []
    for row in csv_rows(run_dir / "accounts.csv"):
        email = (row.get("account_email") or "").strip().lower()
        source = (row.get("source") or "").strip().lower()
        if email and source == "msgvault":
            emails.append(email)
    if not emails:
        return False, [], ""
    return True, list(dict.fromkeys(emails)), str(artifact)


def refresh_registry(path: Path) -> tuple[dict[str, Any], list[str]]:
    registry = load_registry(path)
    updates: list[str] = []

    # Gmail: infer linked state from local msgvault import artifacts.
    gmail_usernames: list[str] = []
    gmail_artifacts: list[str] = []
    gmail_root = Path(".powerpacks/network-import/gmail")
    if gmail_root.exists():
        for run_dir in sorted(gmail_root.iterdir()):
            ok, account_emails, artifact = msgvault_gmail_run(run_dir)
            if not ok:
                continue
            gmail_usernames.extend(account_emails)
            gmail_artifacts.append(artifact)
            updates.append(f"gmail:{artifact}")
            updates.extend(f"gmail:{email}" for email in account_emails)
    gmail_record = registry.get("accounts", {}).get("gmail", {})
    if gmail_artifacts:
        registry = set_channel_from_artifacts("gmail", path=path, linked=True, usernames=gmail_usernames, artifacts=gmail_artifacts)
    elif not gmail_record.get("skipped", False):
        registry = set_channel_from_artifacts("gmail", path=path, linked=False)

    # Messages: mark linked if local contacts artifact exists.
    if artifact_exists(".powerpacks/messages/contacts.csv"):
        update_channel("messages", path=path, success=True, artifact=".powerpacks/messages/contacts.csv")
        updates.append("messages:contacts.csv")

    # LinkedIn CSV / Twitter: infer from local import artifacts. Prefer canonical
    # people.csv, but accept the older people_harmonic_all.csv alias.
    for source, channel in [("linkedin", "linkedin_csv"), ("twitter", "twitter")]:
        seen_dirs: set[Path] = set()
        for pattern in ["*/people.csv", "*/people_harmonic_all.csv"]:
            for p in Path(f".powerpacks/network-import/{source}").glob(pattern):
                if p.parent in seen_dirs:
                    continue
                seen_dirs.add(p.parent)
                update_channel(channel, path=path, success=True, artifact=str(p))
                updates.append(f"{channel}:{p}")

    registry = load_registry(path)
    return registry, updates


def build_steps(registry: dict[str, Any]) -> list[dict[str, Any]]:
    acct = registry.get("accounts", {})
    return [
        {
            "channel": "messages",
            "linked": acct.get("messages", {}).get("linked", False),
            "skipped": acct.get("messages", {}).get("skipped", False),
            "what_it_needs": "Full Disk Access for iMessage and/or wacli for WhatsApp, then messages import.",
            "next_action": "Run the import-contacts workflow if you want message/contact metadata.",
            "command": "uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py status",
        },
        {
            "channel": "gmail",
            "linked": acct.get("gmail", {}).get("linked", False),
            "skipped": acct.get("gmail", {}).get("skipped", False),
            "what_it_needs": "Local msgvault SQLite archive with Gmail metadata, usually ~/.msgvault/msgvault.db.",
            "next_action": "Run msgvault sync, then choose one or more Gmail source accounts to import.",
            "command": "uv run --project . python packs/ingestion/primitives/gmail_network_import/gmail_network_import.py msgvault-accounts --db ~/.msgvault/msgvault.db",
        },
        {
            "channel": "linkedin_csv",
            "linked": acct.get("linkedin_csv", {}).get("linked", False),
            "skipped": acct.get("linkedin_csv", {}).get("skipped", False),
            "what_it_needs": "LinkedIn Connections.csv export from LinkedIn settings.",
            "next_action": "Export Connections.csv, then run linkedin_network_import run --csv <path> --source-user <label>.",
            "command": "uv run --project . python packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py run --csv <Connections.csv> --source-user <label>",
        },
        {
            "channel": "twitter",
            "linked": acct.get("twitter", {}).get("linked", False),
            "skipped": acct.get("twitter", {}).get("skipped", False),
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
    registry, updates = refresh_registry(path)
    emit({"status": "checked", "accounts_path": args.accounts, "updates": updates, "registry": registry, "steps": build_steps(registry)})
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    registry = load_registry(Path(args.accounts))
    steps = build_steps(registry)
    todo = [step for step in steps if args.all or (not step["linked"] and not step.get("skipped"))]
    emit({"status": "plan", "accounts_path": args.accounts, "todo": todo, "already_linked": [s for s in steps if s["linked"]], "skipped": [s for s in steps if s.get("skipped")]})
    return 0


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def shell_join(parts: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def onboarding_step_command(args: argparse.Namespace, *, placeholders: bool = False) -> str:
    cmd = [
        "uv", "run", "--project", ".", "python",
        "packs/ingestion/primitives/onboarding/onboarding.py", "step",
        "--accounts", args.accounts,
        "--gmail-db", args.gmail_db,
        "--gmail-output-dir", args.gmail_output_dir,
        "--linkedin-ledger", args.linkedin_ledger,
    ]
    if placeholders:
        cmd.extend(["--linkedin-csv", "<Connections.csv>", "--linkedin-source-user", "<label>"])
    else:
        if args.linkedin_csv:
            cmd.extend(["--linkedin-csv", args.linkedin_csv])
        if args.linkedin_source_user:
            cmd.extend(["--linkedin-source-user", args.linkedin_source_user])
    return shell_join(cmd)


def gmail_import_py() -> str:
    return "packs/ingestion/primitives/gmail_network_import/gmail_network_import.py"


def msgvault_setup_py() -> str:
    return "packs/ingestion/primitives/msgvault_setup/msgvault_setup.py"


def linkedin_import_py() -> str:
    return "packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py"


def discover_msgvault_accounts(args: argparse.Namespace) -> tuple[int, dict[str, Any] | None, str]:
    cmd = [sys.executable, gmail_import_py(), "msgvault-accounts", "--db", args.gmail_db]
    return run_command(cmd, timeout=args.import_timeout)


def run_gmail_msgvault_import(args: argparse.Namespace, account_email: str) -> tuple[int, dict[str, Any] | None, str]:
    cmd = [
        sys.executable,
        gmail_import_py(),
        "msgvault",
        "--db", args.gmail_db,
        "--account-email", account_email,
        "--operator-id", args.operator_id,
        "--output-dir", args.gmail_output_dir,
    ]
    return run_command(cmd, timeout=args.import_timeout)


def run_gmail_accounts_command(args: argparse.Namespace) -> str:
    return shell_join(["uv", "run", "--project", ".", "python", gmail_import_py(), "msgvault-accounts", "--db", args.gmail_db])


def run_gmail_import_command(args: argparse.Namespace, account_email: str) -> str:
    return shell_join([
        "uv", "run", "--project", ".", "python", gmail_import_py(), "msgvault",
        "--db", args.gmail_db,
        "--account-email", account_email,
        "--operator-id", args.operator_id,
        "--output-dir", args.gmail_output_dir,
    ])


def normalize_email(value: str) -> str:
    email = (value or "").strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise ValueError(f"invalid email: {value}")
    return email


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


def gmail_add_email_commands(args: argparse.Namespace, emails: list[str]) -> list[dict[str, str]]:
    add_users = [
        "uv", "run", "--project", ".", "python", msgvault_setup_py(), "add-test-users",
        *emails,
    ]
    commands = [{
        "label": "add_oauth_test_users",
        "command": shell_join(add_users),
        "description": "Add the Gmail addresses as Google OAuth test users with browser automation.",
    }]
    for email in emails:
        commands.append({
            "label": f"authorize_{email}",
            "command": shell_join(["uv", "run", "--project", ".", "python", msgvault_setup_py(), "add-account", "--email", email]),
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


def run_linkedin_import(args: argparse.Namespace, mode: str) -> tuple[int, dict[str, Any] | None, str]:
    cmd = [sys.executable, linkedin_import_py(), mode, "--ledger", args.linkedin_ledger]
    if mode == "run":
        cmd = [
            sys.executable,
            linkedin_import_py(),
            "run",
            "--csv", str(Path(args.linkedin_csv).expanduser()),
            "--source-user", args.linkedin_source_user,
            "--operator-id", args.operator_id,
            "--ledger", args.linkedin_ledger,
        ]
    return run_command(cmd, timeout=args.import_timeout)


def import_continue_command(args: argparse.Namespace) -> str:
    return shell_join([
        "uv", "run", "--project", ".", "python", linkedin_import_py(),
        "approve", "--ledger", args.linkedin_ledger,
    ]) + " && " + shell_join([
        "uv", "run", "--project", ".", "python", linkedin_import_py(),
        "continue", "--ledger", args.linkedin_ledger,
    ])


def cmd_step(args: argparse.Namespace) -> int:
    """Idempotent one-step onboarding loop for CLI/harness use.

    The command is safe to run repeatedly. It refreshes accounts.json from local
    artifacts first, then returns the next missing action. LinkedIn CSV is
    handled as the primary interactive handoff: provide a Connections.csv once,
    approve external enrichment separately if blocked, then keep rerunning this
    command until the final people.csv artifact is detected.
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
            emit({
                "status": "needs_agent_action",
                "channel": "gmail",
                "message": "Adding these Gmail accounts requires browser automation and msgvault authorization. Codex should run the commands in order, start Gmail sync in the background, and only ask the user to complete browser login or consent if Google asks. Large mailboxes can take a few hours; onboarding can import from the latest local checkpoint and rerun later.",
                "emails": extra_emails,
                "commands": gmail_add_email_commands(args, extra_emails),
                "repeat_command_after_sync": onboarding_step_command(args),
                "accounts_path": args.accounts,
                "updates": updates,
            })
            return 20

        db_path = Path(args.gmail_db).expanduser()
        if not db_path.exists():
            emit({
                "status": "needs_input",
                "channel": "gmail",
                "prompt": "Link a Gmail account for local msgvault sync, then rerun with --gmail-db <path>. Tell me any other Gmail addresses you want to add; use --gmail-add-email <email>. Large mailboxes can take a few hours to fully sync, so Codex should start the sync while you are here. To skip Gmail, rerun with --skip-gmail.",
                "msgvault_db": str(db_path),
                "example_sync": "msgvault sync-full <email>",
                "next_command": onboarding_step_command(args),
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
                "prompt": "Confirm discovered Gmail accounts to import with --gmail-account <email> or --gmail-all. Message counts are local sync checkpoints; large mailboxes can keep syncing in the background for a few hours. Tell me any other Gmail addresses you want to add; use --gmail-add-email <email> so onboarding can add OAuth test users, authorize them, and start background sync.",
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

        imported: list[dict[str, Any]] = []
        for email in selected:
            code, payload, stderr = run_gmail_msgvault_import(args, email)
            if code != 0 or not payload:
                emit({
                    "status": "failed",
                    "channel": "gmail",
                    "account_email": email,
                    "stderr": stderr,
                    "import": payload,
                    "repeat_command": onboarding_step_command(args),
                })
                return code or 1
            artifacts = payload.get("artifacts", {}) if isinstance(payload, dict) else {}
            artifact = artifacts.get("people_csv") or artifacts.get("manifest_json") or payload.get("run_dir", "")
            registry = update_channel("gmail", path=path, username=email, artifact=artifact, success=True, notes="Imported from local msgvault metadata")
            imported.append({
                "account_email": email,
                "run_dir": payload.get("run_dir"),
                "counts": payload.get("counts"),
                "artifact": artifact,
                "command": run_gmail_import_command(args, email),
            })
        emit({
            "status": "progressed",
            "channel": "gmail",
            "message": "Imported selected Gmail msgvault account metadata and updated accounts.json.",
            "imported_accounts": imported,
            "accounts_path": args.accounts,
            "registry": registry,
            "next_command": onboarding_step_command(args),
        })
        return 0

    if not accounts.get("linkedin_csv", {}).get("linked", False) and not accounts.get("linkedin_csv", {}).get("skipped", False):
        ledger = read_json(Path(args.linkedin_ledger))
        if ledger and ledger.get("status") not in {"completed", "failed"}:
            code, payload, stderr = run_linkedin_import(args, "continue")
            registry, more_updates = refresh_registry(path)
            updates.extend(more_updates)
            if registry.get("accounts", {}).get("linkedin_csv", {}).get("linked", False):
                emit({
                    "status": "progressed",
                    "channel": "linkedin_csv",
                    "message": "LinkedIn CSV import artifact detected and accounts.json is linked.",
                    "accounts_path": args.accounts,
                    "updates": updates,
                    "registry": registry,
                    "next_command": onboarding_step_command(args),
                })
                return 0
            if payload and payload.get("status") == "blocked_approval":
                emit({
                    "status": "blocked_approval",
                    "channel": "linkedin_csv",
                    "message": payload.get("message"),
                    "approval_type": payload.get("approval_type"),
                    "approval_command": payload.get("continue_command") or import_continue_command(args),
                    "repeat_command_after_approval": onboarding_step_command(args),
                    "import": payload,
                })
                return 20
            if code != 0:
                emit({"status": "failed", "channel": "linkedin_csv", "import": payload, "stderr": stderr, "repeat_command": onboarding_step_command(args)})
                return code
            emit({"status": "waiting", "channel": "linkedin_csv", "import": payload, "updates": updates, "repeat_command": onboarding_step_command(args)})
            return 20

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

        code, payload, stderr = run_linkedin_import(args, "run")
        registry, more_updates = refresh_registry(path)
        updates.extend(more_updates)
        if payload and payload.get("status") == "blocked_approval":
            emit({
                "status": "blocked_approval",
                "channel": "linkedin_csv",
                "message": payload.get("message"),
                "approval_type": payload.get("approval_type"),
                "approval_command": payload.get("continue_command") or import_continue_command(args),
                "repeat_command_after_approval": onboarding_step_command(args),
                "import": payload,
                "updates": updates,
            })
            return 20
        if code != 0:
            emit({"status": "failed", "channel": "linkedin_csv", "import": payload, "stderr": stderr, "repeat_command": onboarding_step_command(args)})
            return code
        emit({"status": "progressed", "channel": "linkedin_csv", "import": payload, "updates": updates, "next_command": onboarding_step_command(args)})
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
    step.add_argument("--operator-id", default="local")
    step.add_argument("--linkedin-ledger", default=".powerpacks/network-import/linkedin/import-run.json")
    step.add_argument("--import-timeout", type=int, default=90)
    step.set_defaults(func=cmd_step)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
