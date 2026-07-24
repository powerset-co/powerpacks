#!/usr/bin/env python3
"""Set up msgvault for local Gmail archive access (thin CLI entry).

Guided setup for msgvault Gmail OAuth and Codex MCP registration. The Google
Cloud console still owns classic installed-app OAuth client creation for Gmail
scopes; this primitive automates the local pieces around that step:
install/status, Gmail API enabling, config.toml updates, account auth, and
Codex MCP registration. Flow logic lives in `setup/automations/`; this module
is argparse + cmd_* dispatch only.

Usage (run from the repo root):

    uv run --project . python packs/ingestion/primitives/setup/msgvault_setup.py status
    uv run --project . python packs/ingestion/primitives/setup/msgvault_setup.py browser-setup --email you@gmail.com
    uv run --project . python packs/ingestion/primitives/setup/msgvault_setup.py setup --email you@gmail.com
    uv run --project . python packs/ingestion/primitives/setup/msgvault_setup.py setup --client-secret ~/Downloads/client_secret.json --email you@gmail.com
    uv run --project . python packs/ingestion/primitives/setup/msgvault_setup.py create-oauth-app --email you@gmail.com
    uv run --project . python packs/ingestion/primitives/setup/msgvault_setup.py add-account --email you@gmail.com
    uv run --project . python packs/ingestion/primitives/setup/msgvault_setup.py add-test-users other@gmail.com
    uv run --project . python packs/ingestion/primitives/setup/msgvault_setup.py auth-check --email you@gmail.com
    uv run --project . python packs/ingestion/primitives/setup/msgvault_setup.py mcp-install

Secrets are stored only under `~/.msgvault/`: the primitive updates
`~/.msgvault/config.toml`, runs `msgvault init-db`, and can register the Codex
MCP server with `codex mcp add msgvault -- msgvault mcp`. `browser-setup`
opens Google Console in a persistent Chrome profile, lets the user finish
Google login/security screens, then attempts to create a project, enable the
Gmail API, configure the OAuth screen, create a Desktop OAuth client named
`local-msg-vault`, download the client secret JSON, and feed it back into
msgvault setup. Exit codes: 0 ok, 1 error, 20 needs_user_action.

Changelog:
  2026-07-23 (audit):
    - Decomposed the 1,770-line driver into setup/automations/ (shell,
      msgvault_home, gcloud_project, mcp, oauth_browser, accounts,
      setup_flows, browser_flows); this entry keeps the same path,
      subcommands, and flags. google_oauth_browser.js moved next to its
      driver in automations/.
    - Absorbed the former sidecar README into this docstring; the sidecar
      file is deleted per hygiene rules.
  2026-07-23 (audit dedup): emit now imports from common.jsonio (was
    automations.shell.emit, deleted there as a jsonio dup).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Repo-root bootstrap so `packs.*` imports work in module AND script mode.
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.setup.automations.accounts import (  # noqa: E402
    check_accounts_payload,
    status_payload,
)
from packs.ingestion.primitives.setup.automations.browser_flows import (  # noqa: E402
    add_test_users_flow,
    browser_setup_flow,
)
from packs.ingestion.primitives.setup.automations.mcp import (  # noqa: E402
    install_mcp,
)
from packs.ingestion.primitives.setup.automations.msgvault_home import (  # noqa: E402
    DEFAULT_HOME,
    DEFAULT_PROJECT_NAME,
)
from packs.ingestion.primitives.setup.automations.oauth_browser import (  # noqa: E402
    DEFAULT_BROWSER_PROFILE,
    DEFAULT_DOWNLOAD_DIR,
    DEFAULT_OAUTH_CLIENT_NAME,
)
from packs.ingestion.primitives.setup.automations.setup_flows import (  # noqa: E402
    add_account_flow,
    configure_flow,
    create_oauth_app_flow,
    setup_flow,
)
from packs.ingestion.primitives.setup.automations.shell import expand  # noqa: E402
from packs.ingestion.primitives.common.jsonio import emit  # noqa: E402


def cmd_status(args: argparse.Namespace) -> int:
    """Emit the local msgvault setup status payload."""
    emit(status_payload(expand(args.home)))
    return 0


def cmd_auth_check(args: argparse.Namespace) -> int:
    """Emit per-account Gmail OAuth health without downloading mail."""
    payload = check_accounts_payload(expand(args.home), args.email)
    emit(payload)
    if payload["status"] == "needs_user_action":
        return 20
    return 1 if payload["status"] == "error" else 0


def cmd_create_oauth_app(args: argparse.Namespace) -> int:
    """Run the manual Google OAuth app instructions flow."""
    return create_oauth_app_flow(
        home=expand(args.home),
        oauth_app=args.oauth_app,
        email=args.email,
        project=args.project,
        enable_gmail_api=args.enable_gmail_api,
        open_console=args.open_console,
    )


def cmd_browser_setup(args: argparse.Namespace) -> int:
    """Run the Chrome-driven Google OAuth app creation flow."""
    return browser_setup_flow(
        home=expand(args.home),
        oauth_app=args.oauth_app,
        email=args.email,
        project=args.project,
        project_name=args.project_name,
        oauth_client_name=args.oauth_client_name,
        profile_dir=expand(args.profile_dir),
        download_dir=expand(args.download_dir),
        timeout_seconds=args.timeout_seconds,
        audience=args.audience,
        no_install=args.no_install,
        init_db=args.init_db,
        install_mcp=args.install_mcp,
        enable_gmail_api=args.enable_gmail_api,
        create_project=args.create_project,
        add_account=args.add_account,
        headless=args.headless,
        force_auth=args.force_auth,
        force_browser_setup=args.force_browser_setup,
        no_copy_client_secret=args.no_copy_client_secret,
        no_open_browser=getattr(args, "no_open_browser", False),
    )


def cmd_configure(args: argparse.Namespace) -> int:
    """Store a downloaded client_secret JSON in msgvault config."""
    return configure_flow(
        home=expand(args.home),
        oauth_app=args.oauth_app,
        client_secret=expand(args.client_secret),
        no_copy_client_secret=args.no_copy_client_secret,
    )


def cmd_setup(args: argparse.Namespace) -> int:
    """Run the install/configure/authorize setup flow."""
    return setup_flow(
        home=expand(args.home),
        oauth_app=args.oauth_app,
        email=args.email,
        project=args.project,
        client_secret=args.client_secret,
        no_install=args.no_install,
        no_copy_client_secret=args.no_copy_client_secret,
        init_db=args.init_db,
        install_mcp=args.install_mcp,
        enable_gmail_api=args.enable_gmail_api,
        open_console=args.open_console,
        headless=args.headless,
        force_auth=args.force_auth,
    )


def cmd_add_account(args: argparse.Namespace) -> int:
    """Authorize a Gmail account with msgvault."""
    return add_account_flow(
        home=expand(args.home),
        oauth_app=args.oauth_app,
        email=args.email,
        headless=args.headless,
        force_auth=args.force_auth,
    )


def cmd_add_test_users(args: argparse.Namespace) -> int:
    """Add OAuth consent-screen test users through Google Console automation."""
    return add_test_users_flow(
        home=expand(args.home),
        oauth_app=args.oauth_app,
        project=args.project,
        emails=args.emails,
        test_user=args.test_user,
        login_email=args.login_email,
        oauth_client_name=args.oauth_client_name,
        profile_dir=expand(args.profile_dir),
        download_dir=expand(args.download_dir),
        timeout_seconds=args.timeout_seconds,
        no_open_browser=args.no_open_browser,
    )


def cmd_mcp_install(args: argparse.Namespace) -> int:
    """Install the msgvault MCP server in Codex."""
    emit(install_mcp())
    return 0


def add_common(parser: argparse.ArgumentParser) -> None:
    """Add the shared --home flag."""
    parser.add_argument("--home", default=str(DEFAULT_HOME), help="msgvault home directory")


def add_setup_args(parser: argparse.ArgumentParser) -> None:
    """Add the flags shared by the setup and browser-setup subcommands."""
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
    """Add the browser-setup-only automation flags."""
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
    """Build the msgvault setup CLI parser with all subcommands."""
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
    """Parse arguments and dispatch to the selected cmd_* handler."""
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ValueError as exc:
        emit({"status": "error", "message": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
