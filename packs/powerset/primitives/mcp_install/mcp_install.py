#!/usr/bin/env python3
"""Install / status / remove the Powerset Search MCP for Claude Code or Codex.

The MCP server URL is supplied explicitly through `POWERPACKS_MCP_URL` or
`--url`. It exposes `expand_query`, `list_sets`, `count`, `search`, `query_results`,
`sales_nav_resolve`, `sales_nav_search`, and artifact/extended lead tools to
any MCP-aware host on the machine.

Authentication is a Bearer Auth0 access token (the same JWT cached at
`~/.powerpacks/credentials.json` by the `auth` primitive). The two hosts
store that token in their MCP config:

- **Claude Code** (`claude mcp add ... --header "Authorization: Bearer <T>"`)
  bakes the token into config. Re-run `install --host claude` after token
  rotation.
- **Codex** is registered with the HTTP URL, then this primitive writes
  `[mcp_servers.<name>.http_headers] Authorization = "Bearer <T>"` into
  `~/.codex/config.toml`, matching Codex's first-party config shape. Re-run
  `install --host codex` to refresh the token in config.

Stdlib-only.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SELF_DIR = Path(__file__).resolve().parent
PACK_DIR = SELF_DIR.parent
AUTH = PACK_DIR / "auth" / "auth.py"

DEFAULT_NAME = os.environ.get("POWERPACKS_MCP_NAME", "powerset-search")
# Trailing slash matters: Cloud Run + FastAPI mount returns 307 from
# `/mcp` -> `/mcp/`, and many MCP host clients (Claude Code, Codex) do not
# re-POST after a 307. Always register with `/mcp/` so initialization is a
# single round-trip.
DEFAULT_URL = os.environ.get("POWERPACKS_MCP_URL")
DEFAULT_CLAUDE_SCOPE = os.environ.get("POWERPACKS_MCP_SCOPE", "user")
DEFAULT_TOKEN_ENV_VAR = os.environ.get("POWERPACKS_TOKEN_ENV_VAR", "POWERPACKS_POWERSET_TOKEN")
DEFAULT_CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
DEFAULT_CREDENTIALS = Path(os.environ.get(
    "POWERPACKS_CREDENTIALS_PATH",
    str(Path.home() / ".powerpacks" / "credentials.json"),
))

EXPECTED_TOOLS = [
    "expand_query",
    "list_sets",
    "list_conversations",
    "create_set",
    "create_set_invite",
    "delete_set",
    "count",
    "search",
    "query_results",
    "sales_nav_resolve",
    "sales_nav_search",
    "query_extended_leads",
    "enrich_extended_profiles",
    "score_extended_leads",
    "sales_nav_resolve_member_ids",
    "find_mutuals",
    "get_artifact",
]

SUPPORTED_HOSTS = ("claude", "codex")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))



def missing_mcp_url_message() -> str:
    return (
        "missing required Powerset MCP config: set POWERPACKS_MCP_URL or pass --url. "
        "Copy packs/powerset/templates/env.powerset.example to .env for Powerset-hosted use."
    )


def require_mcp_url(value: str | None) -> str:
    if value:
        return value
    raise SystemExit(missing_mcp_url_message())

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
    state: dict[str, Any] = {"installed": True, "host": "codex", "details": out.strip()[:400]}
    token_state = codex_bearer_token_state(name)
    if token_state:
        state.update(token_state)
    return state


def toml_string(value: str) -> str:
    return json.dumps(value)


def codex_config_path() -> Path:
    return DEFAULT_CODEX_HOME / "config.toml"


def jwt_payload(token: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def codex_bearer_token_state(name: str) -> dict[str, Any] | None:
    config_path = codex_config_path()
    if not config_path.exists():
        return {"auth_status": "missing_config"}
    text = config_path.read_text()
    header_name = f"mcp_servers.{name}.http_headers"
    in_header = False
    auth_value: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        match = re.match(r"^\[([^\]]+)\]\s*$", line)
        if match:
            in_header = match.group(1) == header_name
            continue
        if in_header and line.startswith("Authorization"):
            _, _, value = line.partition("=")
            value = value.strip()
            try:
                auth_value = json.loads(value)
            except json.JSONDecodeError:
                auth_value = value.strip("\"'")
            break
    if not auth_value:
        return {"auth_status": "missing_authorization_header"}
    if not auth_value.startswith("Bearer "):
        return {"auth_status": "non_bearer_authorization_header"}
    token = auth_value.removeprefix("Bearer ").strip()
    payload = jwt_payload(token)
    if not payload:
        return {"auth_status": "unparseable_bearer_token"}
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return {"auth_status": "bearer_token_without_exp"}
    seconds_remaining = int(exp - time.time())
    return {
        "auth_status": "expired" if seconds_remaining <= 0 else "valid",
        "token_expired": seconds_remaining <= 0,
        "token_seconds_remaining": max(0, seconds_remaining),
        "token_expires_at": datetime.fromtimestamp(exp, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


def remove_toml_sections(text: str, section_names: set[str]) -> str:
    out: list[str] = []
    skip = False
    header_re = re.compile(r"^\[([^\]]+)\]\s*$")
    for line in text.splitlines():
        match = header_re.match(line.strip())
        if match:
            skip = match.group(1) in section_names
        if not skip:
            out.append(line)
    return "\n".join(out).rstrip() + ("\n" if out else "")


def write_codex_bearer_header(name: str, url: str, token: str) -> Path:
    config_path = codex_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing = config_path.read_text() if config_path.exists() else ""
    section = f"mcp_servers.{name}"
    header_section = f"{section}.http_headers"
    existing = remove_toml_sections(existing, {section, header_section})
    addition = (
        f"\n[{section}]\n"
        f"url = {toml_string(url)}\n\n"
        f"[{header_section}]\n"
        f"Authorization = {toml_string('Bearer ' + token)}\n"
    )
    config_path.write_text(existing.rstrip() + addition)
    return config_path


def codex_install(name: str, url: str, token: str) -> dict[str, Any]:
    """Register the MCP in Codex with a bearer Authorization header in config."""
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
    ]
    code, out, err = run(add_cmd, timeout=30)
    if code != 0:
        return {
            "host": "codex", "ok": False,
            "error": (err or out or "codex mcp add failed").strip()[:400],
            "command_line": add_cmd,
        }
    config_path = write_codex_bearer_header(name, url, token)
    return {
        "host": "codex",
        "ok": True,
        "url": url,
        "replaced_existing": replaced,
        "config_path": str(config_path),
        "auth_header": "Authorization: Bearer <redacted>",
        "token_handling": "baked into Codex config; re-run install to refresh",
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
    try:
        args.url = require_mcp_url(args.url)
    except SystemExit as exc:
        emit({
            "primitive": "mcp_install",
            "command": "install",
            "status": "failed",
            "error": str(exc),
        })
        return 2

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
            results.append(codex_install(args.name, args.url, token))

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
                   help="Legacy token-env output variable name; ignored by install/status/remove.")


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
        help="Print a single `export POWERPACKS_POWERSET_TOKEN=...` line for manual shell use",
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
