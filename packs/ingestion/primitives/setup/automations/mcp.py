"""Codex MCP registration for the msgvault server.

Wraps `codex mcp get/add msgvault` so setup can report and install the
msgvault MCP server. Kept as its own module (rather than folded into
msgvault_home) because MCP registration is harness state, not msgvault home
state.

Changelog:
  2026-07-23 (audit):
    - Split out of the former 1,770-line setup/msgvault_setup.py.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode.
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.setup.automations.shell import (  # noqa: E402
    progress,
    run_command,
    tail,
)


def mcp_status() -> dict[str, Any]:
    """Report whether Codex is available and the msgvault MCP server is registered."""
    if not shutil.which("codex"):
        return {"installed": False, "available": False, "reason": "codex not found"}
    result = run_command(["codex", "mcp", "get", "msgvault"], timeout=20)
    return {
        "available": True,
        "installed": result["ok"],
        "message": "" if result["ok"] else tail(result.get("stderr") or result.get("stdout") or ""),
    }


def install_mcp() -> dict[str, Any]:
    """Register the msgvault MCP server in Codex when it is not already installed."""
    if not shutil.which("codex"):
        return {"status": "skipped", "reason": "codex not found"}
    current = mcp_status()
    if current.get("installed"):
        return {"status": "ok", "already_installed": True}
    progress("Installing msgvault MCP in Codex...")
    result = run_command(["codex", "mcp", "add", "msgvault", "--", "msgvault", "mcp"], timeout=60)
    if result["ok"]:
        progress("msgvault MCP installed.")
        return {"status": "ok", "already_installed": False}
    return {"status": "error", "message": tail(result.get("stderr") or result.get("stdout") or "")}
