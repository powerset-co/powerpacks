"""Browser-driven command flows for msgvault setup.

The `browser-setup` and `add-test-users` subcommands, one frozen request class
each: gcloud login, project choose/create, Gmail API, database/MCP prep, then
the Chrome automation (oauth_browser) and client-secret configuration.
`from_args` is the argparse boundary — it expands paths, validates the OAuth
app name and project id, and resolves the automation defaults — so `run()`
reads typed attributes. `run()` returns its JSON payload; the CLI entry emits
it and maps its status to an exit code. Cross-module calls are
module-qualified so tests patch the defining submodule.

Changelog:
  2026-07-29 (setup style pass):
    - The two keyword-only `*_flow` functions became frozen request classes
      (`browser_setup_flow` took twenty keyword arguments unpacked from a
      Namespace and then re-derived four of them at the top of its body).
      Flows return their payload instead of emitting it and returning an exit
      code.
    - DELETED the dead `missing` guard in the test-user flow: `status` read
      `"ok" if browser ok and not missing else browser.get("status", "error")`,
      whose two branches return the same value in every case, so a browser run
      that reported missing test users has always been reported as ok. Behavior
      is preserved exactly; treating missing users as not-ok would be a
      deliberate behavior change and is left to a follow-up.
  2026-07-23 (audit):
    - Extracted from the former fat cmd_* bodies in setup/msgvault_setup.py
      (cmd_browser_setup was ~217 lines inside the entry).
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
from packs.ingestion.primitives.setup.automations.msgvault_home import (  # noqa: E402
    DEFAULT_PROJECT_NAME,
)
from packs.ingestion.primitives.setup.automations.oauth_browser import (  # noqa: E402
    DEFAULT_OAUTH_CLIENT_NAME,
)
from packs.ingestion.primitives.setup.automations.shell import expand, progress  # noqa: E402


@dataclass(frozen=True)
class BrowserSetup:
    """`browser-setup`: create the Google OAuth app in Chrome, configure msgvault.

    Short-circuits when a valid client secret is already configured (unless
    force_browser_setup); otherwise runs gcloud login pinned to the email,
    chooses/creates the project, enables the Gmail API, and drives the browser
    automation, feeding the downloaded secret back into config."""

    home: Path
    app_name: str
    email: str
    requested_project: str
    project_name: str
    oauth_client_name: str
    profile_dir: Path
    download_dir: Path
    timeout_seconds: int
    audience: str
    install: bool
    init_db: bool
    install_mcp: bool
    enable_gmail_api: bool
    create_project: bool
    add_account: bool
    headless: bool
    force_auth: bool
    force_browser_setup: bool
    copy_client_secret: bool
    open_browser: bool

    @classmethod
    def from_args(cls, args: Namespace) -> BrowserSetup:
        return cls(
            home=expand(args.home),
            app_name=msgvault_home.validate_oauth_app(args.oauth_app),
            email=args.email,
            requested_project=gcloud_project.validate_project_id(args.project),
            project_name=args.project_name or DEFAULT_PROJECT_NAME,
            oauth_client_name=args.oauth_client_name or DEFAULT_OAUTH_CLIENT_NAME,
            profile_dir=expand(args.profile_dir),
            download_dir=expand(args.download_dir),
            timeout_seconds=args.timeout_seconds,
            audience=args.audience,
            install=not args.no_install,
            init_db=args.init_db,
            install_mcp=args.install_mcp,
            enable_gmail_api=args.enable_gmail_api,
            create_project=args.create_project,
            add_account=args.add_account,
            headless=args.headless,
            force_auth=args.force_auth,
            force_browser_setup=args.force_browser_setup,
            copy_client_secret=not args.no_copy_client_secret,
            open_browser=not args.no_open_browser,
        )

    def authorize(self) -> dict[str, Any] | None:
        """Authorize the Gmail account when asked to, else None."""
        if not (self.add_account and self.email):
            return None
        return accounts.add_account(
            self.home, self.email, self.app_name, headless=self.headless, force=self.force_auth
        )

    def already_configured(self, msgvault: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
        """Finish without touching Chrome: msgvault already has a valid client secret."""
        progress("msgvault OAuth client is already configured.")
        db = msgvault_home.init_db(self.home) if self.init_db else {"status": "skipped"}
        mcp_result = mcp.install_mcp() if self.install_mcp else {"status": "skipped"}
        account = self.authorize()
        if account is not None and account["status"] != "ok":
            return {
                "status": "error",
                "message": "msgvault account authorization failed.",
                "home": str(self.home),
                "oauth_app": self.app_name or "default",
                "msgvault": msgvault,
                "configured": existing,
                "database": db,
                "mcp": mcp_result,
                "account": account,
            }
        msgvault_home.save_oauth_app_state(
            self.home,
            self.app_name,
            {
                "project_id": self.requested_project,
                "email": self.email,
                "oauth_client_name": self.oauth_client_name,
                "client_secret_path": existing["client_secret_path"],
                "client_id": existing["client_id"],
            },
        )
        return {
            "status": "ok",
            "message": "msgvault is already configured.",
            "home": str(self.home),
            "oauth_app": self.app_name or "default",
            "oauth_client_name": self.oauth_client_name,
            "msgvault": msgvault,
            "database": db,
            "mcp": mcp_result,
            "browser": {
                "status": "skipped",
                "reason": "valid client secret already configured",
            },
            "configured": existing,
            "account": account,
            "current": accounts.status_payload(self.home),
        }

    def run(self) -> dict[str, Any]:
        progress("Starting local message vault setup.")
        msgvault = msgvault_home.ensure_msgvault(self.install)
        if not msgvault["installed"]:
            return {"status": "error", "message": "msgvault is not installed.", "msgvault": msgvault}
        progress("msgvault is installed.")

        existing_config = msgvault_home.configured_client_secret(self.home, self.app_name)
        if existing_config and existing_config["status"] == "configured" and not self.force_browser_setup:
            return self.already_configured(msgvault, existing_config)

        auth = gcloud_project.ensure_gcloud_auth(
            open_browser=self.open_browser,
            expected_account=self.email,
        )
        if auth["status"] != "ok":
            return {"status": "error", "message": "Google login failed.", "gcloud_auth": auth}
        account_email = self.email or auth.get("account", "")
        project_id, project_choice = gcloud_project.choose_project_id(
            self.home,
            self.requested_project,
            self.email,
            str(auth.get("account") or ""),
            self.app_name,
        )
        progress(f"Using Google Cloud project {project_id} ({project_choice.get('source')}).")

        project_result: dict[str, Any] = {"status": "skipped", "project": project_id}
        if self.create_project:
            project_result = gcloud_project.create_gcloud_project(project_id, self.project_name)
            if project_result["status"] != "ok":
                return {
                    "status": "error",
                    "message": "Google Cloud project creation failed.",
                    "project": project_result,
                }
            # create_gcloud_project may have fallen back to a fresh id when the
            # deterministic one was globally reserved. Adopt the id that was really
            # created and pin it to state so every later step and re-run reuses it.
            created_project_id = gcloud_project.validate_project_id(str(project_result.get("project") or "")) or project_id
            if created_project_id != project_id:
                project_id = created_project_id
                msgvault_home.save_oauth_app_state(
                    self.home,
                    self.app_name,
                    {"project_id": project_id, "email": account_email},
                )
        else:
            progress(f"Using Google Cloud project {project_id}.")
        api = gcloud_project.enable_gmail_api(project_id) if self.enable_gmail_api else {"status": "skipped"}
        db = msgvault_home.init_db(self.home) if self.init_db else {"status": "skipped"}
        mcp_result = mcp.install_mcp() if self.install_mcp else {"status": "skipped"}

        browser = oauth_browser.run_browser_automation(
            project=project_id,
            email=account_email,
            oauth_client_name=self.oauth_client_name,
            profile_dir=self.profile_dir,
            download_dir=self.download_dir,
            timeout_seconds=self.timeout_seconds,
            audience=self.audience,
        )
        secret_path = browser.get("client_secret_path") or ""

        configured: dict[str, Any] | None = None
        account = None
        if secret_path and Path(secret_path).exists():
            copied = msgvault_home.copy_client_secret(
                Path(secret_path), self.home, self.app_name, copy_secret=self.copy_client_secret
            )
            if copied["ok"]:
                msgvault_home.write_msgvault_config(
                    msgvault_home.config_path(self.home), Path(copied["path"]), self.app_name
                )
                configured = {
                    "status": "configured",
                    "config": str(msgvault_home.config_path(self.home)),
                    "client_secret_path": copied["path"],
                    "client_id": copied["client_id"],
                }
                account = self.authorize()
            else:
                configured = {"status": "error", "message": copied["message"]}

        run_report = {
            "home": str(self.home),
            "oauth_app": self.app_name or "default",
            "oauth_client_name": self.oauth_client_name,
            "project_choice": project_choice,
            "project": project_result,
            # `gcloud config set project` is never run: every step below passes
            # --project explicitly and the console URLs carry the project too.
            "selected_project": {
                "status": "skipped",
                "project": project_id,
                "reason": "using explicit project flags and console URLs",
            },
            "gcloud_auth": auth,
            "gmail_api": api,
            "database": db,
            "mcp": mcp_result,
            "browser": browser,
            "download_dir": str(self.download_dir),
            "client_secret_path": secret_path,
            "configured": configured,
            "account": account,
        }
        if account is not None and account["status"] != "ok":
            return {
                **run_report,
                "status": "error",
                "message": "msgvault account authorization failed.",
            }

        if configured and configured["status"] == "configured":
            status, message = "ok", "Google OAuth app created and msgvault configured."
        else:
            status = "needs_user_action"
            message = "Finish the Google OAuth app in the browser, then download the client secret JSON."
        saved_state = {
            "project_id": project_id,
            "email": account_email,
            "oauth_client_name": self.oauth_client_name,
        }
        if status == "ok":
            saved_state["client_secret_path"] = secret_path
            saved_state["client_id"] = configured["client_id"] if configured else ""
        msgvault_home.save_oauth_app_state(self.home, self.app_name, saved_state)
        progress(
            "Local message vault setup finished."
            if status == "ok"
            else "Local message vault setup needs the browser step to finish."
        )
        return {
            **run_report,
            "status": status,
            "message": message,
            "current": accounts.status_payload(self.home),
        }


@dataclass(frozen=True)
class TestUsers:
    """`add-test-users`: add OAuth consent-screen test users through Chrome."""

    home: Path
    app_name: str
    requested_project: str
    test_users: tuple[str, ...]
    login_email: str
    oauth_client_name: str
    profile_dir: Path
    download_dir: Path
    timeout_seconds: int
    open_browser: bool

    @classmethod
    def from_args(cls, args: Namespace) -> TestUsers:
        return cls(
            home=expand(args.home),
            app_name=msgvault_home.validate_oauth_app(args.oauth_app),
            requested_project=gcloud_project.validate_project_id(args.project),
            test_users=tuple(accounts.normalize_email_list([*args.test_user, *args.emails])),
            login_email=args.login_email,
            oauth_client_name=args.oauth_client_name,
            profile_dir=expand(args.profile_dir),
            download_dir=expand(args.download_dir),
            timeout_seconds=args.timeout_seconds,
            open_browser=not args.no_open_browser,
        )

    def save_users(self, project_id: str, login_email: str) -> None:
        """Merge the newly added test users into this app's setup state."""
        existing_users = accounts.normalize_email_list(
            list(msgvault_home.load_setup_state(self.home, self.app_name).test_users)
        )
        msgvault_home.save_oauth_app_state(
            self.home,
            self.app_name,
            {
                "project_id": project_id,
                "email": login_email,
                "oauth_client_name": self.oauth_client_name,
                "test_users": accounts.normalize_email_list([*existing_users, *self.test_users]),
            },
        )

    def run(self) -> dict[str, Any]:
        if not self.test_users:
            return {"status": "error", "message": "Provide at least one OAuth test user email."}

        auth = gcloud_project.ensure_gcloud_auth(open_browser=self.open_browser)
        if auth["status"] != "ok":
            return {"status": "error", "message": "Google login failed.", "gcloud_auth": auth}

        login_email = self.login_email or str(auth.get("account") or "")
        project_id, project_choice = gcloud_project.choose_project_id(
            self.home, self.requested_project, login_email, login_email, self.app_name
        )
        progress(f"Using Google Cloud project {project_id} ({project_choice.get('source')}).")

        browser = oauth_browser.run_browser_add_test_users(
            project=project_id,
            email=login_email,
            test_users=list(self.test_users),
            profile_dir=self.profile_dir,
            download_dir=self.download_dir,
            timeout_seconds=self.timeout_seconds,
            oauth_client_name=self.oauth_client_name,
        )
        status = browser.get("status", "error")
        if status == "ok":
            self.save_users(project_id, login_email)
        return {
            "status": status,
            "message": "Google OAuth test users updated." if status == "ok" else "Google OAuth test user setup needs attention.",
            "home": str(self.home),
            "oauth_app": self.app_name or "default",
            "project": project_id,
            "project_choice": project_choice,
            "login_email": login_email,
            "test_users": list(self.test_users),
            "browser": browser,
        }
