"""gcloud auth and project orchestration for msgvault setup automation.

Owns the Google Cloud side of setup: reading gcloud config values, detecting
reauth and project-id-taken errors, login enforcement (optionally pinned to an
expected account), project id validation, the choose-then-create project flow
(argument > saved state > current gcloud project > existing local-msg-vault
project > deterministic default), Gmail API enablement, and Google Console
URL building/opening.

Changelog:
  2026-07-23 (audit):
    - Split out of the former 1,770-line setup/msgvault_setup.py.
    - Setup-state persistence used by choose_project_id lives in
      msgvault_home.py.
"""

from __future__ import annotations

import platform
import re
import shutil
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

# Repo-root bootstrap so `packs.*` imports work in module AND script mode.
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.setup.automations.msgvault_home import (  # noqa: E402
    DEFAULT_PROJECT_NAME,
    default_project_id,
    load_setup_state,
    local_msg_vault_projects,
    save_oauth_app_state,
    setup_state_path,
)
from packs.ingestion.primitives.setup.automations.shell import (  # noqa: E402
    progress,
    run_command,
    run_visible_command,
    tail,
)


GMAIL_SERVICE = "gmail.googleapis.com"
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]
PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")


def gcloud_value(args: list[str]) -> str:
    """Return a gcloud config value, mapping failures and "(unset)" to ""."""
    result = run_command(["gcloud", *args], timeout=20)
    if not result["ok"]:
        return ""
    value = result["stdout"].strip()
    return "" if value == "(unset)" else value


def is_gcloud_reauth_error(text: str) -> bool:
    """Return True when gcloud output means the auth token needs a fresh login."""
    haystack = (text or "").lower()
    return (
        "problem refreshing your current auth tokens" in haystack
        or "reauthentication failed" in haystack
        or "cannot prompt during non-interactive execution" in haystack
        or "please run:" in haystack and "gcloud auth login" in haystack
    )


def is_project_taken_error(text: str) -> bool:
    """Return True when gcloud says the requested project id is globally in use."""
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
    """Return gcloud install state plus the active account and project."""
    gcloud_path = shutil.which("gcloud") or ""
    active_project = project or gcloud_value(["config", "get-value", "project", "--quiet"])
    account = gcloud_value(["config", "get-value", "account", "--quiet"]) if gcloud_path else ""
    return {
        "installed": bool(gcloud_path),
        "path": gcloud_path,
        "account": account,
        "project": active_project or "",
    }


def validate_project_id(project_id: str) -> str:
    """Return the project id when valid ("" passes through), raising ValueError otherwise."""
    if not project_id:
        return ""
    if not PROJECT_ID_RE.match(project_id):
        raise ValueError(
            "--project must be 6-30 chars, start with a lowercase letter, "
            "and contain only lowercase letters, numbers, or dashes"
        )
    return project_id


def ensure_gcloud_auth(open_browser: bool, expected_account: str = "") -> dict[str, Any]:
    """Ensure a working gcloud login, re-running `gcloud auth login` when stale.

    When expected_account is set, a login under any other account is an
    error — msgvault setup must run against the mailbox owner's project."""
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
    """Return True when the active account can describe the project."""
    if not project_id or not shutil.which("gcloud"):
        return False
    result = run_command(["gcloud", "projects", "describe", project_id, "--format=json"], timeout=60)
    return result["ok"]


def choose_project_id(home: Path, requested_project: str, email: str, account: str, app_name: str = "") -> tuple[str, dict[str, Any]]:
    """Pick the project id for this setup and record how it was chosen.

    Precedence: explicit argument > saved setup state > deterministic default
    when the target email differs from the gcloud account > current gcloud
    project when it is local-msg-vault-* > newest existing local-msg-vault
    project > deterministic default seeded by email/account. Every non-argument
    choice is pinned into setup state so re-runs stay stable."""
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


def create_gcloud_project(
    project_id: str,
    project_name: str,
    *,
    allow_fallback: bool = True,
    max_attempts: int = 4,
) -> dict[str, Any]:
    """Create (or adopt) a gcloud project, retrying with fresh ids when taken."""
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
    """Set the active gcloud project via `gcloud config set project`."""
    if not project_id:
        return {"status": "skipped", "reason": "missing project"}
    progress(f"Setting active Google Cloud project to {project_id}...")
    result = run_command(["gcloud", "config", "set", "project", project_id, "--quiet"], timeout=30)
    if result["ok"]:
        progress(f"Active Google Cloud project set to {project_id}.")
        return {"status": "ok", "project": project_id}
    return {"status": "error", "project": project_id, "message": tail(result.get("stderr") or result.get("stdout") or "")}


def enable_gmail_api(project: str | None) -> dict[str, Any]:
    """Enable the Gmail API service for the project via gcloud."""
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


def console_urls(project: str | None) -> dict[str, str]:
    """Return Google Console URLs for the Gmail API, consent screen, and credentials."""
    suffix = f"?project={quote(project)}" if project else ""
    return {
        "gmail_api": f"https://console.cloud.google.com/apis/library/{GMAIL_SERVICE}{suffix}",
        "oauth_consent": f"https://console.cloud.google.com/auth/overview{suffix}",
        "credentials": f"https://console.cloud.google.com/apis/credentials{suffix}",
        "oauth_client": f"https://console.cloud.google.com/apis/credentials/oauthclient{suffix}",
    }


def open_urls(urls: list[str]) -> list[str]:
    """Open URLs with the platform opener, returning the ones that opened."""
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
