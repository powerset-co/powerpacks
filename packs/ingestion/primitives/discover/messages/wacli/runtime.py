"""Error, progress, and subprocess plumbing shared by every wacli module.

`PrimitiveBlocked` / `PrimitiveFailed` are the two outcomes the client raises
(blocked = the user must do something, exit 20; failed = exit 1), `emit_status`
is the one-line stderr progress channel, `write_progress` appends a stage's
human-readable progress lines, and `run_command` is the single place the wacli
GO BINARY is invoked as a subprocess.

Changelog:
  2026-07-30 (wacli split): extracted from the single-file `whatsapp_wacli.py`.
    Behavior unchanged; every other wacli module calls `runtime.run_command`
    rather than defining its own runner.
  2026-07-24 (dedup): the local `parse_last_json` fork was deleted; its
    scan-forward recovery was promoted into `common/jsonio.parse_last_json`,
    which this module now imports (results are `{}` rather than `None` when no
    object decodes — every consumer already coerced non-dicts to `{}`).
    `run_command` is PINNED as deliberately divergent from
    `common/proc.run_cmd` (see the reasons at its definition); its never-passed
    `env` and `stream_to_stderr` parameters were dropped.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[6]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import now_iso, parse_last_json  # noqa: E402

STATUS_PREFIX = "[whatsapp-wacli]"


class PrimitiveBlocked(Exception):
    def __init__(self, payload: dict[str, Any], code: int = 20) -> None:
        super().__init__(payload.get("message") or payload.get("status") or "blocked")
        self.payload = payload
        self.code = code


class PrimitiveFailed(Exception):
    pass


def emit_status(message: str) -> None:
    print(f"{STATUS_PREFIX} {message}", file=sys.stderr, flush=True)


def write_progress(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"timestamp": now_iso(), **payload}, sort_keys=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def run_command(
    cmd: list[str],
    *,
    timeout: int,
    heartbeat_message: str | None = None,
    heartbeat_interval: float = 120.0,
) -> dict[str, Any]:
    """Run one `wacli` binary invocation, returning
    `{returncode, stdout, stderr, json}`.

    PINNED DIVERGENCE from `common/proc.py:run_cmd` — deliberately NOT unified:

    - `run_cmd` returns `(code, last_json, stderr)` and throws the child's raw
      stdout away, because its children are Powerpacks primitives whose stdout
      IS the JSON payload. wacli is a Go binary whose stdout TEXT is
      load-bearing: `wacli_version` parses the version string out of it,
      `run_sync` scans stdout+stderr for the linked-device block message, and
      the failure paths tail it into the error. Adding stdout to `run_cmd`
      would break its tuple contract for its own caller.
    - On timeout `run_cmd` kills the child and reports the killed process's
      returncode; here the timeout is normalized to 124 with the partial stdout
      and stderr preserved, so a sync that ran out of its 3-hour budget can
      still be classified (e.g. the linked-device block) instead of just
      failing opaquely.
    - `run_cmd` streams the child's stderr through live as progress. wacli logs
      pages of connection/sync noise, so its stderr is captured silently and a
      single `heartbeat_message` line is emitted every `heartbeat_interval`
      seconds for the long auth/sync/history-depth runs instead.
    - No heartbeat means no reader threads at all: a plain
      `subprocess.run` fast path for the short `--version` / `--json` calls.

    Unifying would mean bolting four additive knobs (raw stdout, 124
    normalization, heartbeat, stderr suppression) onto a helper with one other
    caller — a net complexity increase, not a dedup.
    """
    if not heartbeat_message:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            stderr = (stderr + f"\ncommand timed out after {timeout}s").strip() + "\n"
            return {
                "returncode": 124,
                "stdout": stdout,
                "stderr": stderr,
                "json": parse_last_json(stdout),
            }
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "json": parse_last_json(proc.stdout),
        }

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def reader(stream: Any, chunks: list[str]) -> None:
        for line in iter(stream.readline, ""):
            chunks.append(line)

    threads = [
        threading.Thread(target=reader, args=(proc.stdout, stdout_chunks), daemon=True),
        threading.Thread(target=reader, args=(proc.stderr, stderr_chunks), daemon=True),
    ]
    for thread in threads:
        thread.start()

    started = time.time()
    next_heartbeat = started + heartbeat_interval
    timed_out = False
    while proc.poll() is None:
        if time.time() - started > timeout:
            timed_out = True
            proc.kill()
            break
        if heartbeat_message and time.time() >= next_heartbeat:
            emit_status(heartbeat_message)
            next_heartbeat = time.time() + heartbeat_interval
        time.sleep(0.2)

    returncode = proc.wait()
    for thread in threads:
        thread.join(timeout=1)
    stdout = "".join(stdout_chunks)
    stderr = "".join(stderr_chunks)
    if timed_out:
        stderr = (stderr + f"\ncommand timed out after {timeout}s").strip() + "\n"
        returncode = 124
    return {
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "json": parse_last_json(stdout),
    }


def command_text(cmd: list[str]) -> str:
    return " ".join(cmd)
