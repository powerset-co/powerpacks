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

from packs.ingestion.pipeline_paths import ACCOUNTS_JSON

DEFAULT_ACCOUNTS_PATH = ACCOUNTS_JSON
CHANNELS = ["messages", "gmail", "linkedin_csv", "linkedin_mcp", "twitter"]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def empty_channel() -> dict[str, Any]:
    return {
        "linked": False,
        "usernames": [],
        "artifacts": [],
        "last_checked_at": "",
        "last_success_at": "",
        "notes": "",
    }


def default_registry() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": now_iso(),
        "accounts": {channel: empty_channel() for channel in CHANNELS},
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
    data.setdefault("version", 1)
    data.setdefault("updated_at", now_iso())
    accounts = data.setdefault("accounts", {})
    for channel in CHANNELS:
        base = empty_channel()
        base.update(accounts.get(channel) or {})
        if not isinstance(base.get("usernames"), list):
            base["usernames"] = [str(base["usernames"])] if base.get("usernames") else []
        if not isinstance(base.get("artifacts"), list):
            base["artifacts"] = [str(base["artifacts"])] if base.get("artifacts") else []
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
    save_registry(registry, path)
    return registry
