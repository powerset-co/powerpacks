"""Account authorization and OAuth health payloads for msgvault setup.

Owns the per-account surface: email normalization, `msgvault add-account`
authorization, reauthorization detection, the agent-facing authorize command
strings, the overall `status` payload (install/config/accounts/MCP/gcloud),
and the `auth-check` payload that verifies token health per account without
downloading mail.

Changelog:
  2026-07-23 (audit):
    - Split out of the former 1,770-line setup/msgvault_setup.py.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode.
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.setup.automations.gcloud_project import (  # noqa: E402
    gcloud_context,
)
from packs.ingestion.primitives.setup.automations.mcp import (  # noqa: E402
    mcp_status,
)
from packs.ingestion.primitives.setup.automations.msgvault_home import (  # noqa: E402
    config_path,
    db_path,
    load_setup_state,
    parse_client_secret_paths,
    run_msgvault,
)
from packs.ingestion.primitives.setup.automations.shell import (  # noqa: E402
    parse_json_fragment,
    progress,
    run_command,
    run_visible_command,
    tail,
)


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MSGVAULT_REAUTH_ERROR_MARKERS = (
    "expired or revoked",
    "cannot re-authorize",
    "invalid_grant",
    "missing token",
    "no valid token",
    "token is missing",
)


def normalize_email_list(values: list[str]) -> list[str]:
    """Split, validate, and case-insensitively dedupe emails, preserving order."""
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
    """Return True when msgvault output means the account token needs re-auth."""
    haystack = (text or "").lower()
    return any(marker in haystack for marker in MSGVAULT_REAUTH_ERROR_MARKERS)


def msgvault_account_authorize_command(home: Path, email: str, *, force: bool) -> str:
    """Return the repo-root shell command that (re)authorizes one account."""
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


def add_account(home: Path, email: str, app_name: str, *, headless: bool, force: bool) -> dict[str, Any]:
    """Authorize a Gmail account with `msgvault add-account` (visible OAuth flow)."""
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


def status_payload(home: Path) -> dict[str, Any]:
    """Build the full `status` payload: binary, config, accounts, MCP, gcloud."""
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


def check_accounts_payload(home: Path, requested_emails: list[str]) -> dict[str, Any]:
    """Build the `auth-check` payload: per-account token health without mail sync.

    Accounts absent from msgvault are missing_token; `msgvault verify` failures
    split into reauthorization_required (token revoked/expired) versus
    transient_error, each carrying the exact authorize command to run."""
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
