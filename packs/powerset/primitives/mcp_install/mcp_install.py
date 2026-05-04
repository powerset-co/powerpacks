#!/usr/bin/env python3
"""Install / status / remove the Powerset Search MCP for Claude Code or Codex.

The MCP server lives at https://search-api-7wk4uhe77q-uw.a.run.app/mcp and
exposes `expand_query`, `count`, `search`, `query_results`,
`sales_nav_resolve`, and `sales_nav_search` to any MCP-aware host on the
machine.

Authentication is a Bearer Auth0 access token (the same JWT cached at
`~/.powerpacks/credentials.json` by the `auth` primitive). The two hosts
handle that token differently:

- **Claude Code** (`claude mcp add ... --header "Authorization: Bearer <T>"`)
  bakes the token into config. Re-run `install --host claude` after token
  rotation.
- **Codex** (`codex mcp add --bearer-token-env-var <NAME>`) reads the token
  from the named env var at runtime. The user (or a launcher) just keeps
  `POWERPACKS_POWERSET_TOKEN` exported. Use `token-env` to print a fresh
  `export` line.

Stdlib-only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SELF_DIR = Path(__file__).resolve().parent
PACK_DIR = SELF_DIR.parent
AUTH = PACK_DIR / "auth" / "auth.py"

DEFAULT_NAME = os.environ.get("POWERPACKS_MCP_NAME", "powerset-search")
DEFAULT_URL = os.environ.get(
    "POWERPACKS_MCP_URL",
    "https://search-api-7wk4uhe77q-uw.a.run.app/mcp",
)
DEFAULT_CLAUDE_SCOPE = os.environ.get("POWERPACKS_MCP_SCOPE", "user")
DEFAULT_TOKEN_ENV_VAR = os.environ.get("POWERPACKS_TOKEN_ENV_VAR", "POWERPACKS_POWERSET_TOKEN")
DEFAULT_CREDENTIALS = Path(os.environ.get(
    "POWERPACKS_CREDENTIALS_PATH",
    str(Path.home() / ".powerpacks" / "credentials.json"),
))

EXPECTED_TOOLS = [
    "expand_query",
    "count",
    "search",
    "query_results",
    "sales_nav_resolve",
    "sales_nav_search",
]

SUPPORTED_HOSTS = ("claude", "codex")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def run(cmd: list[str], *, timeout: int = 30) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError as exc:
        return 127, "", f"command not found: {exc}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"


def fetch_bearer_token(
    credentials_path: Path,
    auth0_domain: str | None,
    client_id: str | None,
) -> tuple[str | None, str | None]:
    """Return (token, error). Auto-refreshes via auth.py if the cached token is expiring."""
    if not AUTH.exists():
        return None, f"missing auth primitive at {AUTH}"
    cmd = [sys.executable, str(AUTH), "token", "--bearer-only",
           "--credentials-path", str(credentials_path)]
    if auth0_domain:
        cmd.extend(["--auth0-domain", auth0_domain])
    if client_id:
        cmd.extend(["--client-id", client_id])
    code, out, err = run(cmd, timeout=15)
    if code != 0 or not out.strip():
        return None, (err or out or "auth.token failed").strip()
    return out.strip().splitlines()[0].strip(), None


def host_cli(host: str) -> str | None:
    return shutil.which(host)


# ---------------------------------------------------------------------------
# Per-host: register, status, remove
# ---------------------------------------------------------------------------

def claude_status(name: str) -> dict[str, Any]:
    if not host_cli("claude"):
        return {"installed": False, "host": "claude", "error": "claude CLI not on PATH"}
    code, out, err = run(["claude", "mcp", "get", name], timeout=15)
    if code != 0:
        return {
            "installed": False, "host": "claude",
            "error": (err or out or "not registered").strip()[:200],
        }
    return {"installed": True, "host": "claude", "details": out.strip()[:400]}


def claude_install(name: str, url: str, scope: str, token: str) -> dict[str, Any]:
    """Register the MCP in Claude Code. Token is baked into config via --header."""
    if not host_cli("claude"):
        return {"host": "claude", "ok": False, "error": "claude CLI not on PATH"}
    state = claude_status(name)
    replaced = state.get("installed", False)
    if replaced:
        run(["claude", "mcp", "remove", "--scope", scope, name], timeout=15)
    add_cmd = [
        "claude", "mcp", "add",
        "--scope", scope,
        "--transport", "http",
        name, url,
        "--header", f"Authorization: Bearer {token}",
    ]
    code, out, err = run(add_cmd, timeout=30)
    if code != 0:
        return {
            "host": "claude",
            "ok": False,
            "error": (err or out or "claude mcp add failed").strip()[:400],
            "command_line": [
                "Authorization: Bearer <REDACTED>" if a.startswith("Authorization:") else a
                for a in add_cmd
            ],
        }
    return {
        "host": "claude",
        "ok": True,
        "scope": scope,
        "url": url,
        "replaced_existing": replaced,
        "token_handling": "baked into config; re-install on rotation",
        "details": claude_status(name).get("details"),
    }


def claude_remove(name: str, scope: str) -> dict[str, Any]:
    if not host_cli("claude"):
        return {"host": "claude", "ok": False, "skipped": True, "reason": "claude CLI not on PATH"}
    state = claude_status(name)
    if not state.get("installed"):
        return {"host": "claude", "ok": True, "skipped": True, "reason": "not registered"}
    code, out, err = run(["claude", "mcp", "remove", "--scope", scope, name], timeout=15)
    return {"host": "claude", "ok": code == 0, "scope": scope,
            "error": (err or out or "").strip() if code != 0 else None}


def codex_status(name: str) -> dict[str, Any]:
    if not host_cli("codex"):
        return {"installed": False, "host": "codex", "error": "codex CLI not on PATH"}
    code, out, err = run(["codex", "mcp", "get", name], timeout=15)
    if code != 0:
        return {
            "installed": False, "host": "codex",
            "error": (err or out or "not registered").strip()[:200],
        }
    return {"installed": True, "host": "codex", "details": out.strip()[:400]}


def codex_install(name: str, url: str, token_env_var: str) -> dict[str, Any]:
    """Register the MCP in Codex. Token is read at runtime from `token_env_var`."""
    if not host_cli("codex"):
        return {"host": "codex", "ok": False, "error": "codex CLI not on PATH"}
    state = codex_status(name)
    replaced = state.get("installed", False)
    if replaced:
        run(["codex", "mcp", "remove", name], timeout=15)
    add_cmd = [
        "codex", "mcp", "add",
        name,
        "--url", url,
        "--bearer-token-env-var", token_env_var,
    ]
    code, out, err = run(add_cmd, timeout=30)
    if code != 0:
        return {
            "host": "codex", "ok": False,
            "error": (err or out or "codex mcp add failed").strip()[:400],
            "command_line": add_cmd,
        }
    return {
        "host": "codex",
        "ok": True,
        "url": url,
        "replaced_existing": replaced,
        "bearer_token_env_var": token_env_var,
        "token_handling": (
            f"read from ${token_env_var} at runtime; rotate by re-exporting,"
            f" no re-install needed"
        ),
        "details": codex_status(name).get("details"),
    }


def codex_remove(name: str) -> dict[str, Any]:
    if not host_cli("codex"):
        return {"host": "codex", "ok": False, "skipped": True, "reason": "codex CLI not on PATH"}
    state = codex_status(name)
    if not state.get("installed"):
        return {"host": "codex", "ok": True, "skipped": True, "reason": "not registered"}
    code, out, err = run(["codex", "mcp", "remove", name], timeout=15)
    return {"host": "codex", "ok": code == 0,
            "error": (err or out or "").strip() if code != 0 else None}


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def resolve_hosts(host: str) -> list[str]:
    if host == "all":
        return [h for h in SUPPORTED_HOSTS if host_cli(h)]
    if host not in SUPPORTED_HOSTS:
        raise SystemExit(f"unsupported --host {host}; expected one of {SUPPORTED_HOSTS} or 'all'")
    return [host]


def cmd_install(args: argparse.Namespace) -> int:
    hosts = resolve_hosts(args.host)
    if not hosts:
        emit({
            "primitive": "mcp_install",
            "command": "install",
            "status": "failed",
            "error": "no supported MCP host CLIs on PATH (claude, codex)",
            "fix_command": {
                "claude": "https://docs.claude.com/en/docs/claude-code/setup",
                "codex": "https://docs.openai.com/codex/cli",
            },
        })
        return 1

    token, err = fetch_bearer_token(args.credentials_path, args.auth0_domain, args.client_id)
    if not token:
        emit({
            "primitive": "mcp_install",
            "command": "install",
            "status": "failed",
            "error": f"could not get an Auth0 access token: {err}",
            "fix_command": f"python {AUTH} login",
        })
        return 1

    results: list[dict[str, Any]] = []
    for h in hosts:
        if h == "claude":
            results.append(claude_install(args.name, args.url, args.scope, token))
        elif h == "codex":
            results.append(codex_install(args.name, args.url, args.token_env_var))

    overall_ok = all(r.get("ok") for r in results)
    payload = {
        "primitive": "mcp_install",
        "command": "install",
        "status": "ok" if overall_ok else "partial",
        "name": args.name,
        "url": args.url,
        "expected_tools": EXPECTED_TOOLS,
        "token": {"length": len(token), "redacted": "***"},
        "hosts": results,
        "installed_at": now_iso(),
    }
    if any(r.get("host") == "codex" and r.get("ok") for r in results):
        payload["codex_post_install"] = {
            "env_var": args.token_env_var,
            "shell_export": _shell_export_line(args.token_env_var, token),
            "tip": (
                f"Codex reads ${args.token_env_var} when it spawns the MCP."
                f" Either export it in your shell rc or wrap codex in a launcher"
                f" that runs `eval $(python {SELF_DIR / 'mcp_install.py'} token-env)`"
                f" before exec'ing codex."
            ),
        }
    emit(payload)
    return 0 if overall_ok else 2


def cmd_status(args: argparse.Namespace) -> int:
    hosts = resolve_hosts(args.host)
    states: list[dict[str, Any]] = []
    for h in hosts:
        if h == "claude":
            states.append(claude_status(args.name))
        elif h == "codex":
            states.append(codex_status(args.name))
    any_installed = any(s.get("installed") for s in states)
    emit({
        "primitive": "mcp_install",
        "command": "status",
        "checked_at": now_iso(),
        "name": args.name,
        "url": args.url,
        "expected_tools": EXPECTED_TOOLS,
        "hosts": states,
    })
    return 0 if any_installed else 1


def cmd_remove(args: argparse.Namespace) -> int:
    hosts = resolve_hosts(args.host)
    results: list[dict[str, Any]] = []
    for h in hosts:
        if h == "claude":
            results.append(claude_remove(args.name, args.scope))
        elif h == "codex":
            results.append(codex_remove(args.name))
    overall_ok = all(r.get("ok") for r in results)
    emit({
        "primitive": "mcp_install",
        "command": "remove",
        "status": "ok" if overall_ok else "partial",
        "name": args.name,
        "hosts": results,
    })
    return 0 if overall_ok else 1


def _shell_export_line(env_var: str, token: str) -> str:
    # Quote safely for sh.
    safe = token.replace("'", "'\\''")
    return f"export {env_var}='{safe}'"


def cmd_token_env(args: argparse.Namespace) -> int:
    """Print a single `export <env_var>=<token>` line for `eval` in a launcher."""
    token, err = fetch_bearer_token(args.credentials_path, args.auth0_domain, args.client_id)
    if not token:
        # Print a comment to stderr so eval-ing this doesn't accidentally
        # nuke the user's env var with an empty value.
        print(f"# mcp_install token-env failed: {err}", file=sys.stderr)
        return 1
    print(_shell_export_line(args.token_env_var, token))
    return 0


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------

def add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--name", default=DEFAULT_NAME)
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--host", default="claude", choices=("claude", "codex", "all"),
                   help="Which MCP host CLI to target. 'all' = every CLI on PATH.")
    p.add_argument("--scope", default=DEFAULT_CLAUDE_SCOPE, choices=("user", "project", "local"),
                   help="Claude Code config scope (ignored by codex).")
    p.add_argument("--token-env-var", default=DEFAULT_TOKEN_ENV_VAR,
                   help="Codex env var that holds the bearer token (ignored by claude).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Install the Powerset Search MCP into Claude Code or Codex")
    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install", help="Add or replace the MCP server entry")
    add_common(install)
    install.add_argument("--credentials-path", type=Path, default=DEFAULT_CREDENTIALS)
    install.add_argument("--auth0-domain")
    install.add_argument("--client-id")
    install.set_defaults(func=cmd_install)

    status = sub.add_parser("status", help="Check whether the MCP server is registered on each host")
    add_common(status)
    status.set_defaults(func=cmd_status)

    remove = sub.add_parser("remove", help="Unregister the MCP server")
    add_common(remove)
    remove.set_defaults(func=cmd_remove)

    tokenenv = sub.add_parser(
        "token-env",
        help="Print a single `export POWERPACKS_POWERSET_TOKEN=...` line for shell eval (Codex launcher pattern)",
    )
    tokenenv.add_argument("--credentials-path", type=Path, default=DEFAULT_CREDENTIALS)
    tokenenv.add_argument("--auth0-domain")
    tokenenv.add_argument("--client-id")
    tokenenv.add_argument("--token-env-var", default=DEFAULT_TOKEN_ENV_VAR)
    tokenenv.set_defaults(func=cmd_token_env)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
