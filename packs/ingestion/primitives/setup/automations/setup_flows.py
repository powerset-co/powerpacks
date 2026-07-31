"""Non-browser command flows for msgvault setup.

The `setup`, `configure`, `create-oauth-app`, and `add-account` subcommands,
one frozen request class each. `from_args` is the argparse boundary: it
expands paths, validates the OAuth app name, and turns the `--no-*` flags into
positive values, so `run()` reads typed attributes and never re-parses. `run()`
returns its JSON payload; the CLI entry emits it and maps its status to an exit
code. Cross-module calls are module-qualified so tests patch the defining
submodule.

Changelog:
  2026-07-29 (setup style pass): the four keyword-only `*_flow` functions
    became frozen request classes constructed once at the CLI boundary
    (`setup_flow` alone took thirteen keyword arguments unpacked from a
    Namespace, then re-derived `app_name`, the client-secret Path, and the
    negated flags inside the flow). Flows return their payload instead of
    emitting it and returning an exit code; status-to-exit-code is now one
    table in the entry.
  2026-07-23 (audit):
    - Extracted from the former fat cmd_* bodies in setup/msgvault_setup.py.
  2026-07-23 (audit dedup): emit now imports from common.jsonio (was
    automations.shell.emit, deleted there as a jsonio dup).
"""

from __future__ import annotations

import sys
from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode.
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.setup.automations import (  # noqa: E402
    accounts,
    gcloud_project,
    mcp,
    msgvault_home,
    oauth_browser,
)
from packs.ingestion.primitives.setup.automations.shell import expand  # noqa: E402

def skipped_not_requested() -> dict[str, Any]:
    """A FRESH skipped marker per payload — a shared module-level dict embedded
    in emitted payloads is an aliasing footgun (one caller's mutation would
    silently edit every payload holding it)."""
    return {"status": "skipped", "reason": "not requested"}


def open_console_urls(action: dict[str, Any], open_console: bool) -> list[str]:
    """Open the Gmail API and OAuth client console pages for a manual action."""
    if not open_console:
        return []
    return gcloud_project.open_urls([action["urls"]["gmail_api"], action["urls"]["oauth_client"]])


@dataclass(frozen=True)
class OAuthAppInstructions:
    """`create-oauth-app`: print the manual Google OAuth app steps.

    Always needs_user_action — the Google Cloud console owns installed-app
    OAuth client creation, so this command can only hand the user the URLs."""

    home: Path
    app_name: str
    email: str
    project: str
    enable_gmail_api: bool
    open_console: bool

    @classmethod
    def from_args(cls, args: Namespace) -> OAuthAppInstructions:
        return cls(
            home=expand(args.home),
            app_name=msgvault_home.validate_oauth_app(args.oauth_app),
            email=args.email,
            project=args.project,
            enable_gmail_api=args.enable_gmail_api,
            open_console=args.open_console,
        )

    def run(self) -> dict[str, Any]:
        gcloud = gcloud_project.gcloud_context(self.project)
        project = gcloud["project"] or self.project
        api = gcloud_project.enable_gmail_api(project) if self.enable_gmail_api else skipped_not_requested()
        action = oauth_browser.build_user_action(project, self.email, self.app_name, self.home)
        return {
            "status": "needs_user_action",
            "message": action["message"],
            "home": str(self.home),
            "oauth_app": self.app_name or "default",
            "gcloud": gcloud,
            "gmail_api": api,
            "opened": open_console_urls(action, self.open_console),
            "action": action,
        }


@dataclass(frozen=True)
class ClientSecretConfig:
    """`configure`: validate a downloaded client secret and store it in config."""

    home: Path
    app_name: str
    client_secret: Path
    copy_client_secret: bool

    @classmethod
    def from_args(cls, args: Namespace) -> ClientSecretConfig:
        return cls(
            home=expand(args.home),
            app_name=msgvault_home.validate_oauth_app(args.oauth_app),
            client_secret=expand(args.client_secret),
            copy_client_secret=not args.no_copy_client_secret,
        )

    def run(self) -> dict[str, Any]:
        copied = msgvault_home.copy_client_secret(
            self.client_secret, self.home, self.app_name, copy_secret=self.copy_client_secret
        )
        if not copied["ok"]:
            return {"status": "error", "message": copied["message"]}
        msgvault_home.write_msgvault_config(
            msgvault_home.config_path(self.home), Path(copied["path"]), self.app_name
        )
        msgvault_home.save_oauth_app_state(
            self.home,
            self.app_name,
            {
                "oauth_app": self.app_name or "default",
                "client_secret_path": copied["path"],
                "client_id": copied["client_id"],
            },
        )
        return {
            "status": "configured",
            "home": str(self.home),
            "config": str(msgvault_home.config_path(self.home)),
            "oauth_app": self.app_name or "default",
            "client_secret_path": copied["path"],
            "client_id": copied["client_id"],
        }


@dataclass(frozen=True)
class MsgvaultSetup:
    """`setup`: install/configure msgvault and optionally authorize an account.

    Without a client secret (given or already configured) this stops with
    needs_user_action and the manual OAuth-app instructions."""

    home: Path
    app_name: str
    email: str
    project: str
    client_secret: Path | None
    install: bool
    copy_client_secret: bool
    init_db: bool
    install_mcp: bool
    enable_gmail_api: bool
    open_console: bool
    headless: bool
    force_auth: bool

    @classmethod
    def from_args(cls, args: Namespace) -> MsgvaultSetup:
        return cls(
            home=expand(args.home),
            app_name=msgvault_home.validate_oauth_app(args.oauth_app),
            email=args.email,
            project=args.project,
            client_secret=expand(args.client_secret) if args.client_secret else None,
            install=not args.no_install,
            copy_client_secret=not args.no_copy_client_secret,
            init_db=args.init_db,
            install_mcp=args.install_mcp,
            enable_gmail_api=args.enable_gmail_api,
            open_console=args.open_console,
            headless=args.headless,
            force_auth=args.force_auth,
        )

    def needs_oauth_app(self) -> bool:
        """True when no client secret was given and none is configured yet."""
        if self.client_secret:
            return False
        return not msgvault_home.parse_client_secret_paths(msgvault_home.config_path(self.home))

    def run(self) -> dict[str, Any]:
        msgvault = msgvault_home.ensure_msgvault(self.install)
        if not msgvault["installed"]:
            return {"status": "error", "message": "msgvault is not installed.", "msgvault": msgvault}

        gcloud = gcloud_project.gcloud_context(self.project)
        api = (
            gcloud_project.enable_gmail_api(gcloud["project"] or self.project)
            if self.enable_gmail_api
            else skipped_not_requested()
        )

        configured: dict[str, Any] | None = None
        if self.client_secret:
            copied = msgvault_home.copy_client_secret(
                self.client_secret, self.home, self.app_name, copy_secret=self.copy_client_secret
            )
            if not copied["ok"]:
                return {"status": "error", "message": copied["message"]}
            msgvault_home.write_msgvault_config(
                msgvault_home.config_path(self.home), Path(copied["path"]), self.app_name
            )
            configured = {
                "status": "configured",
                "config": str(msgvault_home.config_path(self.home)),
                "client_secret_path": copied["path"],
                "client_id": copied["client_id"],
            }
            msgvault_home.save_oauth_app_state(
                self.home,
                self.app_name,
                {
                    "project_id": self.project,
                    "email": self.email,
                    "oauth_app": self.app_name or "default",
                    "client_secret_path": copied["path"],
                    "client_id": copied["client_id"],
                },
            )

        db = msgvault_home.init_db(self.home) if self.init_db else {"status": "skipped"}
        mcp_result = mcp.install_mcp() if self.install_mcp else {"status": "skipped"}

        if self.needs_oauth_app():
            action = oauth_browser.build_user_action(
                gcloud["project"] or self.project, self.email or None, self.app_name, self.home
            )
            return {
                "status": "needs_user_action",
                "message": action["message"],
                "home": str(self.home),
                "oauth_app": self.app_name or "default",
                "msgvault": msgvault,
                "gcloud": gcloud,
                "gmail_api": api,
                "database": db,
                "mcp": mcp_result,
                "opened": open_console_urls(action, self.open_console),
                "action": action,
            }

        account: dict[str, Any] | None = None
        if self.email:
            account = accounts.add_account(
                self.home, self.email, self.app_name, headless=self.headless, force=self.force_auth
            )
            if account["status"] != "ok":
                return {
                    "status": "error",
                    "message": "msgvault account authorization failed.",
                    "home": str(self.home),
                    "oauth_app": self.app_name or "default",
                    "msgvault": msgvault,
                    "configured": configured,
                    "database": db,
                    "mcp": mcp_result,
                    "account": account,
                }

        return {
            "status": "ok",
            "message": "msgvault is configured.",
            "home": str(self.home),
            "oauth_app": self.app_name or "default",
            "msgvault": msgvault,
            "gcloud": gcloud,
            "gmail_api": api,
            "configured": configured,
            "database": db,
            "mcp": mcp_result,
            "account": account,
            "current": accounts.status_payload(self.home),
        }


@dataclass(frozen=True)
class AccountAuthorization:
    """`add-account`: authorize one Gmail account with msgvault."""

    home: Path
    app_name: str
    email: str
    headless: bool
    force_auth: bool

    @classmethod
    def from_args(cls, args: Namespace) -> AccountAuthorization:
        return cls(
            home=expand(args.home),
            app_name=msgvault_home.validate_oauth_app(args.oauth_app),
            email=args.email,
            headless=args.headless,
            force_auth=args.force_auth,
        )

    def run(self) -> dict[str, Any]:
        if not msgvault_home.parse_client_secret_paths(msgvault_home.config_path(self.home)):
            action = oauth_browser.build_user_action(None, self.email, self.app_name, self.home)
            return {"status": "needs_user_action", "message": action["message"], "action": action}
        account = accounts.add_account(
            self.home, self.email, self.app_name, headless=self.headless, force=self.force_auth
        )
        return {"status": account["status"], "home": str(self.home), "account": account}
