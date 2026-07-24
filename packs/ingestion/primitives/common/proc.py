#!/usr/bin/env python3
"""Child-process orchestration helpers shared by the ingestion primitives.

The discover/import orchestrators shell out to leaf primitives and read back
their JSON result. This is the ONE copy of that machinery:

- `run_cmd` — run a child with a hard timeout, stream its stderr through live,
  capture stdout, and return `(exit_code, last_json_dict, stderr_text)`. Child
  stdin is `/dev/null` so a tool cannot start an implicit interactive OAuth flow
  and block until the timeout; the working directory is the repo root.
- `py_cmd` — build a `[python, script, *args]` argv for a repo-relative script.
- `emit_progress` — one human-readable progress line to stderr, tagged with the
  caller's stage prefix (`[discover]`, `[enrich-people]`, `[gmail-import]`).

Changelog:
  2026-07-23 (audit consolidation): created; unifies the run_cmd / py_cmd copies
    from discover/common and gmail/import_steps and the three emit_progress
    copies (which differed only by prefix). The canonical run_cmd is the
    discover variant — stdin=DEVNULL and repo-root cwd — so the gmail import
    child now also runs from the repo root (its old cwd pointed one level too
    deep). `emit_progress` takes the prefix as an argument; run_cmd's timeout
    line uses the caller-supplied prefix.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import parse_last_json  # noqa: E402

DEFAULT_CHILD_TIMEOUT_SECONDS = int(os.environ.get("POWERPACKS_IMPORT_NETWORK_CHILD_TIMEOUT_SECONDS", str(6 * 60 * 60)))
DEFAULT_PROGRESS_PREFIX = "[discover]"


def emit_progress(message: str, prefix: str = DEFAULT_PROGRESS_PREFIX) -> None:
    """Write one human-readable progress line to stderr, tagged with `prefix`."""
    print(f"{prefix} {message}", file=sys.stderr, flush=True)


def run_cmd(cmd: list[str], *, timeout: int | None = None, prefix: str = DEFAULT_PROGRESS_PREFIX) -> tuple[int, dict[str, Any], str]:
    """Run a child command, returning `(exit_code, last_json_dict, stderr_text)`.

    stderr is streamed through live for progress; stdout is captured and its
    last top-level JSON object is returned. On timeout the child is killed and a
    timeout note is appended to stderr and emitted as progress under `prefix`.
    """
    effective_timeout = DEFAULT_CHILD_TIMEOUT_SECONDS if timeout is None else timeout
    proc = subprocess.Popen(
        cmd,
        cwd=_REPO_ROOT,
        # Automation-only: inheriting a terminal here lets tools such as msgvault
        # start an implicit browser OAuth flow and wait for a hidden callback
        # until the six-hour child timeout. Explicit authorization belongs to the
        # setup primitive and its consent gate.
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def read_stdout() -> None:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            stdout_chunks.append(line)

    def read_stderr() -> None:
        if proc.stderr is None:
            return
        for line in proc.stderr:
            stderr_chunks.append(line)
            sys.stderr.write(line)
            sys.stderr.flush()

    threads = [
        threading.Thread(target=read_stdout, daemon=True),
        threading.Thread(target=read_stderr, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        code = proc.wait(timeout=effective_timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = proc.wait()
        timeout_message = f"child command timed out after {effective_timeout} seconds: {' '.join(cmd)}"
        stderr_chunks.append(timeout_message + "\n")
        emit_progress(timeout_message, prefix)
    for thread in threads:
        thread.join(timeout=1)
    payload = parse_last_json("".join(stdout_chunks))
    stderr = "".join(stderr_chunks)
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()
    return code, payload, stderr


def py_cmd(script: str, *args: str) -> list[str]:
    """Build a `[python, script, *args]` argv using the current interpreter."""
    return [sys.executable, script, *args]
