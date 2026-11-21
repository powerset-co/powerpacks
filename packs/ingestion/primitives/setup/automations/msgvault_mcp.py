"""Give the person's own Codex and Claude Code the msgvault MCP server.

The app's chat already has it (desktop `codex.rs`). This registers the same local server,
`msgvault --home <home> mcp`, in the agents the person runs themselves, so "search my inbox" or
"draft a reply" works there too. Each agent asks before a tool that changes anything, by its own
rules; msgvault never sends mail.

- Codex: a `[mcp_servers.msgvault]` table in `~/.codex/config.toml`, when that folder exists.
- Claude Code: `claude mcp add --scope user`, when the `claude` command is installed.

An entry that already works is left alone, whoever wrote it. One whose command is gone (the app
moved) is replaced.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from packs.ingestion.primitives.setup.automations.shell import run_command

SERVER_NAME = "msgvault"
_TABLE = f"mcp_servers.{SERVER_NAME}"
_HEADER_RE = re.compile(r"^\[([^\]]+)\]\s*$")
_COMMAND_RE = re.compile(r'^command\s*=\s*(".*")\s*$')


def server_command(home: Path) -> list[str]:
    """The command an agent starts the server with; empty when msgvault is not installed."""
    binary = shutil.which("msgvault")
    return [binary, "--home", str(home), "mcp"] if binary else []


def _works(command: str | None) -> bool:
    return bool(command) and shutil.which(command) is not None


def _codex_tables(text: str) -> tuple[str, str | None]:
    """The config without the msgvault tables, and the command the msgvault table names."""
    kept: list[str] = []
    command: str | None = None
    table = ""
    for line in text.splitlines():
        header = _HEADER_RE.match(line.strip())
        if header:
            table = header.group(1)
        if table != _TABLE and not table.startswith(_TABLE + "."):
            kept.append(line)
        elif table == _TABLE and (found := _COMMAND_RE.match(line.strip())):
            command = json.loads(found.group(1))
    return "\n".join(kept).rstrip(), command


def register_codex(command: list[str], config: Path) -> bool:
    """Write the server into Codex's config. False when Codex is not set up or already has it."""
    if not config.parent.is_dir():
        return False
    text = config.read_text() if config.exists() else ""
    rest, current = _codex_tables(text)
    if _works(current):
        return False
    table = f"[{_TABLE}]\ncommand = {json.dumps(command[0])}\nargs = {json.dumps(command[1:])}\n"
    config.write_text(f"{rest}\n\n{table}" if rest else table)
    return True


def _claude_command(settings: Path) -> str | None:
    """The command Claude Code's own settings name for the server, read without starting it."""
    try:
        servers = json.loads(settings.read_text()).get("mcpServers") or {}
    except (OSError, ValueError):
        return None
    entry = servers.get(SERVER_NAME)
    return entry.get("command") if isinstance(entry, dict) else None


def register_claude(command: list[str], settings: Path) -> bool:
    """Add the server to Claude Code for every project. False when absent or already there."""
    if not shutil.which("claude"):
        return False
    current = _claude_command(settings)
    if _works(current):
        return False
    if current is not None:
        run_command(["claude", "mcp", "remove", "--scope", "user", SERVER_NAME], timeout=30)
    added = run_command(["claude", "mcp", "add", "--scope", "user", SERVER_NAME, "--", *command], timeout=30)
    return added.ok


def register_msgvault_mcp(home: Path) -> list[str]:
    """Register the server in each agent the person has; the agents that gained it."""
    command = server_command(home)
    if not command or not (home / "msgvault.db").exists():
        return []
    gained = []
    try:
        if register_codex(command, Path.home() / ".codex" / "config.toml"):
            gained.append("codex")
        if register_claude(command, Path.home() / ".claude.json"):
            gained.append("claude")
    except OSError:  # a config the app may not write is the person's business, not a failed sync
        pass
    return gained
