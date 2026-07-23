"""Non-browser command flows for msgvault setup.

Full flow logic for the `setup`, `configure`, `create-oauth-app`, and
`add-account` subcommands; the CLI entry unpacks argparse namespaces into
these keyword-only calls and returns their exit codes. Flows emit their JSON
payload and return the process exit code (0 ok, 1 error, 20
needs_user_action). Cross-module calls are module-qualified so tests patch
the defining submodule.

Changelog:
  2026-07-23 (audit):
    - Extracted from the former fat cmd_* bodies in setup/msgvault_setup.py.
"""

from __future__ import annotations

import sys
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
from packs.ingestion.primitives.setup.automations.shell import (  # noqa: E402
    emit,
    expand,
)


def create_oauth_app_flow(
    *,
    home: Path,
    oauth_app: str,
    email: str,
    project: str,
    enable_gmail_api: bool,
    open_console: bool,
) -> int:
    """Emit the manual Google OAuth app instructions (always needs_user_action)."""
    app_name = msgvault_home.validate_oauth_app(oauth_app)
    gcloud = gcloud_project.gcloud_context(project)
    api: dict[str, Any] = {"status": "skipped", "reason": "not requested"}
    if enable_gmail_api:
        api = gcloud_project.enable_gmail_api(gcloud.get("project") or project)
    action = oauth_browser.build_user_action(gcloud.get("project") or project, email, app_name, home)
    opened = gcloud_project.open_urls([action["urls"]["gmail_api"], action["urls"]["oauth_client"]]) if open_console else []
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


def configure_flow(
    *,
    home: Path,
    oauth_app: str,
    client_secret: Path,
    no_copy_client_secret: bool,
) -> int:
    """Validate a downloaded client secret and store it in msgvault config."""
    app_name = msgvault_home.validate_oauth_app(oauth_app)
    copied = msgvault_home.copy_client_secret(client_secret, home, app_name, copy_secret=not no_copy_client_secret)
    if not copied["ok"]:
        emit({"status": "error", "message": copied["message"]})
        return 1
    msgvault_home.write_msgvault_config(msgvault_home.config_path(home), Path(copied["path"]), app_name)
    msgvault_home.save_oauth_app_state(
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
            "config": str(msgvault_home.config_path(home)),
            "oauth_app": app_name or "default",
            "client_secret_path": copied["path"],
            "client_id": copied["client_id"],
        }
    )
    return 0


def setup_flow(
    *,
    home: Path,
    oauth_app: str,
    email: str,
    project: str,
    client_secret: str,
    no_install: bool,
    no_copy_client_secret: bool,
    init_db: bool,
    install_mcp: bool,
    enable_gmail_api: bool,
    open_console: bool,
    headless: bool,
    force_auth: bool,
) -> int:
    """Install/configure msgvault and optionally authorize an account.

    Without a client secret (given or already configured) this stops with
    needs_user_action and the manual OAuth-app instructions."""
    app_name = msgvault_home.validate_oauth_app(oauth_app)
    msgvault = msgvault_home.ensure_msgvault(not no_install)
    if not msgvault["installed"]:
        emit({"status": "error", "message": "msgvault is not installed.", "msgvault": msgvault})
        return 1

    gcloud = gcloud_project.gcloud_context(project)
    api: dict[str, Any] = {"status": "skipped", "reason": "not requested"}
    if enable_gmail_api:
        api = gcloud_project.enable_gmail_api(gcloud.get("project") or project)

    configured: dict[str, Any] | None = None
    if client_secret:
        source = expand(client_secret)
        copied = msgvault_home.copy_client_secret(source, home, app_name, copy_secret=not no_copy_client_secret)
        if not copied["ok"]:
            emit({"status": "error", "message": copied["message"]})
            return 1
        msgvault_home.write_msgvault_config(msgvault_home.config_path(home), Path(copied["path"]), app_name)
        configured = {
            "status": "configured",
            "config": str(msgvault_home.config_path(home)),
            "client_secret_path": copied["path"],
            "client_id": copied["client_id"],
        }
        msgvault_home.save_oauth_app_state(
            home,
            app_name,
            {
                "project_id": project,
                "email": email,
                "oauth_app": app_name or "default",
                "client_secret_path": copied["path"],
                "client_id": copied["client_id"],
            },
        )

    db = msgvault_home.init_db(home) if init_db else {"status": "skipped"}
    mcp_result = mcp.install_mcp() if install_mcp else {"status": "skipped"}

    account: dict[str, Any] | None = None
    if email:
        if not client_secret and not msgvault_home.parse_client_secret_paths(msgvault_home.config_path(home)):
            action = oauth_browser.build_user_action(gcloud.get("project") or project, email, app_name, home)
            opened = gcloud_project.open_urls([action["urls"]["gmail_api"], action["urls"]["oauth_client"]]) if open_console else []
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
                    "mcp": mcp_result,
                    "opened": opened,
                    "action": action,
                }
            )
            return 20
        account = accounts.add_account(home, email, app_name, headless=headless, force=force_auth)
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
                    "mcp": mcp_result,
                    "account": account,
                }
            )
            return 1
    elif not client_secret and not msgvault_home.parse_client_secret_paths(msgvault_home.config_path(home)):
        action = oauth_browser.build_user_action(gcloud.get("project") or project, None, app_name, home)
        opened = gcloud_project.open_urls([action["urls"]["gmail_api"], action["urls"]["oauth_client"]]) if open_console else []
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
                "mcp": mcp_result,
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
            "mcp": mcp_result,
            "account": account,
            "current": accounts.status_payload(home),
        }
    )
    return 0


def add_account_flow(
    *,
    home: Path,
    oauth_app: str,
    email: str,
    headless: bool,
    force_auth: bool,
) -> int:
    """Authorize one Gmail account, or emit the OAuth-app instructions when unconfigured."""
    app_name = msgvault_home.validate_oauth_app(oauth_app)
    if not msgvault_home.parse_client_secret_paths(msgvault_home.config_path(home)):
        action = oauth_browser.build_user_action(None, email, app_name, home)
        emit({"status": "needs_user_action", "message": action["message"], "action": action})
        return 20
    account = accounts.add_account(home, email, app_name, headless=headless, force=force_auth)
    emit({"status": account["status"], "home": str(home), "account": account})
    return 0 if account["status"] == "ok" else 1
