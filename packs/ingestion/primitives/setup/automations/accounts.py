"""Account authorization and OAuth health payloads for msgvault setup.

Owns the per-account surface: email normalization, `msgvault add-account`
authorization, reauthorization detection, the agent-facing authorize command
strings, the overall `status` payload (install/config/accounts/MCP/gcloud),
and the `auth-check` payload that verifies token health per account without
downloading mail.

The auth-check decision is `check_account` — one first-rule-wins verdict per
account — and `CHECK_BUCKETS` says which payload lists each verdict lands in.
`VaultHealth` parses the `status` payload once so the check never re-probes a
nested dict.

Changelog:
  2026-07-29 (setup style pass): extracted the auth-check decision. The loop
    used to inline the four verdicts and then re-derive five parallel lists and
    the overall status with five more comprehensions over the same payload
    dicts; verdicts are now the frozen `AccountCheck` and the buckets are a
    literal table. `VaultHealth.from_status` replaced the four
    `current.get("x", {}).get("y")` probes and the stored-account
    `email or identifier` chain. Same payload, same key set per verdict.
  2026-07-23 (audit):
    - Split out of the former 1,770-line setup/msgvault_setup.py.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import sys
from dataclasses import dataclass, field
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
    command_error,
    command_output,
    parse_json_fragment,
    progress,
    run_command,
    run_visible_command,
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
# Which auth-check payload lists each verdict lands in, in verdict order. A
# verdict that needs the user's browser also lands in accounts_to_authorize.
CHECK_BUCKETS: dict[str, tuple[str, ...]] = {
    "healthy": ("healthy_accounts",),
    "missing_token": ("missing_accounts", "accounts_to_authorize"),
    "reauthorization_required": ("expired_accounts", "accounts_to_authorize"),
    "transient_error": ("error_accounts",),
}
CHECK_LISTS = (
    "healthy_accounts",
    "missing_accounts",
    "expired_accounts",
    "accounts_to_authorize",
    "error_accounts",
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


@dataclass(frozen=True)
class AccountCheck:
    """One account's auth-check verdict, and the record the CLI emits for it.

    Optional fields are omitted from the record when empty, so a healthy
    account carries no error keys and a transient failure carries no
    authorize command (there is nothing for the user to re-authorize yet)."""

    email: str
    status: str
    error_code: str = ""
    error: str = ""
    authorize_command: str = ""
    network_called: bool = False

    def record(self) -> dict[str, Any]:
        """Return this verdict as its `accounts[]` entry."""
        entry: dict[str, Any] = {"email": self.email, "status": self.status}
        if self.error_code:
            entry["error_code"] = self.error_code
        if self.error:
            entry["error"] = self.error
        if self.authorize_command:
            entry["authorize_command"] = self.authorize_command
        return entry


@dataclass(frozen=True)
class VaultHealth:
    """The `status` payload parsed once at the auth-check boundary.

    Everything auth-check needs to know about the local vault: whether it can
    be probed at all, and which account identities msgvault already stores.
    msgvault has reported the identity as `email` and as `identifier` across
    versions, so both are read here and nowhere else."""

    installed: bool = False
    oauth_configured: bool = False
    database_exists: bool = False
    accounts_error: str = ""
    stored_emails: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_status(cls, payload: dict[str, Any]) -> VaultHealth:
        """Parse a `status_payload` result."""
        accounts = payload.get("accounts") or []
        return cls(
            installed=bool(payload.get("msgvault", {}).get("installed")),
            oauth_configured=bool(payload.get("config", {}).get("oauth_configured")),
            database_exists=bool(payload.get("database", {}).get("exists")),
            accounts_error=str(payload.get("accounts_error") or ""),
            stored_emails=frozenset(
                str(account.get("email") or account.get("identifier") or "").strip().lower()
                for account in accounts
                if isinstance(account, dict)
            ),
        )

    @property
    def blockers(self) -> list[str]:
        """Reasons no account can be checked at all, in report order."""
        reasons = []
        if not self.installed:
            reasons.append("msgvault is not installed")
        if not self.oauth_configured:
            reasons.append("msgvault OAuth is not configured")
        if not self.database_exists:
            reasons.append("msgvault database is missing")
        if self.accounts_error:
            reasons.append(self.accounts_error)
        return reasons


def check_account(home: Path, email: str, *, stored: bool) -> AccountCheck:
    """Return one account's verdict; first rule wins.

    Not stored by msgvault at all -> the token is missing. Otherwise `msgvault
    verify` decides: success is healthy, a revoked/expired token needs the user
    to re-authorize, and anything else is transient and not the user's problem
    to fix. Only the stored branch touches the network."""
    if not stored:
        return AccountCheck(
            email=email,
            status="missing_token",
            error_code="gmail_authorization_missing",
            authorize_command=msgvault_account_authorize_command(home, email, force=False),
        )
    result = run_msgvault(["verify", email, "--skip-db-check", "--sample", "0", "--local"], home, timeout=60)
    if result["ok"]:
        return AccountCheck(email=email, status="healthy", network_called=True)
    error = command_error(result)
    if msgvault_reauthorization_required(error):
        return AccountCheck(
            email=email,
            status="reauthorization_required",
            error_code="gmail_reauthorization_required",
            error=error,
            authorize_command=msgvault_account_authorize_command(home, email, force=True),
            network_called=True,
        )
    return AccountCheck(
        email=email,
        status="transient_error",
        error_code="gmail_auth_check_failed",
        error=error or f"msgvault verify exited with {result.get('returncode')}",
        network_called=True,
    )


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
            accounts_error = command_error(result)
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
    # The emails the user asked to authorize live in setup state as test_users
    # (saved by add-test-users). They're the source of truth for "accounts
    # available to authorize" — msgvault only knows who's already authorized.
    desired_emails = normalize_email_list([setup_state.email, *setup_state.test_users])
    return {
        "status": "ok" if ready else "needs_setup",
        "home": str(home),
        "owner_email": setup_state.email,
        "desired_emails": desired_emails,
        "msgvault": {
            "installed": bool(msgvault_path),
            "path": msgvault_path,
            "version": command_output(version),
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

    When the vault itself cannot be probed, every requested account is one
    transient failure carrying that reason; otherwise each account gets its
    own `check_account` verdict. Both paths render through the same buckets."""
    requested = normalize_email_list(requested_emails)
    health = VaultHealth.from_status(status_payload(home))
    blockers = health.blockers
    if blockers:
        unavailable = "; ".join(blockers)
        checks = [
            AccountCheck(
                email=email,
                status="transient_error",
                error_code="gmail_auth_check_unavailable",
                error=unavailable,
            )
            for email in requested
        ]
    else:
        checks = [
            check_account(home, email, stored=email.lower() in health.stored_emails)
            for email in requested
        ]

    buckets: dict[str, list[str]] = {name: [] for name in CHECK_LISTS}
    for check in checks:
        for bucket in CHECK_BUCKETS.get(check.status, ()):
            buckets[bucket].append(check.email)
    if buckets["error_accounts"]:
        status = "error"
    elif buckets["accounts_to_authorize"]:
        status = "needs_user_action"
    else:
        status = "ok"
    return {
        "status": status,
        "home": str(home),
        "requested_accounts": requested,
        **buckets,
        "accounts": [check.record() for check in checks],
        "browser_opened": False,
        "mail_downloaded": False,
        "network_called": any(check.network_called for check in checks),
    }
