#!/usr/bin/env python3
"""Set up msgvault for local Gmail archive access.

The Google Cloud console still owns classic installed-app OAuth client
creation for Gmail scopes. This primitive automates the local pieces around
that step: install/status, Gmail API enabling, config.toml updates, account
auth, and Codex MCP registration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any
from urllib.parse import quote


DEFAULT_HOME = Path("~/.msgvault")
DEFAULT_CONFIG = "config.toml"
DEFAULT_STATE_FILE = "local-msg-vault-state.json"
DEFAULT_BROWSER_PROFILE = Path("~/.powerpacks/browser-profiles/google-oauth")
DEFAULT_DOWNLOAD_DIR = Path("~/.msgvault/oauth-downloads")
DEFAULT_NODE_DEPS = Path("~/.powerpacks/browser-node")
DEFAULT_OAUTH_CLIENT_NAME = "local-msg-vault"
DEFAULT_PROJECT_NAME = "local-msg-vault"
GMAIL_SERVICE = "gmail.googleapis.com"
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]
INSTALL_COMMAND = "curl -fsSL https://msgvault.io/install.sh | bash"
OAUTH_APP_RE = re.compile(r"^[A-Za-z0-9_-]+$")
PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MSGVAULT_REAUTH_ERROR_MARKERS = (
    "expired or revoked",
    "cannot re-authorize",
    "invalid_grant",
    "missing token",
    "no valid token",
    "token is missing",
)


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def progress(message: str) -> None:
    print(f"[local-msg-vault] {message}", file=sys.stderr, flush=True)


def expand(path: str | Path) -> Path:
    return Path(path).expanduser()


def run_command(cmd: list[str], *, timeout: int = 90, env: dict[str, str] | None = None) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError:
        return {"ok": False, "returncode": 127, "stdout": "", "stderr": f"{cmd[0]} not found"}
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": 124,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or f"{cmd[0]} timed out",
        }
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def run_visible_command(cmd: list[str], *, timeout: int | None = None) -> dict[str, Any]:
    try:
        completed = subprocess.run(cmd, timeout=timeout)
    except FileNotFoundError:
        return {"ok": False, "returncode": 127, "message": f"{cmd[0]} not found"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": 124, "message": f"{cmd[0]} timed out"}
    return {"ok": completed.returncode == 0, "returncode": completed.returncode, "message": ""}


def run_streaming_command(cmd: list[str], *, timeout: int, env: dict[str, str] | None = None) -> dict[str, Any]:
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    except FileNotFoundError:
        return {"ok": False, "returncode": 127, "stdout": "", "stderr": f"{cmd[0]} not found"}

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def read_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            stdout_chunks.append(line)

    def read_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_chunks.append(line)
            print(line, end="", file=sys.stderr, flush=True)

    threads = [threading.Thread(target=read_stdout), threading.Thread(target=read_stderr)]
    for thread in threads:
        thread.daemon = True
        thread.start()
    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        returncode = 124
    for thread in threads:
        thread.join(timeout=1)
    return {
        "ok": returncode == 0,
        "returncode": returncode,
        "stdout": "".join(stdout_chunks),
        "stderr": "".join(stderr_chunks),
    }


def run_msgvault(args: list[str], home: Path, *, timeout: int = 120) -> dict[str, Any]:
    return run_command(["msgvault", "--home", str(home), *args], timeout=timeout)


def ensure_msgvault(install: bool) -> dict[str, Any]:
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


def tail(text: str, limit: int = 1600) -> str:
    text = (text or "").strip()
    return text[-limit:] if len(text) > limit else text


def parse_json_fragment(text: str) -> Any:
    decoder = json.JSONDecoder()
    for idx, char in enumerate(text or ""):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text[idx:])
            return value
        except json.JSONDecodeError:
            continue
    raise json.JSONDecodeError("no JSON object or array found", text or "", 0)


def gcloud_value(args: list[str]) -> str:
    result = run_command(["gcloud", *args], timeout=20)
    if not result["ok"]:
        return ""
    value = result["stdout"].strip()
    return "" if value == "(unset)" else value


def is_gcloud_reauth_error(text: str) -> bool:
    haystack = (text or "").lower()
    return (
        "problem refreshing your current auth tokens" in haystack
        or "reauthentication failed" in haystack
        or "cannot prompt during non-interactive execution" in haystack
        or "please run:" in haystack and "gcloud auth login" in haystack
    )


def is_project_taken_error(text: str) -> bool:
    # A deterministic project id can be globally reserved by another account, or
    # held in Google's 30-day soft-delete purge window, even when the active
    # account cannot describe it. gcloud surfaces this as "already in use".
    haystack = (text or "").lower()
    return (
        "already in use by another project" in haystack
        or "requested entity already exists" in haystack
        or "project id you specified is already in use" in haystack
    )


def gcloud_context(project: str | None = None) -> dict[str, Any]:
    gcloud_path = shutil.which("gcloud") or ""
    active_project = project or gcloud_value(["config", "get-value", "project", "--quiet"])
    account = gcloud_value(["config", "get-value", "account", "--quiet"]) if gcloud_path else ""
    return {
        "installed": bool(gcloud_path),
        "path": gcloud_path,
        "account": account,
        "project": active_project or "",
    }


def default_project_id(seed: str = "") -> str:
    if seed:
        digest = hashlib.sha1(seed.strip().lower().encode("utf-8")).hexdigest()[:10]
        return f"{DEFAULT_PROJECT_NAME}-{digest}"
    return f"{DEFAULT_PROJECT_NAME}-{secrets.token_hex(3)}"


def setup_state_path(home: Path) -> Path:
    return home / DEFAULT_STATE_FILE


def load_setup_state(home: Path) -> dict[str, Any]:
    path = setup_state_path(home)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_setup_state(home: Path, state: dict[str, Any]) -> None:
    path = setup_state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load_setup_state(home)
    current.update({key: value for key, value in state.items() if value})
    path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def save_oauth_app_state(home: Path, app_name: str, state: dict[str, Any]) -> None:
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


def choose_project_id(home: Path, requested_project: str, email: str, account: str, app_name: str = "") -> tuple[str, dict[str, Any]]:
    if requested_project:
        return validate_project_id(requested_project), {"source": "argument"}
    state = load_setup_state(home)
    state_source = state
    if app_name:
        apps = state.get("oauth_apps") if isinstance(state.get("oauth_apps"), dict) else {}
        state_source = apps.get(app_name) if isinstance(apps.get(app_name), dict) else {}
    state_project = validate_project_id(str(state_source.get("project_id") or "")) if state_source.get("project_id") else ""
    if state_project:
        return state_project, {"source": "state", "state_path": str(setup_state_path(home))}
    if email and account and email.lower() != account.lower():
        project_id = default_project_id(email)
        save_oauth_app_state(home, app_name, {"project_id": project_id, "email": email})
        return project_id, {"source": "deterministic_default", "state_path": str(setup_state_path(home))}
    current = gcloud_value(["config", "get-value", "project", "--quiet"])
    if current.startswith(f"{DEFAULT_PROJECT_NAME}-"):
        save_oauth_app_state(home, app_name, {"project_id": current, "email": email or account})
        return validate_project_id(current), {"source": "gcloud_current_project"}
    candidates = local_msg_vault_projects()
    if candidates:
        project_id = validate_project_id(str(candidates[0].get("projectId") or ""))
        if project_id:
            save_oauth_app_state(home, app_name, {"project_id": project_id})
            return project_id, {
                "source": "existing_local_msg_vault_project",
                "existing_count": len(candidates),
                "state_path": str(setup_state_path(home)),
            }
    seed = email or account
    project_id = default_project_id(seed)
    save_oauth_app_state(home, app_name, {"project_id": project_id, "email": email or account})
    return project_id, {"source": "deterministic_default", "state_path": str(setup_state_path(home))}


def validate_project_id(project_id: str) -> str:
    if not project_id:
        return ""
    if not PROJECT_ID_RE.match(project_id):
        raise ValueError(
            "--project must be 6-30 chars, start with a lowercase letter, "
            "and contain only lowercase letters, numbers, or dashes"
        )
    return project_id


def ensure_gcloud_auth(open_browser: bool, expected_account: str = "") -> dict[str, Any]:
    if not shutil.which("gcloud"):
        return {"status": "error", "message": "gcloud not installed"}
    progress("Checking Google Cloud login...")
    expected = expected_account.strip()
    account = gcloud_value(["config", "get-value", "account", "--quiet"])
    needs_login = bool(expected and account and account.lower() != expected.lower())
    if account and not needs_login:
        token = run_command(["gcloud", "auth", "print-access-token", "--quiet"], timeout=30)
        if token["ok"]:
            if expected and account.lower() != expected.lower():
                return {
                    "status": "error",
                    "account": account,
                    "expected_account": expected,
                    "message": f"Google Cloud login must use {expected}; active account is {account}.",
                    "login_ran": False,
                }
            progress(f"Google Cloud login confirmed as {account}.")
            return {"status": "ok", "account": account, "login_ran": False}
        token_error = token.get("stderr") or token.get("stdout") or ""
        if not is_gcloud_reauth_error(token_error):
            return {
                "status": "error",
                "account": account,
                "message": tail(token_error),
                "login_ran": False,
            }
    cmd = ["gcloud", "auth", "login"]
    if expected:
        cmd.append(expected)
    if not open_browser:
        cmd.append("--no-launch-browser")
    progress("Refreshing Google Cloud login...")
    result = run_visible_command(cmd, timeout=900)
    account = gcloud_value(["config", "get-value", "account", "--quiet"])
    if result["ok"] and expected and account.lower() != expected.lower():
        return {
            "status": "error",
            "account": account,
            "expected_account": expected,
            "message": f"Google Cloud login must use {expected}; active account is {account or 'unknown'}.",
            "login_ran": True,
        }
    token = run_command(["gcloud", "auth", "print-access-token", "--quiet"], timeout=30) if account else {"ok": False, "stderr": ""}
    if result["ok"] and account and token["ok"]:
        progress(f"Google Cloud login refreshed as {account}.")
        return {"status": "ok", "account": account, "login_ran": True}
    return {"status": "error", "message": result.get("message") or "gcloud login did not finish"}


def project_exists(project_id: str) -> bool:
    if not project_id or not shutil.which("gcloud"):
        return False
    result = run_command(["gcloud", "projects", "describe", project_id, "--format=json"], timeout=60)
    return result["ok"]


def create_gcloud_project(
    project_id: str,
    project_name: str,
    *,
    allow_fallback: bool = True,
    max_attempts: int = 4,
) -> dict[str, Any]:
    project_id = validate_project_id(project_id)
    if not shutil.which("gcloud"):
        return {"status": "error", "message": "gcloud not installed"}
    progress(f"Checking Google Cloud project {project_id}...")
    if project_exists(project_id):
        progress(f"Using existing Google Cloud project {project_id}.")
        return {"status": "ok", "project": project_id, "created": False}

    attempt_id = project_id
    fallbacks_used: list[str] = []
    for attempt in range(max_attempts):
        progress(f"Creating Google Cloud project {attempt_id}...")
        result = run_command(
            ["gcloud", "projects", "create", attempt_id, "--name", project_name, "--format=json"],
            timeout=180,
        )
        if result["ok"]:
            progress(f"Google Cloud project {attempt_id} created.")
            return {
                "status": "ok",
                "project": attempt_id,
                "created": True,
                "requested_project": project_id,
                "fallbacks_used": fallbacks_used,
            }
        message = tail(result.get("stderr") or result.get("stdout") or "")
        if is_gcloud_reauth_error(message):
            return {
                "status": "reauth_required",
                "project": attempt_id,
                "message": message,
                "fix_command": "gcloud auth login",
            }
        # The deterministic id is globally reserved (owned by another account or
        # in soft-delete limbo). Retry with a fresh random id so a fresh install
        # is not blocked for 30 days by a poisoned project id.
        if allow_fallback and is_project_taken_error(message) and attempt < max_attempts - 1:
            attempt_id = default_project_id()
            fallbacks_used.append(attempt_id)
            progress(f"Project id is already in use; retrying with {attempt_id}...")
            continue
        return {
            "status": "error",
            "project": attempt_id,
            "requested_project": project_id,
            "fallbacks_used": fallbacks_used,
            "message": message,
        }
    return {
        "status": "error",
        "project": attempt_id,
        "requested_project": project_id,
        "fallbacks_used": fallbacks_used,
        "message": "exhausted project id creation attempts",
    }


def set_gcloud_project(project_id: str) -> dict[str, Any]:
    if not project_id:
        return {"status": "skipped", "reason": "missing project"}
    progress(f"Setting active Google Cloud project to {project_id}...")
    result = run_command(["gcloud", "config", "set", "project", project_id, "--quiet"], timeout=30)
    if result["ok"]:
        progress(f"Active Google Cloud project set to {project_id}.")
        return {"status": "ok", "project": project_id}
    return {"status": "error", "project": project_id, "message": tail(result.get("stderr") or result.get("stdout") or "")}


def enable_gmail_api(project: str | None) -> dict[str, Any]:
    if not project:
        return {"status": "skipped", "reason": "missing gcloud project"}
    if not shutil.which("gcloud"):
        return {"status": "skipped", "reason": "gcloud not installed"}
    progress(f"Enabling Gmail API for {project}. This can take a minute...")
    result = run_command(["gcloud", "services", "enable", GMAIL_SERVICE, "--project", project, "--quiet"], timeout=180)
    if result["ok"]:
        progress("Gmail API enabled.")
        return {"status": "ok", "project": project, "service": GMAIL_SERVICE}
    message = tail(result.get("stderr") or result.get("stdout") or "")
    if is_gcloud_reauth_error(message):
        return {
            "status": "reauth_required",
            "project": project,
            "service": GMAIL_SERVICE,
            "message": message,
            "fix_command": "gcloud auth login",
        }
    return {
        "status": "error",
        "project": project,
        "service": GMAIL_SERVICE,
        "message": message,
    }


def config_path(home: Path) -> Path:
    return home / DEFAULT_CONFIG


def db_path(home: Path) -> Path:
    return home / "msgvault.db"


def parse_toml_string(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, str) else str(parsed)
    except json.JSONDecodeError:
        return value.strip("'\"")


def parse_client_secret_paths(path: Path) -> dict[str, str]:
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


def status_payload(home: Path) -> dict[str, Any]:
    msgvault_path = shutil.which("msgvault") or ""
    version = run_command(["msgvault", "version"], timeout=15) if msgvault_path else {"stdout": "", "stderr": ""}
    cfg_path = config_path(home)
    secrets = parse_client_secret_paths(cfg_path)
    accounts: Any = []
    accounts_error = ""
    if msgvault_path and db_path(home).exists():
        result = run_msgvault(["list-accounts", "--json", "--local"], home, timeout=30)
        if result["ok"]:
            try:
                accounts = parse_json_fragment(result["stdout"] or "[]")
            except json.JSONDecodeError:
                if "No accounts found" in result["stdout"]:
                    accounts = []
                else:
                    accounts_error = "list-accounts did not return JSON"
        else:
            accounts_error = tail(result.get("stderr") or result.get("stdout") or "")
    mcp = mcp_status()
    secret_records = {
        name: {"path": value, "exists": bool(value and Path(value).expanduser().exists())}
        for name, value in secrets.items()
    }
    # "ready" means the user can actually sync: vault configured AND at least one
    # authorized account. Without the account gate the Gmail page jumps to the
    # stats view and the authorize step becomes unreachable.
    ready = bool(msgvault_path and cfg_path.exists() and secrets and db_path(home).exists() and accounts)
    setup_state = load_setup_state(home)
    owner_email = str(setup_state.get("email") or "")
    # The emails the user asked to authorize live in setup state as test_users
    # (saved by add-test-users). They're the source of truth for "accounts
    # available to authorize" — msgvault only knows who's already authorized.
    desired_emails = normalize_email_list([owner_email, *(setup_state.get("test_users") or [])])
    return {
        "status": "ok" if ready else "needs_setup",
        "home": str(home),
        "owner_email": owner_email,
        "desired_emails": desired_emails,
        "msgvault": {
            "installed": bool(msgvault_path),
            "path": msgvault_path,
            "version": (version.get("stdout") or version.get("stderr") or "").strip(),
        },
        "database": {"path": str(db_path(home)), "exists": db_path(home).exists()},
        "config": {
            "path": str(cfg_path),
            "exists": cfg_path.exists(),
            "oauth_configured": bool(secrets),
            "client_secrets": secret_records,
        },
        "accounts": accounts,
        "accounts_error": accounts_error,
        "mcp": mcp,
        "gcloud": gcloud_context(),
    }


def validate_oauth_app(app_name: str | None) -> str:
    if not app_name:
        return ""
    if not OAUTH_APP_RE.match(app_name):
        raise ValueError("--oauth-app must contain only letters, numbers, underscores, or dashes")
    return app_name


def normalize_email_list(values: list[str]) -> list[str]:
    emails: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in re.split(r"[,\s]+", value or ""):
            email = item.strip()
            if not email:
                continue
            key = email.lower()
            if key in seen:
                continue
            if not EMAIL_RE.match(email):
                raise ValueError(f"invalid email address: {email}")
            seen.add(key)
            emails.append(email)
    return emails


def msgvault_reauthorization_required(text: str) -> bool:
    haystack = (text or "").lower()
    return any(marker in haystack for marker in MSGVAULT_REAUTH_ERROR_MARKERS)


def msgvault_account_authorize_command(home: Path, email: str, *, force: bool) -> str:
    cmd = [
        "uv",
        "run",
        "--project",
        ".",
        "python",
        "packs/ingestion/primitives/setup/msgvault_setup.py",
        "add-account",
        "--home",
        str(home),
        "--email",
        email,
    ]
    if force:
        cmd.append("--force-auth")
    return shlex.join(cmd)


def check_accounts_payload(home: Path, requested_emails: list[str]) -> dict[str, Any]:
    requested = normalize_email_list(requested_emails)
    current = status_payload(home)
    setup_errors: list[str] = []
    if not current.get("msgvault", {}).get("installed"):
        setup_errors.append("msgvault is not installed")
    if not current.get("config", {}).get("oauth_configured"):
        setup_errors.append("msgvault OAuth is not configured")
    if not current.get("database", {}).get("exists"):
        setup_errors.append("msgvault database is missing")
    if current.get("accounts_error"):
        setup_errors.append(str(current["accounts_error"]))
    if setup_errors:
        error = "; ".join(setup_errors)
        return {
            "status": "error",
            "home": str(home),
            "requested_accounts": requested,
            "healthy_accounts": [],
            "missing_accounts": [],
            "expired_accounts": [],
            "accounts_to_authorize": [],
            "error_accounts": requested,
            "accounts": [
                {
                    "email": email,
                    "status": "transient_error",
                    "error_code": "gmail_auth_check_unavailable",
                    "error": error,
                }
                for email in requested
            ],
            "browser_opened": False,
            "mail_downloaded": False,
            "network_called": False,
        }
    stored_accounts = {
        str(account.get("email") or account.get("identifier") or "").strip().lower(): account
        for account in (current.get("accounts") or [])
        if isinstance(account, dict)
    }
    checks: list[dict[str, Any]] = []
    network_called = False
    for email in requested:
        normalized = email.lower()
        if normalized not in stored_accounts:
            checks.append({
                "email": email,
                "status": "missing_token",
                "error_code": "gmail_authorization_missing",
                "authorize_command": msgvault_account_authorize_command(home, email, force=False),
            })
            continue
        network_called = True
        result = run_msgvault(
            ["verify", email, "--skip-db-check", "--sample", "0", "--local"],
            home,
            timeout=60,
        )
        if result["ok"]:
            checks.append({"email": email, "status": "healthy"})
            continue
        error = tail(result.get("stderr") or result.get("stdout") or "")
        if msgvault_reauthorization_required(error):
            checks.append({
                "email": email,
                "status": "reauthorization_required",
                "error_code": "gmail_reauthorization_required",
                "error": error,
                "authorize_command": msgvault_account_authorize_command(home, email, force=True),
            })
            continue
        checks.append({
            "email": email,
            "status": "transient_error",
            "error_code": "gmail_auth_check_failed",
            "error": error or f"msgvault verify exited with {result.get('returncode')}",
        })
    healthy_accounts = [item["email"] for item in checks if item["status"] == "healthy"]
    missing_accounts = [item["email"] for item in checks if item["status"] == "missing_token"]
    expired_accounts = [item["email"] for item in checks if item["status"] == "reauthorization_required"]
    errors = [item["email"] for item in checks if item["status"] == "transient_error"]
    accounts_to_authorize = [
        item["email"]
        for item in checks
        if item["status"] in {"missing_token", "reauthorization_required"}
    ]
    if errors:
        status = "error"
    elif accounts_to_authorize:
        status = "needs_user_action"
    else:
        status = "ok"
    return {
        "status": status,
        "home": str(home),
        "requested_accounts": requested,
        "healthy_accounts": healthy_accounts,
        "missing_accounts": missing_accounts,
        "expired_accounts": expired_accounts,
        "accounts_to_authorize": accounts_to_authorize,
        "error_accounts": errors,
        "accounts": checks,
        "browser_opened": False,
        "mail_downloaded": False,
        "network_called": network_called,
    }


def validate_client_secret(path: Path) -> dict[str, Any]:
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
    suffix = f"_{app_name}" if app_name else ""
    return home / f"client_secret{suffix}.json"


def copy_client_secret(source: Path, home: Path, app_name: str, *, copy_secret: bool) -> dict[str, Any]:
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
    progress("Initializing msgvault database...")
    result = run_msgvault(["init-db"], home, timeout=120)
    if result["ok"]:
        progress("msgvault database ready.")
        return {"status": "ok", "path": str(db_path(home))}
    return {"status": "error", "path": str(db_path(home)), "message": tail(result.get("stderr") or result.get("stdout") or "")}


def add_account(home: Path, email: str, app_name: str, *, headless: bool, force: bool) -> dict[str, Any]:
    progress(f"Authorizing msgvault account {email}...")
    cmd = ["msgvault", "--home", str(home), "add-account", email]
    if headless:
        cmd.append("--headless")
    if force:
        cmd.append("--force")
    if app_name:
        cmd.extend(["--oauth-app", app_name])
    result = run_visible_command(cmd, timeout=900)
    if result["ok"]:
        progress("msgvault account authorized.")
        return {"status": "ok", "email": email, "oauth_app": app_name or "default"}
    return {
        "status": "error",
        "email": email,
        "oauth_app": app_name or "default",
        "message": result.get("message") or f"msgvault add-account exited with {result.get('returncode')}",
    }


def mcp_status() -> dict[str, Any]:
    if not shutil.which("codex"):
        return {"installed": False, "available": False, "reason": "codex not found"}
    result = run_command(["codex", "mcp", "get", "msgvault"], timeout=20)
    return {
        "available": True,
        "installed": result["ok"],
        "message": "" if result["ok"] else tail(result.get("stderr") or result.get("stdout") or ""),
    }


def install_mcp() -> dict[str, Any]:
    if not shutil.which("codex"):
        return {"status": "skipped", "reason": "codex not found"}
    current = mcp_status()
    if current.get("installed"):
        return {"status": "ok", "already_installed": True}
    progress("Installing msgvault MCP in Codex...")
    result = run_command(["codex", "mcp", "add", "msgvault", "--", "msgvault", "mcp"], timeout=60)
    if result["ok"]:
        progress("msgvault MCP installed.")
        return {"status": "ok", "already_installed": False}
    return {"status": "error", "message": tail(result.get("stderr") or result.get("stdout") or "")}


def console_urls(project: str | None) -> dict[str, str]:
    suffix = f"?project={quote(project)}" if project else ""
    return {
        "gmail_api": f"https://console.cloud.google.com/apis/library/{GMAIL_SERVICE}{suffix}",
        "oauth_consent": f"https://console.cloud.google.com/auth/overview{suffix}",
        "credentials": f"https://console.cloud.google.com/apis/credentials{suffix}",
        "oauth_client": f"https://console.cloud.google.com/apis/credentials/oauthclient{suffix}",
    }


def open_urls(urls: list[str]) -> list[str]:
    opened: list[str] = []
    if not urls:
        return opened
    opener: list[str] | None = None
    if platform.system() == "Darwin" and shutil.which("open"):
        opener = ["open"]
    elif shutil.which("xdg-open"):
        opener = ["xdg-open"]
    if not opener:
        return opened
    for url in urls:
        result = run_command([*opener, url], timeout=10)
        if result["ok"]:
            opened.append(url)
    return opened


def latest_client_secret(paths: list[Path]) -> Path | None:
    matches: list[Path] = []
    for base in paths:
        if not base.exists():
            continue
        if base.is_file() and base.name.startswith("client_secret") and base.suffix == ".json":
            matches.append(base)
        elif base.is_dir():
            matches.extend(base.glob("client_secret*.json"))
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def ensure_playwright_core(node_deps: Path = DEFAULT_NODE_DEPS.expanduser()) -> dict[str, Any]:
    if not shutil.which("node"):
        return {"status": "error", "message": "node is not installed"}
    if not shutil.which("npm"):
        return {"status": "error", "message": "npm is not installed"}
    package_dir = node_deps / "node_modules" / "playwright-core"
    if package_dir.exists():
        return {
            "status": "ok",
            "installed": False,
            "node_path": str(node_deps / "node_modules"),
        }
    node_deps.mkdir(parents=True, exist_ok=True)
    progress("Installing browser automation runtime...")
    result = run_command(["npm", "install", "--prefix", str(node_deps), "playwright-core"], timeout=300)
    if not result["ok"]:
        return {"status": "error", "message": tail(result.get("stderr") or result.get("stdout") or "")}
    progress("Browser automation runtime ready.")
    return {
        "status": "ok",
        "installed": True,
        "node_path": str(node_deps / "node_modules"),
    }


def run_browser_automation(
    *,
    project: str,
    email: str,
    oauth_client_name: str,
    profile_dir: Path,
    download_dir: Path,
    timeout_seconds: int,
    audience: str,
) -> dict[str, Any]:
    progress("Opening Chrome to create the Google OAuth app...")
    deps = ensure_playwright_core()
    if deps["status"] != "ok":
        return deps
    script = Path(__file__).with_name("google_oauth_browser.js")
    download_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "node",
        str(script),
        "--project",
        project,
        "--email",
        email,
        "--client-name",
        oauth_client_name,
        "--profile-dir",
        str(profile_dir),
        "--download-dir",
        str(download_dir),
        "--timeout-seconds",
        str(timeout_seconds),
        "--audience",
        audience,
    ]
    env = {**os.environ, "NODE_PATH": deps["node_path"]}
    result = run_streaming_command(cmd, timeout=timeout_seconds + 180, env=env)
    payload: dict[str, Any]
    try:
        payload = parse_json_fragment(result.get("stdout", ""))
    except json.JSONDecodeError:
        payload = {
            "status": "error",
            "message": tail(result.get("stderr") or result.get("stdout") or ""),
        }
    if not result["ok"] and payload.get("status") == "ok":
        payload["status"] = "error"
    payload.setdefault("returncode", result["returncode"])
    if result.get("stderr"):
        payload.setdefault("log", tail(result["stderr"]))
    payload.setdefault("browser_deps", deps)
    if payload.get("status") == "ok":
        progress("Google OAuth client secret downloaded.")
    else:
        progress("Chrome is waiting for Google OAuth setup to finish.")
    return payload


def run_browser_add_test_users(
    *,
    project: str,
    email: str,
    test_users: list[str],
    profile_dir: Path,
    download_dir: Path,
    timeout_seconds: int,
    oauth_client_name: str = DEFAULT_OAUTH_CLIENT_NAME,
) -> dict[str, Any]:
    progress("Opening Chrome to add Google OAuth test users...")
    deps = ensure_playwright_core()
    if deps["status"] != "ok":
        return deps
    script = Path(__file__).with_name("google_oauth_browser.js")
    download_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "node",
        str(script),
        "--mode",
        "add-test-users",
        "--project",
        project,
        "--email",
        email,
        "--client-name",
        oauth_client_name,
        "--profile-dir",
        str(profile_dir),
        "--download-dir",
        str(download_dir),
        "--timeout-seconds",
        str(timeout_seconds),
        "--test-users",
        ",".join(test_users),
    ]
    env = {**os.environ, "NODE_PATH": deps["node_path"]}
    result = run_streaming_command(cmd, timeout=timeout_seconds + 180, env=env)
    try:
        payload: dict[str, Any] = parse_json_fragment(result.get("stdout", ""))
    except json.JSONDecodeError:
        payload = {
            "status": "error",
            "message": tail(result.get("stderr") or result.get("stdout") or ""),
        }
    if not result["ok"] and payload.get("status") == "ok":
        payload["status"] = "error"
    payload.setdefault("returncode", result["returncode"])
    if result.get("stderr"):
        payload.setdefault("log", tail(result["stderr"]))
    payload.setdefault("browser_deps", deps)
    if payload.get("status") == "ok":
        progress("Google OAuth test users updated.")
    else:
        progress("Chrome is waiting for Google OAuth test user setup to finish.")
    return payload


def build_user_action(
    project: str | None,
    email: str | None,
    app_name: str,
    home: Path,
    oauth_client_name: str = DEFAULT_OAUTH_CLIENT_NAME,
) -> dict[str, Any]:
    urls = console_urls(project)
    cmd = [
        "uv",
        "run",
        "--project",
        ".",
        "python",
        "packs/ingestion/primitives/setup/msgvault_setup.py",
        "setup",
        "--client-secret",
        "/path/to/client_secret.json",
    ]
    if email:
        cmd.extend(["--email", email])
    if app_name:
        cmd.extend(["--oauth-app", app_name])
    if home != DEFAULT_HOME.expanduser():
        cmd.extend(["--home", str(home)])
    return {
        "message": f"Create a Google OAuth Desktop app named {oauth_client_name}, download the client secret JSON, then rerun with --client-secret.",
        "urls": urls,
        "steps": [
            "Enable the Gmail API for the selected project.",
            "Configure the OAuth consent screen. Add yourself as a test user if the app is in testing.",
            "Add Gmail OAuth scopes: " + ", ".join(GMAIL_SCOPES) + ".",
            f"Create an OAuth client named {oauth_client_name} with application type Desktop app.",
            "Download the JSON file and pass it back with --client-secret.",
        ],
        "automation_note": "Workspace accounts may require an internal OAuth audience or adding the Gmail address as a test user before authorization.",
        "instructions_url": "packs/ingestion/primitives/setup/msgvault_setup.README.md",
        "expected_client_type": "Desktop app",
        "oauth_client_name": oauth_client_name,
        "continue_command": " ".join(cmd),
    }


def cmd_status(args: argparse.Namespace) -> int:
    emit(status_payload(expand(args.home)))
    return 0


def cmd_auth_check(args: argparse.Namespace) -> int:
    payload = check_accounts_payload(expand(args.home), args.email)
    emit(payload)
    if payload["status"] == "needs_user_action":
        return 20
    return 1 if payload["status"] == "error" else 0


def cmd_create_oauth_app(args: argparse.Namespace) -> int:
    home = expand(args.home)
    app_name = validate_oauth_app(args.oauth_app)
    gcloud = gcloud_context(args.project)
    api = {"status": "skipped", "reason": "not requested"}
    if args.enable_gmail_api:
        api = enable_gmail_api(gcloud.get("project") or args.project)
    action = build_user_action(gcloud.get("project") or args.project, args.email, app_name, home)
    opened = open_urls([action["urls"]["gmail_api"], action["urls"]["oauth_client"]]) if args.open_console else []
    emit(
        {
            "status": "needs_user_action",
            "message": action["message"],
            "home": str(home),
            "oauth_app": app_name or "default",
            "gcloud": gcloud,
            "gmail_api": api,
            "opened": opened,
            "action": action,
        }
    )
    return 20


def cmd_browser_setup(args: argparse.Namespace) -> int:
    home = expand(args.home)
    app_name = validate_oauth_app(args.oauth_app)
    project_name = args.project_name or DEFAULT_PROJECT_NAME
    requested_project = validate_project_id(args.project) if args.project else ""
    oauth_client_name = args.oauth_client_name or DEFAULT_OAUTH_CLIENT_NAME
    profile_dir = expand(args.profile_dir)
    download_dir = expand(args.download_dir)

    progress("Starting local message vault setup.")
    msgvault = ensure_msgvault(not args.no_install)
    if not msgvault["installed"]:
        emit({"status": "error", "message": "msgvault is not installed.", "msgvault": msgvault})
        return 1
    progress("msgvault is installed.")

    existing_config = configured_client_secret(home, app_name)
    if existing_config and existing_config.get("status") == "configured" and not args.force_browser_setup:
        progress("msgvault OAuth client is already configured.")
        db = init_db(home) if args.init_db else {"status": "skipped"}
        mcp = install_mcp() if args.install_mcp else {"status": "skipped"}
        account: dict[str, Any] | None = None
        if args.add_account and args.email:
            account = add_account(home, args.email, app_name, headless=args.headless, force=args.force_auth)
            if account["status"] != "ok":
                emit(
                    {
                        "status": "error",
                        "message": "msgvault account authorization failed.",
                        "home": str(home),
                        "oauth_app": app_name or "default",
                        "msgvault": msgvault,
                        "configured": existing_config,
                        "database": db,
                        "mcp": mcp,
                        "account": account,
                    }
                )
                return 1
        save_oauth_app_state(
            home,
            app_name,
            {
                "project_id": requested_project,
                "email": args.email,
                "oauth_client_name": oauth_client_name,
                "client_secret_path": existing_config["client_secret_path"],
                "client_id": existing_config["client_id"],
            },
        )
        emit(
            {
                "status": "ok",
                "message": "msgvault is already configured.",
                "home": str(home),
                "oauth_app": app_name or "default",
                "oauth_client_name": oauth_client_name,
                "msgvault": msgvault,
                "database": db,
                "mcp": mcp,
                "browser": {
                    "status": "skipped",
                    "reason": "valid client secret already configured",
                },
                "configured": existing_config,
                "account": account,
                "current": status_payload(home),
            }
        )
        return 0

    auth = ensure_gcloud_auth(
        open_browser=not getattr(args, "no_open_browser", False),
        expected_account=args.email,
    )
    if auth["status"] != "ok":
        emit({"status": "error", "message": "Google login failed.", "gcloud_auth": auth})
        return 1
    project_id, project_choice = choose_project_id(
        home,
        requested_project,
        args.email,
        str(auth.get("account") or ""),
        app_name,
    )
    progress(f"Using Google Cloud project {project_id} ({project_choice.get('source')}).")

    project = {"status": "skipped", "project": project_id}
    if args.create_project:
        project = create_gcloud_project(project_id, project_name)
        if project["status"] != "ok":
            emit({"status": "error", "message": "Google Cloud project creation failed.", "project": project})
            return 1
        # create_gcloud_project may have fallen back to a fresh id when the
        # deterministic one was globally reserved. Adopt the id that was really
        # created and pin it to state so every later step and re-run reuses it.
        created_project_id = validate_project_id(str(project.get("project") or "")) or project_id
        if created_project_id != project_id:
            project_id = created_project_id
            save_oauth_app_state(
                home,
                app_name,
                {"project_id": project_id, "email": args.email or auth.get("account", "")},
            )
    else:
        progress(f"Using Google Cloud project {project_id}.")
    selected_project = {
        "status": "skipped",
        "project": project_id,
        "reason": "using explicit project flags and console URLs",
    }
    api = enable_gmail_api(project_id) if args.enable_gmail_api else {"status": "skipped"}
    db = init_db(home) if args.init_db else {"status": "skipped"}
    mcp = install_mcp() if args.install_mcp else {"status": "skipped"}

    browser = run_browser_automation(
        project=project_id,
        email=args.email or auth.get("account", ""),
        oauth_client_name=oauth_client_name,
        profile_dir=profile_dir,
        download_dir=download_dir,
        timeout_seconds=args.timeout_seconds,
        audience=args.audience,
    )
    secret_path = browser.get("client_secret_path") or ""

    configured: dict[str, Any] | None = None
    account: dict[str, Any] | None = None
    if secret_path and Path(secret_path).exists():
        copied = copy_client_secret(Path(secret_path), home, app_name, copy_secret=not args.no_copy_client_secret)
        if copied["ok"]:
            write_msgvault_config(config_path(home), Path(copied["path"]), app_name)
            configured = {
                "status": "configured",
                "config": str(config_path(home)),
                "client_secret_path": copied["path"],
                "client_id": copied["client_id"],
            }
            if args.add_account and args.email:
                account = add_account(home, args.email, app_name, headless=args.headless, force=args.force_auth)
                if account["status"] != "ok":
                    emit(
                        {
                            "status": "error",
                            "message": "msgvault account authorization failed.",
                            "home": str(home),
                            "oauth_app": app_name or "default",
                            "oauth_client_name": oauth_client_name,
                            "project_choice": project_choice,
                            "project": project,
                            "selected_project": selected_project,
                            "gcloud_auth": auth,
                            "gmail_api": api,
                            "database": db,
                            "mcp": mcp,
                            "browser": browser,
                            "download_dir": str(download_dir),
                            "client_secret_path": secret_path,
                            "configured": configured,
                            "account": account,
                        }
                    )
                    return 1
        else:
            configured = {"status": "error", "message": copied["message"]}

    status = "ok" if configured and configured.get("status") == "configured" else "needs_user_action"
    message = "Google OAuth app created and msgvault configured." if status == "ok" else "Finish the Google OAuth app in the browser, then download the client secret JSON."
    if status == "ok":
        save_oauth_app_state(
            home,
            app_name,
            {
                "project_id": project_id,
                "email": args.email or auth.get("account", ""),
                "oauth_client_name": oauth_client_name,
                "client_secret_path": secret_path,
                "client_id": configured.get("client_id") if configured else "",
            },
        )
        progress("Local message vault setup finished.")
    else:
        save_oauth_app_state(
            home,
            app_name,
            {
                "project_id": project_id,
                "email": args.email or auth.get("account", ""),
                "oauth_client_name": oauth_client_name,
            },
        )
        progress("Local message vault setup needs the browser step to finish.")
    emit(
        {
            "status": status,
            "message": message,
            "home": str(home),
            "oauth_app": app_name or "default",
            "oauth_client_name": oauth_client_name,
            "project_choice": project_choice,
            "project": project,
            "selected_project": selected_project,
            "gcloud_auth": auth,
            "gmail_api": api,
            "database": db,
            "mcp": mcp,
            "browser": browser,
            "download_dir": str(download_dir),
            "client_secret_path": secret_path,
            "configured": configured,
            "account": account,
            "current": status_payload(home),
        }
    )
    return 0 if status == "ok" else 20


def cmd_configure(args: argparse.Namespace) -> int:
    home = expand(args.home)
    app_name = validate_oauth_app(args.oauth_app)
    source = expand(args.client_secret)
    copied = copy_client_secret(source, home, app_name, copy_secret=not args.no_copy_client_secret)
    if not copied["ok"]:
        emit({"status": "error", "message": copied["message"]})
        return 1
    write_msgvault_config(config_path(home), Path(copied["path"]), app_name)
    save_oauth_app_state(
        home,
        app_name,
        {
            "oauth_app": app_name or "default",
            "client_secret_path": copied["path"],
            "client_id": copied["client_id"],
        },
    )
    emit(
        {
            "status": "configured",
            "home": str(home),
            "config": str(config_path(home)),
            "oauth_app": app_name or "default",
            "client_secret_path": copied["path"],
            "client_id": copied["client_id"],
        }
    )
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    home = expand(args.home)
    app_name = validate_oauth_app(args.oauth_app)
    msgvault = ensure_msgvault(not args.no_install)
    if not msgvault["installed"]:
        emit({"status": "error", "message": "msgvault is not installed.", "msgvault": msgvault})
        return 1

    gcloud = gcloud_context(args.project)
    api = {"status": "skipped", "reason": "not requested"}
    if args.enable_gmail_api:
        api = enable_gmail_api(gcloud.get("project") or args.project)

    configured: dict[str, Any] | None = None
    if args.client_secret:
        source = expand(args.client_secret)
        copied = copy_client_secret(source, home, app_name, copy_secret=not args.no_copy_client_secret)
        if not copied["ok"]:
            emit({"status": "error", "message": copied["message"]})
            return 1
        write_msgvault_config(config_path(home), Path(copied["path"]), app_name)
        configured = {
            "status": "configured",
            "config": str(config_path(home)),
            "client_secret_path": copied["path"],
            "client_id": copied["client_id"],
        }
        save_oauth_app_state(
            home,
            app_name,
            {
                "project_id": args.project,
                "email": args.email,
                "oauth_app": app_name or "default",
                "client_secret_path": copied["path"],
                "client_id": copied["client_id"],
            },
        )

    db = init_db(home) if args.init_db else {"status": "skipped"}
    mcp = install_mcp() if args.install_mcp else {"status": "skipped"}

    account: dict[str, Any] | None = None
    if args.email:
        if not args.client_secret and not parse_client_secret_paths(config_path(home)):
            action = build_user_action(gcloud.get("project") or args.project, args.email, app_name, home)
            opened = open_urls([action["urls"]["gmail_api"], action["urls"]["oauth_client"]]) if args.open_console else []
            emit(
                {
                    "status": "needs_user_action",
                    "message": action["message"],
                    "home": str(home),
                    "oauth_app": app_name or "default",
                    "msgvault": msgvault,
                    "gcloud": gcloud,
                    "gmail_api": api,
                    "database": db,
                    "mcp": mcp,
                    "opened": opened,
                    "action": action,
                }
            )
            return 20
        account = add_account(home, args.email, app_name, headless=args.headless, force=args.force_auth)
        if account["status"] != "ok":
            emit(
                {
                    "status": "error",
                    "message": "msgvault account authorization failed.",
                    "home": str(home),
                    "oauth_app": app_name or "default",
                    "msgvault": msgvault,
                    "configured": configured,
                    "database": db,
                    "mcp": mcp,
                    "account": account,
                }
            )
            return 1
    elif not args.client_secret and not parse_client_secret_paths(config_path(home)):
        action = build_user_action(gcloud.get("project") or args.project, None, app_name, home)
        opened = open_urls([action["urls"]["gmail_api"], action["urls"]["oauth_client"]]) if args.open_console else []
        emit(
            {
                "status": "needs_user_action",
                "message": action["message"],
                "home": str(home),
                "oauth_app": app_name or "default",
                "msgvault": msgvault,
                "gcloud": gcloud,
                "gmail_api": api,
                "database": db,
                "mcp": mcp,
                "opened": opened,
                "action": action,
            }
        )
        return 20

    emit(
        {
            "status": "ok",
            "message": "msgvault is configured.",
            "home": str(home),
            "oauth_app": app_name or "default",
            "msgvault": msgvault,
            "gcloud": gcloud,
            "gmail_api": api,
            "configured": configured,
            "database": db,
            "mcp": mcp,
            "account": account,
            "current": status_payload(home),
        }
    )
    return 0


def cmd_add_account(args: argparse.Namespace) -> int:
    home = expand(args.home)
    app_name = validate_oauth_app(args.oauth_app)
    if not parse_client_secret_paths(config_path(home)):
        action = build_user_action(None, args.email, app_name, home)
        emit({"status": "needs_user_action", "message": action["message"], "action": action})
        return 20
    account = add_account(home, args.email, app_name, headless=args.headless, force=args.force_auth)
    emit({"status": account["status"], "home": str(home), "account": account})
    return 0 if account["status"] == "ok" else 1


def cmd_add_test_users(args: argparse.Namespace) -> int:
    home = expand(args.home)
    app_name = validate_oauth_app(args.oauth_app)
    requested_project = validate_project_id(args.project) if args.project else ""
    profile_dir = expand(args.profile_dir)
    download_dir = expand(args.download_dir)
    test_users = normalize_email_list([*(args.test_user or []), *(args.emails or [])])
    if not test_users:
        emit({"status": "error", "message": "Provide at least one OAuth test user email."})
        return 1

    auth = ensure_gcloud_auth(open_browser=not args.no_open_browser)
    if auth["status"] != "ok":
        emit({"status": "error", "message": "Google login failed.", "gcloud_auth": auth})
        return 1

    login_email = args.login_email or str(auth.get("account") or "")
    project_id, project_choice = choose_project_id(home, requested_project, login_email, login_email, app_name)
    progress(f"Using Google Cloud project {project_id} ({project_choice.get('source')}).")

    browser = run_browser_add_test_users(
        project=project_id,
        email=login_email,
        test_users=test_users,
        profile_dir=profile_dir,
        download_dir=download_dir,
        timeout_seconds=args.timeout_seconds,
        oauth_client_name=args.oauth_client_name,
    )
    browser_users = browser.get("test_users") if isinstance(browser.get("test_users"), dict) else {}
    missing = browser_users.get("missing") if isinstance(browser_users.get("missing"), list) else []
    status = "ok" if browser.get("status") == "ok" and not missing else browser.get("status", "error")
    if status == "ok":
        state = load_setup_state(home)
        app_state: dict[str, Any] = state
        if app_name:
            apps = state.get("oauth_apps") if isinstance(state.get("oauth_apps"), dict) else {}
            app_state = apps.get(app_name) if isinstance(apps.get(app_name), dict) else {}
        existing_users = normalize_email_list([*(app_state.get("test_users") or [])])
        saved_users = normalize_email_list([*existing_users, *test_users])
        save_oauth_app_state(
            home,
            app_name,
            {
                "project_id": project_id,
                "email": login_email,
                "oauth_client_name": args.oauth_client_name,
                "test_users": saved_users,
            },
        )
    emit(
        {
            "status": status,
            "message": "Google OAuth test users updated." if status == "ok" else "Google OAuth test user setup needs attention.",
            "home": str(home),
            "oauth_app": app_name or "default",
            "project": project_id,
            "project_choice": project_choice,
            "login_email": login_email,
            "test_users": test_users,
            "browser": browser,
        }
    )
    if status == "ok":
        return 0
    return 20 if browser.get("status") == "needs_user_action" else 1


def cmd_mcp_install(args: argparse.Namespace) -> int:
    emit(install_mcp())
    return 0


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--home", default=str(DEFAULT_HOME), help="msgvault home directory")


def add_setup_args(parser: argparse.ArgumentParser) -> None:
    add_common(parser)
    parser.add_argument("--email", default="", help="Gmail address to authorize after config exists")
    parser.add_argument("--project", default="", help="Google Cloud project ID")
    parser.add_argument("--client-secret", default="", help="Downloaded Google OAuth client_secret JSON")
    parser.add_argument("--oauth-app", default="", help="Named msgvault OAuth app for Workspace orgs")
    parser.add_argument("--headless", action="store_true", help="Use msgvault's headless OAuth instructions")
    parser.add_argument("--force-auth", action="store_true", help="Force a fresh msgvault OAuth token")
    parser.add_argument("--no-install", action="store_true", help="Do not install msgvault if it is missing")
    parser.add_argument("--no-init-db", dest="init_db", action="store_false", help="Skip msgvault init-db")
    parser.set_defaults(init_db=True)
    parser.add_argument("--no-install-mcp", dest="install_mcp", action="store_false", help="Skip Codex MCP registration")
    parser.set_defaults(install_mcp=True)
    parser.add_argument("--no-enable-gmail-api", dest="enable_gmail_api", action="store_false", help="Skip gcloud services enable")
    parser.set_defaults(enable_gmail_api=True)
    parser.add_argument("--no-open-console", dest="open_console", action="store_false", help="Do not open Google Console URLs")
    parser.set_defaults(open_console=True)
    parser.add_argument("--no-copy-client-secret", action="store_true", help="Reference the provided JSON path in config instead of copying it")


def add_browser_setup_args(parser: argparse.ArgumentParser) -> None:
    add_setup_args(parser)
    parser.add_argument("--project-name", default=DEFAULT_PROJECT_NAME, help="Google Cloud project display name")
    parser.add_argument("--oauth-client-name", default=DEFAULT_OAUTH_CLIENT_NAME, help="Google OAuth Desktop client name")
    parser.add_argument("--profile-dir", default=str(DEFAULT_BROWSER_PROFILE), help="Persistent Chrome profile for Google Console automation")
    parser.add_argument("--download-dir", default=str(DEFAULT_DOWNLOAD_DIR), help="Directory for downloaded client_secret JSON")
    parser.add_argument("--timeout-seconds", type=int, default=900, help="Browser automation timeout")
    parser.add_argument("--audience", choices=["external", "internal"], default="external", help="OAuth app audience")
    parser.add_argument("--no-open-browser", action="store_true", help="Do not open a browser for gcloud login")
    parser.add_argument("--no-create-project", dest="create_project", action="store_false", help="Use the provided/current project instead of creating one")
    parser.set_defaults(create_project=True)
    parser.add_argument("--add-account", action="store_true", help="Authorize the Gmail account after creating/configuring the OAuth client")
    parser.add_argument("--force-browser-setup", action="store_true", help="Run Google Console automation even when msgvault already has a valid client secret")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Set up msgvault Gmail OAuth and MCP access")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="Check local msgvault setup")
    add_common(status)
    status.set_defaults(func=cmd_status)

    auth_check = sub.add_parser("auth-check", help="Check Gmail OAuth health without downloading mail")
    add_common(auth_check)
    auth_check.add_argument("--email", action="append", required=True, help="Gmail address to check (repeatable)")
    auth_check.set_defaults(func=cmd_auth_check)

    setup = sub.add_parser("setup", help="Install/configure msgvault and optionally authorize an account")
    add_setup_args(setup)
    setup.set_defaults(func=cmd_setup)

    create = sub.add_parser("create-oauth-app", help="Open/print the Google OAuth app setup flow")
    add_common(create)
    create.add_argument("--email", default="", help="Gmail address for the continue command")
    create.add_argument("--project", default="", help="Google Cloud project ID")
    create.add_argument("--oauth-app", default="", help="Named msgvault OAuth app")
    create.add_argument("--no-enable-gmail-api", dest="enable_gmail_api", action="store_false")
    create.set_defaults(enable_gmail_api=True)
    create.add_argument("--no-open-console", dest="open_console", action="store_false")
    create.set_defaults(open_console=True)
    create.set_defaults(func=cmd_create_oauth_app)

    browser = sub.add_parser("browser-setup", help="Drive Google Console in Chrome to create the OAuth app")
    add_browser_setup_args(browser)
    browser.set_defaults(func=cmd_browser_setup)

    configure = sub.add_parser("configure", help="Store client_secret JSON in msgvault config")
    add_common(configure)
    configure.add_argument("--client-secret", required=True)
    configure.add_argument("--oauth-app", default="")
    configure.add_argument("--no-copy-client-secret", action="store_true")
    configure.set_defaults(func=cmd_configure)

    add = sub.add_parser("add-account", help="Authorize a Gmail account with msgvault")
    add_common(add)
    add.add_argument("--email", required=True)
    add.add_argument("--oauth-app", default="")
    add.add_argument("--headless", action="store_true")
    add.add_argument("--force-auth", action="store_true")
    add.set_defaults(func=cmd_add_account)

    test_users = sub.add_parser("add-test-users", help="Add OAuth test users through Google Console automation")
    add_common(test_users)
    test_users.add_argument("emails", nargs="*", help="OAuth test user email addresses")
    test_users.add_argument("--test-user", action="append", default=[], help="OAuth test user email address")
    test_users.add_argument("--login-email", default="", help="Google Console account to use")
    test_users.add_argument("--project", default="", help="Google Cloud project ID")
    test_users.add_argument("--oauth-app", default="", help="Named msgvault OAuth app")
    test_users.add_argument("--oauth-client-name", default=DEFAULT_OAUTH_CLIENT_NAME, help="Google OAuth Desktop client name")
    test_users.add_argument("--profile-dir", default=str(DEFAULT_BROWSER_PROFILE), help="Persistent Chrome profile for Google Console automation")
    test_users.add_argument("--download-dir", default=str(DEFAULT_DOWNLOAD_DIR), help="Directory for browser debug output")
    test_users.add_argument("--timeout-seconds", type=int, default=300, help="Browser automation timeout")
    test_users.add_argument("--no-open-browser", action="store_true", help="Do not open a browser for gcloud login")
    test_users.set_defaults(func=cmd_add_test_users)

    mcp = sub.add_parser("mcp-install", help="Install the msgvault MCP server in Codex")
    mcp.set_defaults(func=cmd_mcp_install)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ValueError as exc:
        emit({"status": "error", "message": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
