"""Subprocess runners and output helpers for msgvault setup automation.

Everything here is side-effect plumbing shared by the other automations
modules: JSON emission to stdout, progress lines to stderr, path expansion,
and the three subprocess run styles (captured, visible/interactive, and
streamed-stderr), plus text tailing and lenient JSON extraction from noisy
CLI output.

Changelog:
  2026-07-23 (audit):
    - Split out of the former 1,770-line setup/msgvault_setup.py.
  2026-07-23 (audit dedup): emit deleted here (byte-identical to
    common.jsonio.emit); its consumers (msgvault_setup, browser_flows,
    setup_flows) now import emit from common.jsonio directly.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode.
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def progress(message: str) -> None:
    """Print a [local-msg-vault] progress line to stderr immediately."""
    print(f"[local-msg-vault] {message}", file=sys.stderr, flush=True)


def expand(path: str | Path) -> Path:
    """Return the path with ~ expanded to the user's home."""
    return Path(path).expanduser()


def run_command(cmd: list[str], *, timeout: int = 90, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Run a command with captured output and detached stdin.

    Returns {ok, returncode, stdout, stderr}; missing binaries map to
    returncode 127 and timeouts to 124 instead of raising."""
    try:
        completed = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError:
        return {"ok": False, "returncode": 127, "stdout": "", "stderr": f"{cmd[0]} not found"}
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": 124,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or f"{cmd[0]} timed out",
        }
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def run_visible_command(cmd: list[str], *, timeout: int | None = None) -> dict[str, Any]:
    """Run a command with inherited stdio for interactive/browser steps.

    Returns {ok, returncode, message}; output goes straight to the user's
    terminal so login prompts and OAuth URLs stay visible."""
    try:
        completed = subprocess.run(cmd, timeout=timeout)
    except FileNotFoundError:
        return {"ok": False, "returncode": 127, "message": f"{cmd[0]} not found"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": 124, "message": f"{cmd[0]} timed out"}
    return {"ok": completed.returncode == 0, "returncode": completed.returncode, "message": ""}


def run_streaming_command(cmd: list[str], *, timeout: int, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Run a command capturing stdout while mirroring stderr live.

    Browser automation logs land on stderr as they happen; stdout is kept
    whole so the final JSON payload can be parsed after exit."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    except FileNotFoundError:
        return {"ok": False, "returncode": 127, "stdout": "", "stderr": f"{cmd[0]} not found"}

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def read_stdout() -> None:
        """Drain the child's stdout into stdout_chunks."""
        assert proc.stdout is not None
        for line in proc.stdout:
            stdout_chunks.append(line)

    def read_stderr() -> None:
        """Drain the child's stderr into stderr_chunks while echoing it live."""
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_chunks.append(line)
            print(line, end="", file=sys.stderr, flush=True)

    threads = [threading.Thread(target=read_stdout), threading.Thread(target=read_stderr)]
    for thread in threads:
        thread.daemon = True
        thread.start()
    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        returncode = 124
    for thread in threads:
        thread.join(timeout=1)
    return {
        "ok": returncode == 0,
        "returncode": returncode,
        "stdout": "".join(stdout_chunks),
        "stderr": "".join(stderr_chunks),
    }


def tail(text: str, limit: int = 1600) -> str:
    """Return the trailing `limit` characters of stripped text."""
    text = (text or "").strip()
    return text[-limit:] if len(text) > limit else text


def parse_json_fragment(text: str) -> Any:
    """Return the first JSON object/array embedded in noisy CLI output.

    msgvault and the browser script interleave log lines with their JSON
    payload; scan for the first decodable `[`/`{` and raise JSONDecodeError
    when none decodes."""
    decoder = json.JSONDecoder()
    for idx, char in enumerate(text or ""):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text[idx:])
            return value
        except json.JSONDecodeError:
            continue
    raise json.JSONDecodeError("no JSON object or array found", text or "", 0)
