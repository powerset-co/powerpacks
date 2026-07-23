"""msgvault home state and config for the setup automation.

Owns everything under `~/.msgvault/`: the setup-state JSON (project id, owner
email, per-OAuth-app records), config.toml parsing and edits, client-secret
JSON validation/copying, the msgvault binary (install check, invocation,
init-db), OAuth-app name validation, and the deterministic
`local-msg-vault-*` project id helpers.

Changelog:
  2026-07-23 (audit):
    - Split out of the former 1,770-line setup/msgvault_setup.py.
    - Account authorization/health payloads live in accounts.py; project
      choose/create lives in gcloud_project.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sys
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode.
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.setup.automations.shell import (  # noqa: E402
    progress,
    run_command,
    tail,
)


DEFAULT_HOME = Path("~/.msgvault")
DEFAULT_CONFIG = "config.toml"
DEFAULT_STATE_FILE = "local-msg-vault-state.json"
DEFAULT_PROJECT_NAME = "local-msg-vault"
INSTALL_COMMAND = "curl -fsSL https://msgvault.io/install.sh | bash"
OAUTH_APP_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def run_msgvault(args: list[str], home: Path, *, timeout: int = 120) -> dict[str, Any]:
    """Run `msgvault --home <home> ...` with captured output."""
    return run_command(["msgvault", "--home", str(home), *args], timeout=timeout)


def ensure_msgvault(install: bool) -> dict[str, Any]:
    """Report the msgvault binary, installing it via curl when allowed."""
    path = shutil.which("msgvault")
    if path:
        version = run_command(["msgvault", "version"], timeout=15)
        return {
            "installed": True,
            "path": path,
            "version": (version.get("stdout") or version.get("stderr") or "").strip(),
            "install_attempted": False,
        }
    if not install:
        return {
            "installed": False,
            "path": "",
            "version": "",
            "install_attempted": False,
            "install_command": INSTALL_COMMAND,
        }
    result = run_command(["bash", "-lc", INSTALL_COMMAND], timeout=600)
    path = shutil.which("msgvault")
    version = run_command(["msgvault", "version"], timeout=15) if path else {"stdout": "", "stderr": ""}
    return {
        "installed": bool(path),
        "path": path or "",
        "version": (version.get("stdout") or version.get("stderr") or "").strip(),
        "install_attempted": True,
        "install_ok": result["ok"],
        "install_error": tail(result.get("stderr", "")) if not result["ok"] else "",
        "install_command": INSTALL_COMMAND,
    }


def default_project_id(seed: str = "") -> str:
    """Return a `local-msg-vault-*` project id, deterministic when seeded."""
    if seed:
        digest = hashlib.sha1(seed.strip().lower().encode("utf-8")).hexdigest()[:10]
        return f"{DEFAULT_PROJECT_NAME}-{digest}"
    return f"{DEFAULT_PROJECT_NAME}-{secrets.token_hex(3)}"


def setup_state_path(home: Path) -> Path:
    """Return the setup-state JSON path inside the msgvault home."""
    return home / DEFAULT_STATE_FILE


def load_setup_state(home: Path) -> dict[str, Any]:
    """Load the setup-state dict, returning {} for missing/corrupt files."""
    path = setup_state_path(home)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_setup_state(home: Path, state: dict[str, Any]) -> None:
    """Merge truthy state values into the setup-state JSON on disk."""
    path = setup_state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load_setup_state(home)
    current.update({key: value for key, value in state.items() if value})
    path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def save_oauth_app_state(home: Path, app_name: str, state: dict[str, Any]) -> None:
    """Merge truthy state values under oauth_apps.<app_name>, or top-level when unnamed."""
    if not app_name:
        save_setup_state(home, state)
        return
    path = setup_state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load_setup_state(home)
    apps = current.get("oauth_apps") if isinstance(current.get("oauth_apps"), dict) else {}
    existing = apps.get(app_name) if isinstance(apps.get(app_name), dict) else {}
    existing.update({key: value for key, value in state.items() if value})
    apps[app_name] = existing
    current["oauth_apps"] = apps
    path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def local_msg_vault_projects() -> list[dict[str, Any]]:
    """List ACTIVE `local-msg-vault-*` gcloud projects, newest first."""
    if not shutil.which("gcloud"):
        return []
    result = run_command(
        ["gcloud", "projects", "list", "--filter=projectId:local-msg-vault-*", "--format=json"],
        timeout=60,
    )
    if not result["ok"]:
        return []
    try:
        projects = json.loads(result["stdout"] or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(projects, list):
        return []
    active = [project for project in projects if project.get("lifecycleState") == "ACTIVE"]
    return sorted(active, key=lambda project: project.get("createTime", ""), reverse=True)


def validate_oauth_app(app_name: str | None) -> str:
    """Return a validated OAuth app name ("" when unset), raising ValueError otherwise."""
    if not app_name:
        return ""
    if not OAUTH_APP_RE.match(app_name):
        raise ValueError("--oauth-app must contain only letters, numbers, underscores, or dashes")
    return app_name


def config_path(home: Path) -> Path:
    """Return the msgvault config.toml path inside the home."""
    return home / DEFAULT_CONFIG


def db_path(home: Path) -> Path:
    """Return the msgvault SQLite database path inside the home."""
    return home / "msgvault.db"


def parse_toml_string(raw: str) -> str:
    """Decode a TOML string value leniently (JSON-quoted or bare)."""
    value = raw.strip()
    if not value:
        return ""
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, str) else str(parsed)
    except json.JSONDecodeError:
        return value.strip("'\"")


def parse_client_secret_paths(path: Path) -> dict[str, str]:
    """Return client_secrets paths from config.toml keyed by app ("default" for [oauth])."""
    if not path.exists():
        return {}
    section = ""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped.strip("[]")
            continue
        if "=" not in stripped:
            continue
        key, raw = stripped.split("=", 1)
        if key.strip() != "client_secrets":
            continue
        if section == "oauth":
            values["default"] = parse_toml_string(raw)
        elif section.startswith("oauth.apps."):
            values[section.removeprefix("oauth.apps.")] = parse_toml_string(raw)
    return values


def validate_client_secret(path: Path) -> dict[str, Any]:
    """Validate an installed-app OAuth client secret JSON file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"ok": False, "message": f"{path} does not exist"}
    except json.JSONDecodeError as exc:
        return {"ok": False, "message": f"{path} is not valid JSON: {exc}"}
    if not isinstance(data, dict):
        return {"ok": False, "message": "client secret JSON must be an object"}
    installed = data.get("installed")
    if not isinstance(installed, dict):
        return {"ok": False, "message": "expected an installed-app OAuth JSON with an 'installed' object"}
    missing = [key for key in ("client_id", "client_secret") if not installed.get(key)]
    if missing:
        return {"ok": False, "message": f"client secret JSON is missing: {', '.join(missing)}"}
    return {
        "ok": True,
        "client_id": installed.get("client_id", ""),
        "has_client_secret": bool(installed.get("client_secret")),
        "redirect_uris": installed.get("redirect_uris") or [],
    }


def destination_secret_path(home: Path, app_name: str) -> Path:
    """Return the in-home destination path for a (possibly app-named) client secret."""
    suffix = f"_{app_name}" if app_name else ""
    return home / f"client_secret{suffix}.json"


def copy_client_secret(source: Path, home: Path, app_name: str, *, copy_secret: bool) -> dict[str, Any]:
    """Validate a client secret and copy it into the home (chmod 600) unless referenced in place."""
    validation = validate_client_secret(source)
    if not validation["ok"]:
        return {"ok": False, "message": validation["message"]}
    if copy_secret:
        dest = destination_secret_path(home, app_name)
        home.mkdir(parents=True, exist_ok=True)
        if source.resolve() != dest.resolve():
            shutil.copyfile(source, dest)
        os.chmod(dest, 0o600)
    else:
        dest = source
    return {
        "ok": True,
        "source": str(source),
        "path": str(dest),
        "client_id": validation["client_id"],
        "redirect_uris": validation.get("redirect_uris") or [],
    }


def configured_client_secret(home: Path, app_name: str) -> dict[str, Any] | None:
    """Return the configured client secret record for an app, or None when unconfigured."""
    key = app_name or "default"
    secret_paths = parse_client_secret_paths(config_path(home))
    raw_path = secret_paths.get(key)
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    validation = validate_client_secret(path)
    if not validation["ok"]:
        return {
            "status": "invalid",
            "path": str(path),
            "message": validation["message"],
        }
    return {
        "status": "configured",
        "config": str(config_path(home)),
        "client_secret_path": str(path),
        "client_id": validation["client_id"],
        "redirect_uris": validation.get("redirect_uris") or [],
    }


def set_toml_value(text: str, table: str, key: str, value: str) -> str:
    """Set key = "value" inside a TOML table, appending the table when absent."""
    lines = text.splitlines()
    header = f"[{table}]"
    quoted = json.dumps(value)
    key_line = f"{key} = {quoted}"
    start = -1
    end = len(lines)
    for i, line in enumerate(lines):
        if line.strip() == header:
            start = i
            break
    if start == -1:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([header, key_line])
        return "\n".join(lines).rstrip() + "\n"
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = i
            break
    for i in range(start + 1, end):
        stripped = lines[i].strip()
        if stripped.startswith(f"{key}") and "=" in stripped:
            lines[i] = key_line
            return "\n".join(lines).rstrip() + "\n"
    lines.insert(start + 1, key_line)
    return "\n".join(lines).rstrip() + "\n"


def write_msgvault_config(path: Path, client_secret_path: Path, app_name: str = "") -> None:
    """Point config.toml's [oauth] (or [oauth.apps.<name>]) client_secrets at the file, chmod 600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    table = "oauth" if not app_name else f"oauth.apps.{app_name}"
    text = set_toml_value(text, table, "client_secrets", str(client_secret_path))
    if "[sync]" not in text:
        if text and not text.endswith("\n\n"):
            text = text.rstrip() + "\n\n"
        text += "[sync]\nrate_limit_qps = 5\n"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)


def init_db(home: Path) -> dict[str, Any]:
    """Run `msgvault init-db` for the home and report the database path."""
    progress("Initializing msgvault database...")
    result = run_msgvault(["init-db"], home, timeout=120)
    if result["ok"]:
        progress("msgvault database ready.")
        return {"status": "ok", "path": str(db_path(home))}
    return {"status": "error", "path": str(db_path(home)), "message": tail(result.get("stderr") or result.get("stdout") or "")}
