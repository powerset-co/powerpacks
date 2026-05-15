#!/usr/bin/env python3
"""Guided ingestion onboarding.

Shows link/export status for each supported network source and gives the next
command or user action. Persists non-secret account state to
`.powerpacks/ingestion/accounts.json`.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import webbrowser
import zipfile
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


def default_downloads_dir() -> Path:
    return Path.home() / "Downloads"


def linkedin_drop_dir(accounts_path: Path) -> Path:
    return accounts_path.expanduser().parent / "linkedin"


def linkedin_drop_file(accounts_path: Path) -> Path:
    return linkedin_drop_dir(accounts_path) / LINKEDIN_CONNECTIONS_FILENAME


def open_directory(path: Path) -> dict[str, Any]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(path)], check=True, capture_output=True, text=True)
    except Exception as exc:
        return {"opened": False, "path": str(path), "error": str(exc)}
    return {"opened": True, "path": str(path)}


def candidate_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "kind": candidate["kind"],
        "path": str(candidate["path"]),
        "mtime": candidate.get("mtime", 0.0),
    }
    if candidate.get("member"):
        payload["member"] = candidate["member"]
    return payload


def add_linkedin_zip_candidates(path: Path, candidates: list[dict[str, Any]], errors: list[str]) -> None:
    try:
        mtime = path.stat().st_mtime
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                if Path(member.filename).name == LINKEDIN_CONNECTIONS_FILENAME:
                    candidates.append({"kind": "zip", "path": path, "member": member.filename, "mtime": mtime})
    except (OSError, zipfile.BadZipFile) as exc:
        errors.append(f"{path}: {exc}")


def find_linkedin_connections_candidates(downloads_dir: Path) -> dict[str, Any]:
    downloads_dir = downloads_dir.expanduser()
    candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    if not downloads_dir.exists():
        return {"status": "not_found", "downloads": str(downloads_dir), "candidates": [], "errors": ["downloads_missing"]}

    def onerror(exc: OSError) -> None:
        errors.append(str(exc))

    for root, dirs, files in os.walk(downloads_dir, onerror=onerror):
        dirs[:] = [name for name in dirs if not name.startswith(".")]
        root_path = Path(root)
        for name in files:
            path = root_path / name
            if name == LINKEDIN_CONNECTIONS_FILENAME:
                try:
                    candidates.append({"kind": "file", "path": path, "mtime": path.stat().st_mtime})
                except OSError as exc:
                    errors.append(f"{path}: {exc}")
            elif name.lower().endswith(".zip") and "linkedin" in name.lower():
                add_linkedin_zip_candidates(path, candidates, errors)

    candidates.sort(key=lambda item: item.get("mtime", 0.0), reverse=True)
    return {
        "status": "found" if candidates else "not_found",
        "downloads": str(downloads_dir),
        "candidates": [candidate_payload(candidate) for candidate in candidates[:10]],
        "errors": errors[:10],
    }


def copy_linkedin_candidate(candidate: dict[str, Any], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if candidate["kind"] == "file":
        shutil.copy2(candidate["path"], destination)
        return
    with zipfile.ZipFile(candidate["path"]) as archive:
        with archive.open(candidate["member"]) as src, destination.open("wb") as dst:
            shutil.copyfileobj(src, dst)


def scan_and_copy_linkedin_connections(downloads_dir: Path, accounts_path: Path) -> dict[str, Any]:
    downloads_dir = downloads_dir.expanduser()
    result = find_linkedin_connections_candidates(downloads_dir)
    if result["status"] != "found":
        result["message"] = "No LinkedIn Connections.csv found in Downloads."
        result["drop_path"] = str(linkedin_drop_file(accounts_path))
        return result

    selected = result["candidates"][0]
    candidate = {"kind": selected["kind"], "path": Path(selected["path"]), "mtime": selected["mtime"]}
    if selected.get("member"):
        candidate["member"] = selected["member"]
    destination = linkedin_drop_file(accounts_path)
    try:
        copy_linkedin_candidate(candidate, destination)
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        result["status"] = "failed"
        result["message"] = f"Found a LinkedIn export but could not copy {LINKEDIN_CONNECTIONS_FILENAME}: {exc}"
        result["drop_path"] = str(destination)
        return result

    result["status"] = "copied"
    result["selected"] = selected
    result["copied_to"] = str(destination)
    result["message"] = f"Copied {LINKEDIN_CONNECTIONS_FILENAME} from Downloads."
    return result


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
            "what_it_needs": "LinkedIn larger data archive from LinkedIn settings; Connections.csv is inside the archive.",
            "next_action": "Open LinkedIn data archive settings, choose the larger archive, request it, then import Connections.csv after LinkedIn emails the archive.",
            "command": "uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py open-linkedin-archive",
            "url": LINKEDIN_DATA_ARCHIVE_URL,
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



DEFAULT_ONBOARDING_LEDGER = Path(".powerpacks/ingestion/onboarding-run.json")
LINKEDIN_DATA_ARCHIVE_URL = "https://www.linkedin.com/mypreferences/d/download-my-data"
LINKEDIN_LARGER_ARCHIVE_LABEL = (
    "Download larger data archive, including connections, verifications, contacts, "
    "account history, and information LinkedIn infers based on your profile and activity."
)
LINKEDIN_CONNECTIONS_FILENAME = "Connections.csv"
LINKEDIN_CSV_CONTINUE_ACTIONS = {
    "scan-linkedin-downloads",
    "open-downloads",
    "open-linkedin-drop-folder",
    "check-linkedin-drop-folder",
}
LINKEDIN_SCAN_REPLIES = {
    "scan",
    "scan downloads",
    "scan ~/downloads",
    "find",
    "find downloads",
    "find ~/downloads",
}
LINKEDIN_OPEN_DOWNLOADS_REPLIES = {"open downloads", "open ~/downloads", "downloads"}
LINKEDIN_OPEN_DROP_REPLIES = {"open folder", "open drop folder", "open repo folder", "drop folder", "folder"}
LINKEDIN_DROP_DONE_REPLIES = {"done", "ready", "ok"}
SIMPLE_CONTINUE_ACTIONS = {"yes", "no", "skip", "done"}
ONBOARDING_FLOW = ["messages", "gmail", "linkedin_csv", "linkedin_mcp", "twitter", "merge", "enrich"]
YES = {"y", "yes", "true", "1", "ok", "sure"}
NO = {"n", "no", "false", "0", "skip", "s"}


def now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def normalize_reply(value: str) -> str:
    return (value or "").strip()


def load_run(path: Path) -> dict[str, Any]:
    state = read_json(path, {}) or {}
    state.setdefault("version", 1)
    state.setdefault("created_at", now_iso())
    state.setdefault("updated_at", now_iso())
    state.setdefault("index", 0)
    state.setdefault("phase", "ask")
    state.setdefault("answers", {})
    state.setdefault("skipped", [])
    state.setdefault("context", {})
    return state


def save_run(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    write_json(path, state)


def current_step(state: dict[str, Any]) -> str | None:
    idx = int(state.get("index") or 0)
    if idx >= len(ONBOARDING_FLOW):
        return None
    return ONBOARDING_FLOW[idx]


def advance(state: dict[str, Any]) -> None:
    state["index"] = int(state.get("index") or 0) + 1
    state["phase"] = "ask"


def prompt_for(step: str, registry: dict[str, Any]) -> dict[str, Any]:
    acct = registry.get("accounts", {})
    linked = bool(acct.get(step, {}).get("linked", False)) if step in acct else False
    if step == "messages":
        return {
            "status": "needs_user_input",
            "step": step,
            "linked": linked,
            "message": "Import local message contacts? This reads contact metadata only, not message bodies.",
            "choices": ["yes", "no", "skip"],
        }
    if step == "gmail":
        return {
            "status": "needs_user_input",
            "step": step,
            "linked": linked,
            "message": "Connect Gmail? OAuth opens in your browser; Powerpacks does not scan Gmail locally.",
            "choices": ["yes", "no", "skip"],
        }
    if step == "linkedin_csv":
        return {
            "status": "needs_user_input",
            "step": step,
            "linked": linked,
            "message": "Import LinkedIn connections? Reply yes to open LinkedIn's data archive page, then paste Connections.csv after the archive is ready.",
            "choices": ["yes", "no", "skip"],
        }
    if step == "linkedin_mcp":
        return {
            "status": "needs_user_input",
            "step": step,
            "linked": linked,
            "message": "Set up LinkedIn MCP browser access? This is optional and connection export is still WIP upstream.",
            "choices": ["yes", "no", "skip"],
        }
    if step == "twitter":
        return {
            "status": "needs_user_input",
            "step": step,
            "linked": linked,
            "message": "Configure Twitter/X follower import for an operator handle? This uses RapidAPI only after approval.",
            "choices": ["yes", "no", "skip"],
        }
    if step == "merge":
        return {
            "status": "needs_user_input",
            "step": step,
            "linked": False,
            "message": "Merge imported sources locally now? This dedupes by LinkedIn and flags similar names for review.",
            "choices": ["yes", "no", "skip"],
        }
    if step == "enrich":
        return {
            "status": "needs_user_input",
            "step": step,
            "linked": False,
            "message": "Prepare profile enrichment now? Provider calls pause for approval.",
            "choices": ["yes", "no", "skip"],
        }
    raise ValueError(f"unknown step: {step}")


def complete_payload(state: dict[str, Any], registry: dict[str, Any], ledger_path: Path) -> dict[str, Any]:
    return {
        "status": "completed",
        "message": "Onboarding flow complete.",
        "ledger": str(ledger_path),
        "accounts": registry,
        "answers": state.get("answers", {}),
        "skipped": state.get("skipped", []),
    }


def next_prompt(state: dict[str, Any], accounts_path: Path, ledger_path: Path) -> dict[str, Any]:
    registry = load_registry(accounts_path)
    step = current_step(state)
    if not step:
        state["status"] = "completed"
        save_run(ledger_path, state)
        return complete_payload(state, registry, ledger_path)
    payload = prompt_for(step, registry)
    payload.update({"ledger": str(ledger_path), "accounts_path": str(accounts_path), "phase": state.get("phase", "ask")})
    save_run(ledger_path, state)
    return payload


def action_for_yes(step: str, state: dict[str, Any], accounts_path: Path, ledger_path: Path) -> dict[str, Any]:
    state["answers"][step] = "yes"
    if step == "messages":
        if artifact_exists(".powerpacks/messages/contacts.csv"):
            update_channel("messages", path=accounts_path, success=True, artifact=".powerpacks/messages/contacts.csv")
            advance(state)
            payload = next_prompt(state, accounts_path, ledger_path)
            payload["completed_action"] = {
                "step": "messages",
                "message": "Messages contacts already imported.",
                "artifact": ".powerpacks/messages/contacts.csv",
            }
            return payload
        state["phase"] = "awaiting_done"
        return {
            "status": "needs_agent_action",
            "step": step,
            "message": "Starting messages import workflow. It reads contact metadata only, not message bodies.",
            "command": "uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py run",
            "continue_command": f"uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py continue --ledger {ledger_path} --input done",
        }
    if step == "gmail":
        state["phase"] = "awaiting_done"
        return {
            "status": "needs_user_action",
            "step": step,
            "message": "Finish Gmail OAuth in the browser; this terminal will detect the linked account.",
            "url": "https://search.powerset.dev/gmail/connect",
            "command": "uv run --project . python packs/ingestion/primitives/gmail_network_import/gmail_network_import.py connect --timeout-seconds 600",
            "continue_command": f"uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py continue --ledger {ledger_path} --input done",
        }
    if step == "linkedin_csv":
        state["phase"] = "awaiting_csv_path"
        drop_path = linkedin_drop_file(accounts_path)
        return {
            "status": "needs_user_action",
            "step": step,
            "message": "LinkedIn will open in your browser. Select the larger data archive option, click Request archive, then reply scan after the archive downloads, or copy Connections.csv into the repo drop folder and reply done.",
            "url": LINKEDIN_DATA_ARCHIVE_URL,
            "command": "uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py open-linkedin-archive",
            "archive_option": LINKEDIN_LARGER_ARCHIVE_LABEL,
            "drop_path": str(drop_path),
            "example": "~/Downloads/Connections.csv",
            "harness_actions": linkedin_harness_actions(accounts_path, ledger_path),
        }
    if step == "linkedin_mcp":
        state["phase"] = "awaiting_linkedin_mcp_username"
        return {
            "status": "needs_user_action",
            "step": step,
            "message": "Install/login to the LinkedIn MCP, then reply with your LinkedIn profile URL or username.",
            "command": "uv run --project . python packs/ingestion/primitives/linkedin_mcp_import/linkedin_mcp_import.py instructions",
            "login_command": "uvx linkedin-scraper-mcp@latest --login",
        }
    if step == "twitter":
        state["phase"] = "awaiting_twitter_handle"
        return {"status": "needs_user_input", "step": step, "message": "Reply with the Twitter/X operator handle to import."}
    if step == "merge":
        state["phase"] = "awaiting_done"
        return {
            "status": "needs_agent_action",
            "step": step,
            "message": "Starting local merge.",
            "command": "uv run --project . python packs/ingestion/primitives/merge_network_sources/merge_network_sources.py run",
            "continue_command": f"uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py continue --ledger {ledger_path} --input done",
        }
    if step == "enrich":
        state["phase"] = "awaiting_enrich_input"
        return {
            "status": "needs_user_input",
            "step": step,
            "message": "Paste the people CSV to enrich, usually .powerpacks/network-import/merged/people_harmonic_all.merged.csv",
        }
    raise ValueError(f"unknown step: {step}")


def continue_command_base(accounts_path: Path, ledger_path: Path) -> str:
    return (
        "uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py continue "
        f"--accounts {shlex.quote(str(accounts_path))} --ledger {shlex.quote(str(ledger_path))}"
    )


def linkedin_harness_actions(accounts_path: Path, ledger_path: Path) -> dict[str, str]:
    base = continue_command_base(accounts_path, ledger_path)
    return {
        "scan_downloads": f"{base} --action scan-linkedin-downloads",
        "open_downloads": f"{base} --action open-downloads",
        "open_drop_folder": f"{base} --action open-linkedin-drop-folder",
        "check_drop_folder": f"{base} --action check-linkedin-drop-folder",
        "set_csv": f"{base} --csv PATH_TO_CONNECTIONS_CSV",
    }


def complete_linkedin_csv_path(
    state: dict[str, Any],
    accounts_path: Path,
    ledger_path: Path,
    csv_path: Path,
    message: str = "Recorded CSV path. Run this import command when ready.",
) -> dict[str, Any]:
    state["context"]["linkedin_csv_path"] = str(csv_path)
    update_channel(
        "linkedin_csv",
        path=accounts_path,
        username=csv_path.stem,
        artifact=str(csv_path),
        linked=True,
        notes="CSV path recorded; run import command to ingest.",
    )
    advance(state)
    payload = next_prompt(state, accounts_path, ledger_path)
    payload["completed_action"] = {
        "step": "linkedin_csv",
        "message": message,
        "command": (
            "uv run --project . python "
            "packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py "
            f"run --csv {shlex.quote(str(csv_path))} --source-user {shlex.quote(csv_path.stem)}"
        ),
        "artifact": str(csv_path),
    }
    return payload


def handle_continue(
    state: dict[str, Any],
    user_input: str,
    accounts_path: Path,
    ledger_path: Path,
    action: str | None = None,
    downloads_dir: Path | None = None,
) -> dict[str, Any]:
    step = current_step(state)
    if not step:
        return complete_payload(state, load_registry(accounts_path), ledger_path)
    phase = state.get("phase", "ask")
    reply = normalize_reply(user_input)
    low = reply.lower()
    downloads_dir = downloads_dir or default_downloads_dir()

    if action in LINKEDIN_CSV_CONTINUE_ACTIONS and phase != "awaiting_csv_path":
        return {
            "status": "failed",
            "error": f"--action {action} is only valid while waiting for the LinkedIn CSV.",
            "step": step,
            "phase": phase,
            "ledger": str(ledger_path),
        }

    if phase == "ask":
        if low in NO:
            state["answers"][step] = "skip"
            state.setdefault("skipped", []).append(step)
            advance(state)
            return next_prompt(state, accounts_path, ledger_path)
        if low not in YES:
            payload = prompt_for(step, load_registry(accounts_path))
            payload.update({"status": "needs_user_input", "error": "Please reply yes, no, or skip.", "ledger": str(ledger_path)})
            return payload
        payload = action_for_yes(step, state, accounts_path, ledger_path)
        save_run(ledger_path, state)
        payload["ledger"] = str(ledger_path)
        return payload

    if phase == "awaiting_done":
        if low not in {"done", "yes", "y", "ok"}:
            return {"status": "needs_user_input", "step": step, "message": "Reply done when finished, or skip.", "ledger": str(ledger_path)}
        if step == "messages" and artifact_exists(".powerpacks/messages/contacts.csv"):
            update_channel("messages", path=accounts_path, success=True, artifact=".powerpacks/messages/contacts.csv")
        elif step == "gmail":
            gmail = run_json(["uv", "run", "--project", ".", "python", "packs/ingestion/primitives/gmail_network_import/gmail_network_import.py", "accounts"])
            if gmail and gmail.get("status") == "ok":
                for account in (gmail.get("accounts") or gmail.get("connected_accounts") or []):
                    email = account.get("email") or account.get("account_email") if isinstance(account, dict) else ""
                    if email:
                        update_channel("gmail", path=accounts_path, username=email, success=True, artifact="gmail-stats")
            else:
                update_channel("gmail", path=accounts_path, linked=True, notes="User reported Gmail OAuth completed; account check did not confirm.")
        elif step == "merge":
            update_channel("messages", path=accounts_path, artifact=".powerpacks/network-import/merged/people_harmonic_all.merged.csv", notes="Merge step run/requested.")
        advance(state)
        return next_prompt(state, accounts_path, ledger_path)

    if phase == "awaiting_csv_path":
        csv_action = action
        if not csv_action:
            if low in LINKEDIN_SCAN_REPLIES:
                csv_action = "scan-linkedin-downloads"
            elif low in LINKEDIN_OPEN_DOWNLOADS_REPLIES:
                csv_action = "open-downloads"
            elif low in LINKEDIN_OPEN_DROP_REPLIES:
                csv_action = "open-linkedin-drop-folder"
            elif low in LINKEDIN_DROP_DONE_REPLIES:
                csv_action = "check-linkedin-drop-folder"

        if csv_action == "scan-linkedin-downloads":
            scan = scan_and_copy_linkedin_connections(downloads_dir, accounts_path)
            if scan["status"] == "copied":
                payload = complete_linkedin_csv_path(
                    state,
                    accounts_path,
                    ledger_path,
                    Path(scan["copied_to"]),
                    message="Found and copied LinkedIn Connections.csv from Downloads. Run this import command when ready.",
                )
                payload["scan"] = scan
                return payload
            return {
                "status": "needs_user_input",
                "step": step,
                "message": "I could not find Connections.csv in Downloads. Paste the CSV path, reply open downloads, or copy it into the repo drop folder and reply done.",
                "ledger": str(ledger_path),
                "scan": scan,
                "drop_path": str(linkedin_drop_file(accounts_path)),
                "harness_actions": linkedin_harness_actions(accounts_path, ledger_path),
            }
        if csv_action == "open-downloads":
            opened = open_directory(downloads_dir)
            return {
                "status": "needs_user_input",
                "step": step,
                "message": "Opened Downloads. Paste the Connections.csv path, reply scan, or copy it into the repo drop folder and reply done.",
                "ledger": str(ledger_path),
                "opened": opened,
                "drop_path": str(linkedin_drop_file(accounts_path)),
                "harness_actions": linkedin_harness_actions(accounts_path, ledger_path),
            }
        if csv_action == "open-linkedin-drop-folder":
            opened = open_directory(linkedin_drop_dir(accounts_path))
            return {
                "status": "needs_user_input",
                "step": step,
                "message": "Opened the repo drop folder. Copy Connections.csv there, then reply done.",
                "ledger": str(ledger_path),
                "opened": opened,
                "drop_path": str(linkedin_drop_file(accounts_path)),
                "harness_actions": linkedin_harness_actions(accounts_path, ledger_path),
            }
        if csv_action == "check-linkedin-drop-folder":
            csv_path = linkedin_drop_file(accounts_path)
            if csv_path.exists():
                return complete_linkedin_csv_path(state, accounts_path, ledger_path, csv_path)
            return {
                "status": "needs_user_input",
                "step": step,
                "message": "I do not see Connections.csv in the repo drop folder yet. Paste a CSV path, reply scan, or copy it there and reply done.",
                "ledger": str(ledger_path),
                "drop_path": str(csv_path),
                "harness_actions": linkedin_harness_actions(accounts_path, ledger_path),
            }
        csv_path = Path(reply).expanduser()
        if not csv_path.exists():
            return {
                "status": "needs_user_input",
                "step": step,
                "message": "That file does not exist. Paste a valid Connections.csv path, reply scan, reply open downloads, or skip.",
                "ledger": str(ledger_path),
                "drop_path": str(linkedin_drop_file(accounts_path)),
                "harness_actions": linkedin_harness_actions(accounts_path, ledger_path),
            }
        return complete_linkedin_csv_path(state, accounts_path, ledger_path, csv_path)

    if phase == "awaiting_linkedin_mcp_username":
        if not reply:
            return {"status": "needs_user_input", "step": step, "message": "Reply with your LinkedIn profile URL or username, or skip.", "ledger": str(ledger_path)}
        update_channel("linkedin_mcp", path=accounts_path, username=reply, success=True, notes="User reported LinkedIn MCP login/setup completed.")
        advance(state)
        return next_prompt(state, accounts_path, ledger_path)

    if phase == "awaiting_twitter_handle":
        handle = reply.lstrip("@")
        if not handle:
            return {"status": "needs_user_input", "step": step, "message": "Reply with a Twitter/X handle, or skip.", "ledger": str(ledger_path)}
        update_channel("twitter", path=accounts_path, username=handle, linked=True, notes="Handle recorded; crawl requires RapidAPI approval.")
        advance(state)
        payload = next_prompt(state, accounts_path, ledger_path)
        payload["completed_action"] = {
            "step": "twitter",
            "message": "Recorded Twitter/X handle. Run this import command when ready.",
            "command": f"uv run --project . python packs/ingestion/primitives/twitter_network_import/twitter_network_import.py run --handle {handle}",
        }
        return payload

    if phase == "awaiting_enrich_input":
        csv_path = Path(reply).expanduser()
        if not csv_path.exists():
            return {"status": "needs_user_input", "step": step, "message": "That file does not exist. Paste a valid people CSV path or skip.", "ledger": str(ledger_path)}
        advance(state)
        payload = next_prompt(state, accounts_path, ledger_path)
        payload["completed_action"] = {
            "step": "enrich",
            "message": "Run this enrichment command when ready. Provider calls pause for approval.",
            "command": f"uv run --project . python packs/ingestion/primitives/enrich_people/enrich_people.py run --input {csv_path}",
        }
        return payload

    return {"status": "failed", "error": f"unknown phase {phase}", "ledger": str(ledger_path)}


def cmd_run(args: argparse.Namespace) -> int:
    ledger_path = Path(args.ledger)
    if ledger_path.exists() and not args.force:
        state = load_run(ledger_path)
    else:
        state = {"version": 1, "created_at": now_iso(), "updated_at": now_iso(), "index": 0, "phase": "ask", "answers": {}, "skipped": [], "context": {}}
    save_run(ledger_path, state)
    emit(next_prompt(state, Path(args.accounts), ledger_path))
    return 0


def cmd_continue(args: argparse.Namespace) -> int:
    provided = [args.input is not None, args.action is not None, args.csv is not None]
    if sum(provided) != 1:
        emit({
            "status": "failed",
            "error": "Provide exactly one of --input, --action, or --csv.",
            "allowed_actions": sorted(SIMPLE_CONTINUE_ACTIONS | LINKEDIN_CSV_CONTINUE_ACTIONS),
        })
        return 2
    user_input = args.input or ""
    action = args.action
    if args.csv is not None:
        user_input = str(args.csv)
        action = None
    elif action in SIMPLE_CONTINUE_ACTIONS:
        user_input = action
        action = None

    ledger_path = Path(args.ledger)
    state = load_run(ledger_path)
    payload = handle_continue(
        state,
        user_input,
        Path(args.accounts),
        ledger_path,
        action=action,
        downloads_dir=Path(args.downloads),
    )
    save_run(ledger_path, state)
    emit(payload)
    return 0 if payload.get("status") != "failed" else 1


def cmd_skip(args: argparse.Namespace) -> int:
    ledger_path = Path(args.ledger)
    state = load_run(ledger_path)
    step = current_step(state)
    if step:
        state["answers"][step] = "skip"
        state.setdefault("skipped", []).append(step)
        advance(state)
    emit(next_prompt(state, Path(args.accounts), ledger_path))
    return 0


def cmd_run_status(args: argparse.Namespace) -> int:
    ledger_path = Path(args.ledger)
    state = load_run(ledger_path)
    payload = next_prompt(state, Path(args.accounts), ledger_path)
    payload["run_state"] = state
    emit(payload)
    return 0


def cmd_open_linkedin_archive(args: argparse.Namespace) -> int:
    opened = webbrowser.open(LINKEDIN_DATA_ARCHIVE_URL)
    emit({
        "status": "ok" if opened else "failed",
        "opened": opened,
        "url": LINKEDIN_DATA_ARCHIVE_URL,
        "message": "Opened LinkedIn data archive settings." if opened else "Could not open LinkedIn data archive settings.",
        "archive_option": LINKEDIN_LARGER_ARCHIVE_LABEL,
    })
    return 0 if opened else 1


def cmd_open_downloads(args: argparse.Namespace) -> int:
    payload = open_directory(Path(args.downloads))
    payload["status"] = "ok" if payload["opened"] else "failed"
    payload["message"] = "Opened Downloads." if payload["opened"] else "Could not open Downloads."
    emit(payload)
    return 0 if payload["opened"] else 1


def cmd_open_linkedin_drop_folder(args: argparse.Namespace) -> int:
    path = linkedin_drop_dir(Path(args.accounts))
    payload = open_directory(path)
    payload["status"] = "ok" if payload["opened"] else "failed"
    payload["message"] = "Opened LinkedIn CSV drop folder." if payload["opened"] else "Could not open LinkedIn CSV drop folder."
    payload["drop_path"] = str(linkedin_drop_file(Path(args.accounts)))
    emit(payload)
    return 0 if payload["opened"] else 1


def cmd_find_linkedin_connections(args: argparse.Namespace) -> int:
    downloads = Path(args.downloads)
    if args.copy_to_repo:
        payload = scan_and_copy_linkedin_connections(downloads, Path(args.accounts))
    else:
        payload = find_linkedin_connections_candidates(downloads)
    payload["drop_path"] = str(linkedin_drop_file(Path(args.accounts)))
    emit(payload)
    return 0 if payload["status"] in {"found", "copied"} else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Guided onboarding for local network ingestion sources")
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--accounts", default=str(DEFAULT_ACCOUNTS_PATH))
    run_common = argparse.ArgumentParser(add_help=False)
    run_common.add_argument("--accounts", default=str(DEFAULT_ACCOUNTS_PATH))
    run_common.add_argument("--ledger", default=str(DEFAULT_ONBOARDING_LEDGER))

    run = sub.add_parser("run", parents=[run_common], help="Start/resume the conversational onboarding flow")
    run.add_argument("--force", action="store_true")
    run.set_defaults(func=cmd_run)

    cont = sub.add_parser("continue", parents=[run_common], help="Continue onboarding with the user's latest reply or a harness action")
    cont.add_argument("--input", help="Raw user reply")
    cont.add_argument("--action", choices=sorted(SIMPLE_CONTINUE_ACTIONS | LINKEDIN_CSV_CONTINUE_ACTIONS), help="Structured harness action")
    cont.add_argument("--csv", help="LinkedIn Connections.csv path to record")
    cont.add_argument("--downloads", default=str(default_downloads_dir()), help="Downloads directory for LinkedIn scan/open actions")
    cont.set_defaults(func=cmd_continue)

    skip = sub.add_parser("skip", parents=[run_common], help="Skip the current onboarding step")
    skip.set_defaults(func=cmd_skip)

    run_status = sub.add_parser("run-status", parents=[run_common], help="Show current conversational onboarding prompt")
    run_status.set_defaults(func=cmd_run_status)

    open_linkedin = sub.add_parser("open-linkedin-archive", help="Open LinkedIn data archive settings in the default browser")
    open_linkedin.set_defaults(func=cmd_open_linkedin_archive)

    open_downloads = sub.add_parser("open-downloads", help="Open Downloads in Finder")
    open_downloads.add_argument("--downloads", default=str(default_downloads_dir()))
    open_downloads.set_defaults(func=cmd_open_downloads)

    open_linkedin_drop = sub.add_parser("open-linkedin-drop-folder", parents=[common], help="Open the repo LinkedIn CSV drop folder in Finder")
    open_linkedin_drop.set_defaults(func=cmd_open_linkedin_drop_folder)

    find_linkedin = sub.add_parser("find-linkedin-connections", parents=[common], help="Find LinkedIn Connections.csv in Downloads")
    find_linkedin.add_argument("--downloads", default=str(default_downloads_dir()))
    find_linkedin.add_argument("--copy-to-repo", action="store_true")
    find_linkedin.set_defaults(func=cmd_find_linkedin_connections)

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
