"""Browser-driven command flows for msgvault setup.

Full flow logic for the `browser-setup` and `add-test-users` subcommands:
gcloud login, project choose/create, Gmail API, database/MCP prep, then the
Chrome automation (oauth_browser) and client-secret configuration. The CLI
entry unpacks argparse namespaces into these keyword-only calls and returns
their exit codes (0 ok, 1 error, 20 needs_user_action). Cross-module calls
are module-qualified so tests patch the defining submodule.

Changelog:
  2026-07-23 (audit):
    - Extracted from the former fat cmd_* bodies in setup/msgvault_setup.py
      (cmd_browser_setup was ~217 lines inside the entry).
  2026-07-23 (audit dedup): emit now imports from common.jsonio (was
    automations.shell.emit, deleted there as a jsonio dup).
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
from packs.ingestion.primitives.setup.automations.msgvault_home import (  # noqa: E402
    DEFAULT_PROJECT_NAME,
)
from packs.ingestion.primitives.setup.automations.oauth_browser import (  # noqa: E402
    DEFAULT_OAUTH_CLIENT_NAME,
)
from packs.ingestion.primitives.setup.automations.shell import progress  # noqa: E402
from packs.ingestion.primitives.common.jsonio import emit  # noqa: E402


def browser_setup_flow(
    *,
    home: Path,
    oauth_app: str,
    email: str,
    project: str,
    project_name: str,
    oauth_client_name: str,
    profile_dir: Path,
    download_dir: Path,
    timeout_seconds: int,
    audience: str,
    no_install: bool,
    init_db: bool,
    install_mcp: bool,
    enable_gmail_api: bool,
    create_project: bool,
    add_account: bool,
    headless: bool,
    force_auth: bool,
    force_browser_setup: bool,
    no_copy_client_secret: bool,
    no_open_browser: bool,
) -> int:
    """Create the Google OAuth app via Chrome automation and configure msgvault.

    Short-circuits when a valid client secret is already configured (unless
    force_browser_setup); otherwise runs gcloud login pinned to the email,
    chooses/creates the project, enables the Gmail API, and drives the
    browser automation, feeding the downloaded secret back into config."""
    app_name = msgvault_home.validate_oauth_app(oauth_app)
    project_name = project_name or DEFAULT_PROJECT_NAME
    requested_project = gcloud_project.validate_project_id(project) if project else ""
    oauth_client_name = oauth_client_name or DEFAULT_OAUTH_CLIENT_NAME

    progress("Starting local message vault setup.")
    msgvault = msgvault_home.ensure_msgvault(not no_install)
    if not msgvault["installed"]:
        emit({"status": "error", "message": "msgvault is not installed.", "msgvault": msgvault})
        return 1
    progress("msgvault is installed.")

    existing_config = msgvault_home.configured_client_secret(home, app_name)
    if existing_config and existing_config.get("status") == "configured" and not force_browser_setup:
        progress("msgvault OAuth client is already configured.")
        db = msgvault_home.init_db(home) if init_db else {"status": "skipped"}
        mcp_result = mcp.install_mcp() if install_mcp else {"status": "skipped"}
        account: dict[str, Any] | None = None
        if add_account and email:
            account = accounts.add_account(home, email, app_name, headless=headless, force=force_auth)
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
                        "mcp": mcp_result,
                        "account": account,
                    }
                )
                return 1
        msgvault_home.save_oauth_app_state(
            home,
            app_name,
            {
                "project_id": requested_project,
                "email": email,
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
                "mcp": mcp_result,
                "browser": {
                    "status": "skipped",
                    "reason": "valid client secret already configured",
                },
                "configured": existing_config,
                "account": account,
                "current": accounts.status_payload(home),
            }
        )
        return 0

    auth = gcloud_project.ensure_gcloud_auth(
        open_browser=not no_open_browser,
        expected_account=email,
    )
    if auth["status"] != "ok":
        emit({"status": "error", "message": "Google login failed.", "gcloud_auth": auth})
        return 1
    project_id, project_choice = gcloud_project.choose_project_id(
        home,
        requested_project,
        email,
        str(auth.get("account") or ""),
        app_name,
    )
    progress(f"Using Google Cloud project {project_id} ({project_choice.get('source')}).")

    project_result: dict[str, Any] = {"status": "skipped", "project": project_id}
    if create_project:
        project_result = gcloud_project.create_gcloud_project(project_id, project_name)
        if project_result["status"] != "ok":
            emit({"status": "error", "message": "Google Cloud project creation failed.", "project": project_result})
            return 1
        # create_gcloud_project may have fallen back to a fresh id when the
        # deterministic one was globally reserved. Adopt the id that was really
        # created and pin it to state so every later step and re-run reuses it.
        created_project_id = gcloud_project.validate_project_id(str(project_result.get("project") or "")) or project_id
        if created_project_id != project_id:
            project_id = created_project_id
            msgvault_home.save_oauth_app_state(
                home,
                app_name,
                {"project_id": project_id, "email": email or auth.get("account", "")},
            )
    else:
        progress(f"Using Google Cloud project {project_id}.")
    selected_project = {
        "status": "skipped",
        "project": project_id,
        "reason": "using explicit project flags and console URLs",
    }
    api = gcloud_project.enable_gmail_api(project_id) if enable_gmail_api else {"status": "skipped"}
    db = msgvault_home.init_db(home) if init_db else {"status": "skipped"}
    mcp_result = mcp.install_mcp() if install_mcp else {"status": "skipped"}

    browser = oauth_browser.run_browser_automation(
        project=project_id,
        email=email or auth.get("account", ""),
        oauth_client_name=oauth_client_name,
        profile_dir=profile_dir,
        download_dir=download_dir,
        timeout_seconds=timeout_seconds,
        audience=audience,
    )
    secret_path = browser.get("client_secret_path") or ""

    configured: dict[str, Any] | None = None
    account = None
    if secret_path and Path(secret_path).exists():
        copied = msgvault_home.copy_client_secret(Path(secret_path), home, app_name, copy_secret=not no_copy_client_secret)
        if copied["ok"]:
            msgvault_home.write_msgvault_config(msgvault_home.config_path(home), Path(copied["path"]), app_name)
            configured = {
                "status": "configured",
                "config": str(msgvault_home.config_path(home)),
                "client_secret_path": copied["path"],
                "client_id": copied["client_id"],
            }
            if add_account and email:
                account = accounts.add_account(home, email, app_name, headless=headless, force=force_auth)
                if account["status"] != "ok":
                    emit(
                        {
                            "status": "error",
                            "message": "msgvault account authorization failed.",
                            "home": str(home),
                            "oauth_app": app_name or "default",
                            "oauth_client_name": oauth_client_name,
                            "project_choice": project_choice,
                            "project": project_result,
                            "selected_project": selected_project,
                            "gcloud_auth": auth,
                            "gmail_api": api,
                            "database": db,
                            "mcp": mcp_result,
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
        msgvault_home.save_oauth_app_state(
            home,
            app_name,
            {
                "project_id": project_id,
                "email": email or auth.get("account", ""),
                "oauth_client_name": oauth_client_name,
                "client_secret_path": secret_path,
                "client_id": configured.get("client_id") if configured else "",
            },
        )
        progress("Local message vault setup finished.")
    else:
        msgvault_home.save_oauth_app_state(
            home,
            app_name,
            {
                "project_id": project_id,
                "email": email or auth.get("account", ""),
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
            "project": project_result,
            "selected_project": selected_project,
            "gcloud_auth": auth,
            "gmail_api": api,
            "database": db,
            "mcp": mcp_result,
            "browser": browser,
            "download_dir": str(download_dir),
            "client_secret_path": secret_path,
            "configured": configured,
            "account": account,
            "current": accounts.status_payload(home),
        }
    )
    return 0 if status == "ok" else 20


def add_test_users_flow(
    *,
    home: Path,
    oauth_app: str,
    project: str,
    emails: list[str],
    test_user: list[str],
    login_email: str,
    oauth_client_name: str,
    profile_dir: Path,
    download_dir: Path,
    timeout_seconds: int,
    no_open_browser: bool,
) -> int:
    """Add OAuth consent-screen test users via Chrome automation and save them to state."""
    app_name = msgvault_home.validate_oauth_app(oauth_app)
    requested_project = gcloud_project.validate_project_id(project) if project else ""
    test_users = accounts.normalize_email_list([*(test_user or []), *(emails or [])])
    if not test_users:
        emit({"status": "error", "message": "Provide at least one OAuth test user email."})
        return 1

    auth = gcloud_project.ensure_gcloud_auth(open_browser=not no_open_browser)
    if auth["status"] != "ok":
        emit({"status": "error", "message": "Google login failed.", "gcloud_auth": auth})
        return 1

    login_email = login_email or str(auth.get("account") or "")
    project_id, project_choice = gcloud_project.choose_project_id(home, requested_project, login_email, login_email, app_name)
    progress(f"Using Google Cloud project {project_id} ({project_choice.get('source')}).")

    browser = oauth_browser.run_browser_add_test_users(
        project=project_id,
        email=login_email,
        test_users=test_users,
        profile_dir=profile_dir,
        download_dir=download_dir,
        timeout_seconds=timeout_seconds,
        oauth_client_name=oauth_client_name,
    )
    browser_users = browser.get("test_users") if isinstance(browser.get("test_users"), dict) else {}
    missing = browser_users.get("missing") if isinstance(browser_users.get("missing"), list) else []
    status = "ok" if browser.get("status") == "ok" and not missing else browser.get("status", "error")
    if status == "ok":
        state = msgvault_home.load_setup_state(home)
        app_state: dict[str, Any] = state
        if app_name:
            apps = state.get("oauth_apps") if isinstance(state.get("oauth_apps"), dict) else {}
            app_state = apps.get(app_name) if isinstance(apps.get(app_name), dict) else {}
        existing_users = accounts.normalize_email_list([*(app_state.get("test_users") or [])])
        saved_users = accounts.normalize_email_list([*existing_users, *test_users])
        msgvault_home.save_oauth_app_state(
            home,
            app_name,
            {
                "project_id": project_id,
                "email": login_email,
                "oauth_client_name": oauth_client_name,
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
