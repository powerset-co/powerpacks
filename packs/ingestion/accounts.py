"""Local ingestion account registry.

Stored at `.powerpacks/ingestion/accounts.json`. Powerpacks uses JSON for local
generated state/config so the registry can be read/written with the Python
stdlib while remaining easy for users to inspect and edit.

No secrets belong here. Store only usernames, linked/export status, artifact
paths, and setup notes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_ACCOUNTS_PATH = Path(".powerpacks/ingestion/accounts.json")
CHANNELS = ["messages", "gmail", "linkedin_csv", "twitter"]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def empty_config(channel: str) -> dict[str, Any]:
    if channel == "gmail":
        return {
            "msgvault_db": "",
            "account_emails": [],
            "oauth_app": "",
            "oauth_test_users": [],
            "available_accounts": [],
            "selected_accounts": [],
        }
    if channel == "linkedin_csv":
        return {"csv_path": "", "source_label": ""}
    if channel == "twitter":
        return {"handle": ""}
    if channel == "messages":
        return {"contacts_csv": ""}
    return {}


def empty_channel(channel: str = "") -> dict[str, Any]:
    return {
        "linked": False,
        "skipped": False,
        "usernames": [],
        "artifacts": [],
        "config": empty_config(channel),
        "last_checked_at": "",
        "last_success_at": "",
        "notes": "",
    }


def default_registry() -> dict[str, Any]:
    return {
        "version": 2,
        "updated_at": now_iso(),
        "accounts": {channel: empty_channel(channel) for channel in CHANNELS},
    }


def load_registry(path: Path = DEFAULT_ACCOUNTS_PATH) -> dict[str, Any]:
    if not path.exists():
        return default_registry()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Keep the implementation stdlib-only and explicit. If a user hand-edits
        # invalid JSON, ask them to restore/regenerate via account_registry.
        raise ValueError(f"{path} must be valid JSON")
    if not isinstance(data, dict):
        data = default_registry()
    data["version"] = max(int(data.get("version") or 1), 2)
    data.setdefault("updated_at", now_iso())
    accounts = data.setdefault("accounts", {})
    for channel in CHANNELS:
        base = empty_channel(channel)
        base.update(accounts.get(channel) or {})
        cfg = empty_config(channel)
        if isinstance(base.get("config"), dict):
            cfg.update(base["config"])
        base["config"] = cfg
        if not isinstance(base.get("usernames"), list):
            base["usernames"] = [str(base["usernames"])] if base.get("usernames") else []
        if not isinstance(base.get("artifacts"), list):
            base["artifacts"] = [str(base["artifacts"])] if base.get("artifacts") else []
        # Preserve v1 mirrors while seeding v2 config for handoffs.
        if channel == "gmail":
            for key in ["account_emails", "oauth_test_users", "available_accounts", "selected_accounts"]:
                if not isinstance(base["config"].get(key), list):
                    base["config"][key] = [str(base["config"][key])] if base["config"].get(key) else []
            if base["usernames"] and not base["config"].get("account_emails"):
                base["config"]["account_emails"] = list(base["usernames"])
            if base["usernames"] and not base["config"].get("selected_accounts"):
                base["config"]["selected_accounts"] = list(base["usernames"])
        elif channel == "linkedin_csv":
            if base["artifacts"] and not base["config"].get("csv_path"):
                base["config"]["csv_path"] = base["artifacts"][0]
            if base["usernames"] and not base["config"].get("source_label"):
                base["config"]["source_label"] = base["usernames"][0]
        elif channel == "twitter":
            if base["usernames"] and not base["config"].get("handle"):
                base["config"]["handle"] = base["usernames"][0]
        elif channel == "messages":
            if base["artifacts"] and not base["config"].get("contacts_csv"):
                base["config"]["contacts_csv"] = base["artifacts"][0]
        accounts[channel] = base
    return data


def save_registry(registry: dict[str, Any], path: Path = DEFAULT_ACCOUNTS_PATH) -> None:
    registry["updated_at"] = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def update_channel(
    channel: str,
    *,
    path: Path = DEFAULT_ACCOUNTS_PATH,
    linked: bool | None = None,
    skipped: bool | None = None,
    username: str | None = None,
    artifact: str | None = None,
    notes: str | None = None,
    success: bool = False,
) -> dict[str, Any]:
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel {channel!r}; expected one of {CHANNELS}")
    registry = load_registry(path)
    rec = registry["accounts"][channel]
    if linked is not None:
        rec["linked"] = linked
    if skipped is not None:
        rec["skipped"] = skipped
    if username:
        if username not in rec["usernames"]:
            rec["usernames"].append(username)
    if artifact:
        if artifact not in rec["artifacts"]:
            rec["artifacts"].append(artifact)
    if notes is not None:
        rec["notes"] = notes
    rec["last_checked_at"] = now_iso()
    if success:
        rec["last_success_at"] = now_iso()
        rec["linked"] = True
        rec["skipped"] = False
    save_registry(registry, path)
    return registry
