#!/usr/bin/env python3
"""Set up msgvault for local Gmail archive access (thin CLI entry).

Guided setup for msgvault Gmail OAuth and Codex MCP registration. The Google
Cloud console still owns classic installed-app OAuth client creation for Gmail
scopes; this primitive automates the local pieces around that step:
install/status, Gmail API enabling, config.toml updates, account auth, and
Codex MCP registration. Flow logic lives in `setup/automations/`; this module
is argparse plus the exit-code policy.

Flow: parse argv -> build the subcommand's frozen request from the namespace
(the one place paths are expanded and names validated) -> run it -> emit its
JSON payload -> map payload status to an exit code.

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
  2026-07-29 (setup style pass): dropped the `cmd_*(args)` dispatchers and the
    `set_defaults(func=...)` indirection; `main` builds the subcommand's
    request and calls it inline. Flows now return their payload, so emission
    and the status-to-exit-code decision happen once, here, in EXIT_CODES.
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

from packs.ingestion.primitives.setup.automations import accounts, mcp  # noqa: E402
from packs.ingestion.primitives.setup.automations.browser_flows import (  # noqa: E402
    BrowserSetup,
    TestUsers,
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
    AccountAuthorization,
    ClientSecretConfig,
    MsgvaultSetup,
    OAuthAppInstructions,
)
from packs.ingestion.primitives.setup.automations.shell import expand  # noqa: E402
from packs.ingestion.primitives.common.jsonio import emit  # noqa: E402


# Exit-code policy, first rule wins. `status` and `mcp-install` are probes: they
# report what they found and always exit 0. Every other subcommand maps its
# payload status; anything unrecognized is a failure.
PROBE_COMMANDS = frozenset({"status", "mcp-install"})
EXIT_CODES = {
    "ok": 0,
    "configured": 0,
    "needs_user_action": 20,
}
EXIT_UNRECOGNIZED = 1


def exit_code(command: str, status: str) -> int:
    """Map a subcommand's payload status to the process exit code."""
    if command in PROBE_COMMANDS:
        return 0
    return EXIT_CODES.get(status, EXIT_UNRECOGNIZED)


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

    auth_check = sub.add_parser("auth-check", help="Check Gmail OAuth health without downloading mail")
    add_common(auth_check)
    auth_check.add_argument("--email", action="append", required=True, help="Gmail address to check (repeatable)")

    setup = sub.add_parser("setup", help="Install/configure msgvault and optionally authorize an account")
    add_setup_args(setup)

    create = sub.add_parser("create-oauth-app", help="Open/print the Google OAuth app setup flow")
    add_common(create)
    create.add_argument("--email", default="", help="Gmail address for the continue command")
    create.add_argument("--project", default="", help="Google Cloud project ID")
    create.add_argument("--oauth-app", default="", help="Named msgvault OAuth app")
    create.add_argument("--no-enable-gmail-api", dest="enable_gmail_api", action="store_false")
    create.set_defaults(enable_gmail_api=True)
    create.add_argument("--no-open-console", dest="open_console", action="store_false")
    create.set_defaults(open_console=True)

    browser = sub.add_parser("browser-setup", help="Drive Google Console in Chrome to create the OAuth app")
    add_browser_setup_args(browser)

    configure = sub.add_parser("configure", help="Store client_secret JSON in msgvault config")
    add_common(configure)
    configure.add_argument("--client-secret", required=True)
    configure.add_argument("--oauth-app", default="")
    configure.add_argument("--no-copy-client-secret", action="store_true")

    add = sub.add_parser("add-account", help="Authorize a Gmail account with msgvault")
    add_common(add)
    add.add_argument("--email", required=True)
    add.add_argument("--oauth-app", default="")
    add.add_argument("--headless", action="store_true")
    add.add_argument("--force-auth", action="store_true")

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

    sub.add_parser("mcp-install", help="Install the msgvault MCP server in Codex")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the selected subcommand, emit its payload."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "status":
            payload = accounts.status_payload(expand(args.home))
        elif args.command == "auth-check":
            payload = accounts.check_accounts_payload(expand(args.home), args.email)
        elif args.command == "create-oauth-app":
            payload = OAuthAppInstructions.from_args(args).run()
        elif args.command == "browser-setup":
            payload = BrowserSetup.from_args(args).run()
        elif args.command == "configure":
            payload = ClientSecretConfig.from_args(args).run()
        elif args.command == "setup":
            payload = MsgvaultSetup.from_args(args).run()
        elif args.command == "add-account":
            payload = AccountAuthorization.from_args(args).run()
        elif args.command == "add-test-users":
            payload = TestUsers.from_args(args).run()
        elif args.command == "mcp-install":
            payload = mcp.install_mcp()
        else:  # unreachable: argparse subcommands are required
            raise ValueError(f"unknown command: {args.command}")
    except ValueError as exc:
        emit({"status": "error", "message": str(exc)})
        return 1
    emit(payload)
    return exit_code(args.command, payload["status"])


if __name__ == "__main__":
    raise SystemExit(main())
