#!/usr/bin/env python3
"""Wake the Codex thread that launched the Deep Context review UI.

The review server remains file-driven.  After a successful human mutation it
sends one local datagram to this bridge.  The bridge reads the existing
``review-status`` contract, and only when that contract has an agent action does
it resume the originating Codex thread through a short-lived app-server
process.  Browser input never becomes an agent prompt or shell command.

No bridge state is durable: the Unix socket is a fixed runtime endpoint, and
the authoritative workflow state remains the existing review/enrichment
manifests.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from packs.ingestion.primitives.deep_context.common import REVIEW_DIR
from packs.ingestion.primitives.deep_context.reconcile_review_web import workflow_status

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_SOCKET = (REPO_ROOT / REVIEW_DIR / "agent-bridge.sock").resolve()
DEFAULT_LOG = Path(tempfile.gettempdir()) / "powerpacks-deep-context-agent-bridge.log"
THREAD_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

WAKE_ACTIONS = {
    "preview_enrichment",
    "run_approved_enrichment",
    "run_enrichment_from_cache",
    "assemble_synthetic",
    "retry_enrichment",
    "realize",
}
STOP_AFTER_ACTIONS = {"retry_enrichment", "realize"}
BRIDGE_SANDBOX_POLICY = {
    "type": "workspaceWrite",
    "writableRoots": [str(REPO_ROOT)],
    "networkAccess": True,
}

WAKE_PROMPT = f"""\
Deep Context UI state changed. Continue the active $deep-context workflow in
{REPO_ROOT}. Run `bin/deep-context review-status` first,
update the visible task plan in this thread, and follow only the exact
`next_action` it returns. Do not infer or reuse approval from this message or
chat history. Preserve every OpenAI, Parallel, RapidAPI, and Modal approval
gate. If the action is `retry_enrichment`, diagnose and report it without an
automatic paid retry. If it is `realize`, stop at any fresh RapidAPI or Modal
approval boundary.
"""

SMOKE_PROMPT = f"""\
Read-only Deep Context same-thread bridge smoke test. In
{REPO_ROOT}, run `bin/deep-context review-status`, update
the visible task plan with the current review state, and report the
`next_action`. Do not change files, run providers, advance the workflow, or
cross any approval gate.
"""


def _socket_path(path: Path | str = DEFAULT_SOCKET) -> Path:
    return Path(path).expanduser().resolve()


def notify_bridge(event: str = "state_changed", *,
                  socket_path: Path | str = DEFAULT_SOCKET) -> bool:
    """Send a best-effort local wake.  Missing bridges are a normal fallback."""
    path = _socket_path(socket_path)
    if not path.exists():
        return False
    payload = json.dumps({"event": event}, separators=(",", ":")).encode("utf-8")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        client.settimeout(0.1)
        client.sendto(payload, str(path))
        return True
    except OSError:
        return False
    finally:
        client.close()


def _read_tail(path: Path, max_bytes: int = 2_000_000) -> list[str]:
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - max_bytes))
        body = fh.read()
    if size > max_bytes:
        body = body.split(b"\n", 1)[-1]
    return body.decode("utf-8", errors="replace").splitlines()


def find_rollout_path(thread_id: str, *,
                      sessions_root: Path | None = None) -> Path | None:
    """Find the newest persisted rollout for a thread without reading its text."""
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    root = sessions_root or (codex_home / "sessions")
    if not root.exists():
        return None
    matches = list(root.rglob(f"*{thread_id}.jsonl"))
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime_ns)


def rollout_turn_snapshot(path: Path) -> tuple[str, dict[str, str]]:
    """Read one rollout snapshot: latest started id plus per-turn lifecycles."""
    lifecycles: dict[str, str] = {}
    anonymous_lifecycle = ""
    latest_turn_id = ""
    for line in _read_tail(path):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "event_msg":
            continue
        event_type = str((event.get("payload") or {}).get("type") or "")
        if event_type in {"task_started", "task_complete", "turn_aborted"}:
            turn_id = str((event.get("payload") or {}).get("turn_id") or "")
            if event_type == "task_started":
                latest_turn_id = turn_id
            if turn_id:
                lifecycles[turn_id] = event_type
            else:
                anonymous_lifecycle = event_type
    if anonymous_lifecycle:
        lifecycles[""] = anonymous_lifecycle
    return latest_turn_id, lifecycles


def rollout_turn_lifecycles(path: Path) -> dict[str, str]:
    """Return the latest persisted lifecycle state for every observed turn."""
    _, lifecycles = rollout_turn_snapshot(path)
    return lifecycles


def rollout_turn_completed(path: Path, turn_id: str) -> bool:
    """Return whether the rollout durably completed one exact turn."""
    return bool(turn_id) and (
        rollout_turn_lifecycles(path).get(turn_id) == "task_complete")


def rollout_latest_started_turn_id(path: Path) -> str:
    """Return the id of the most recently persisted task_started event."""
    latest_turn_id, _ = rollout_turn_snapshot(path)
    return latest_turn_id


def rollout_turn_is_idle(path: Path) -> bool:
    """Return true when the latest started turn has reached a terminal event."""
    latest_turn_id, lifecycles = rollout_turn_snapshot(path)
    if not latest_turn_id:
        return False
    return lifecycles.get(latest_turn_id) in {"task_complete", "turn_aborted"}


def wait_for_turn_handoff(thread_id: str, *, timeout_seconds: float = 900,
                          poll_seconds: float = 0.25,
                          sessions_root: Path | None = None) -> Path:
    """Wait until the foreground Codex client has durably completed its turn."""
    deadline = time.monotonic() + timeout_seconds
    rollout: Path | None = None
    while time.monotonic() < deadline:
        rollout = find_rollout_path(thread_id, sessions_root=sessions_root)
        if rollout and rollout_turn_is_idle(rollout):
            # Let the final append/fsync settle before another app-server loads it.
            time.sleep(0.25)
            if rollout_turn_is_idle(rollout):
                return rollout
        time.sleep(poll_seconds)
    suffix = f" ({rollout})" if rollout else ""
    raise TimeoutError(f"Codex thread did not reach task_complete before timeout{suffix}")


class AppServerSession:
    """Minimal JSONL client for one resume + one turn/start lifecycle."""

    def __init__(self, *, codex_bin: str = "codex", timeout_seconds: float = 1800):
        self.codex_bin = codex_bin
        self.timeout_seconds = timeout_seconds
        self.process: subprocess.Popen[str] | None = None
        self.selector: selectors.BaseSelector | None = None
        self.next_id = 1

    def __enter__(self) -> "AppServerSession":
        app_server_env = os.environ.copy()
        # The resumed agent still uses repo commands that invoke uv. Keep uv's
        # disposable cache inside the writable workspace so an otherwise-safe
        # background turn cannot stall on a hidden cache approval.
        app_server_env["UV_CACHE_DIR"] = str(REPO_ROOT / ".venv" / "uv-cache")
        self.process = subprocess.Popen(
            [self.codex_bin, "app-server"],
            cwd=REPO_ROOT,
            env=app_server_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        if self.process.stdout is None:
            raise RuntimeError("Codex app-server stdout unavailable")
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        init = self.request("initialize", {
            "clientInfo": {
                "name": "powerpacks_deep_context",
                "title": "Powerpacks Deep Context",
                "version": "0.1.0",
            },
        })
        if "error" in init:
            raise RuntimeError(f"Codex app-server initialize failed: {init['error']}")
        self.send({"method": "initialized", "params": {}})
        return self

    def __exit__(self, *_: object) -> None:
        if self.selector is not None:
            self.selector.close()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)

    def send(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("Codex app-server stdin unavailable")
        self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def _read(self, deadline: float) -> dict[str, Any]:
        if self.process is None or self.process.stdout is None or self.selector is None:
            raise RuntimeError("Codex app-server is not running")
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"Codex app-server exited with status {self.process.returncode}")
            ready = self.selector.select(timeout=min(0.25, deadline - time.monotonic()))
            if not ready:
                continue
            line = self.process.stdout.readline()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        raise TimeoutError("Timed out waiting for Codex app-server")

    def _read_once(self, timeout_seconds: float = 0.5) -> dict[str, Any] | None:
        """Read at most one message so callers can also observe rollout state."""
        if self.process is None or self.process.stdout is None or self.selector is None:
            raise RuntimeError("Codex app-server is not running")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"Codex app-server exited with status {self.process.returncode}")
            ready = self.selector.select(timeout=min(0.25, deadline - time.monotonic()))
            if not ready:
                continue
            line = self.process.stdout.readline()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return None

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        self.send({"method": method, "id": request_id, "params": params})
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            message = self._read(deadline)
            if message.get("id") == request_id:
                return message

    def resume_and_turn(self, thread_id: str, prompt: str) -> dict[str, Any]:
        resumed = self.request("thread/resume", {
            "threadId": thread_id,
            "cwd": str(REPO_ROOT),
        })
        if "error" in resumed:
            raise RuntimeError(f"Codex thread/resume failed: {resumed['error']}")
        actual_id = str(((resumed.get("result") or {}).get("thread") or {}).get("id") or "")
        if actual_id != thread_id:
            raise RuntimeError("Codex thread/resume returned a different thread")
        resumed_result = resumed.get("result") or {}
        original_approval = resumed_result.get("approvalPolicy")
        original_sandbox = resumed_result.get("sandbox") or {}

        request_id = self.next_id
        self.next_id += 1
        self.send({
            "method": "turn/start",
            "id": request_id,
            "params": {
                "threadId": thread_id,
                "cwd": str(REPO_ROOT),
                "input": [{"type": "text", "text": prompt}],
                # UI/provider approvals remain enforced by the fixed
                # review-status contract. Codex itself must not raise an
                # invisible sandbox prompt in this background client.
                "approvalPolicy": "never",
                "sandboxPolicy": BRIDGE_SANDBOX_POLICY,
            },
        })
        deadline = time.monotonic() + self.timeout_seconds
        started: dict[str, Any] | None = None
        started_turn_id = ""
        rollout = find_rollout_path(thread_id)
        completed: dict[str, Any] | None = None
        completed_via = ""
        while time.monotonic() < deadline:
            message = self._read_once(min(0.5, deadline - time.monotonic()))
            if message is None:
                message = {}
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(f"Codex turn/start failed: {message['error']}")
                started = message
                started_turn_id = str(
                    (((message.get("result") or {}).get("turn") or {}).get("id"))
                    or ""
                )
                if not started_turn_id:
                    raise RuntimeError(
                        "Codex turn/start response did not include a turn id")
            elif str(message.get("method") or "") == "turn/completed":
                params = message.get("params") or {}
                completed_turn_id = str(
                    ((params.get("turn") or {}).get("id")) or "")
                if started_turn_id and completed_turn_id == started_turn_id:
                    completed = params
                    completed_via = "notification"
                    break

            # A desktop client can supersede the bridge-started turn before this
            # short-lived app-server receives turn/completed. The canonical
            # rollout still records a task_complete carrying the exact turn id.
            # Correlate on that id so an overlapping foreground turn cannot
            # falsely complete this bridge request.
            if rollout is None:
                rollout = find_rollout_path(thread_id)
            if (rollout is not None and started_turn_id
                    and rollout_turn_completed(rollout, started_turn_id)):
                completed = {
                    "status": "completed",
                    "turn": {"id": started_turn_id},
                }
                completed_via = "rollout"
                break
        else:
            raise TimeoutError("Timed out waiting for Codex app-server turn completion")

        result = {
            "started": started,
            "completed": completed or {},
            "completed_via": completed_via,
        }
        restore_params: dict[str, Any] = {
            "threadId": thread_id,
            "cwd": str(REPO_ROOT),
        }
        if original_approval:
            restore_params["approvalPolicy"] = original_approval
        sandbox_mode = {
            "readOnly": "read-only",
            "workspaceWrite": "workspace-write",
            "dangerFullAccess": "danger-full-access",
        }.get(str(original_sandbox.get("type") or ""))
        if sandbox_mode:
            restore_params["sandbox"] = sandbox_mode
        restored = self.request("thread/resume", restore_params)
        if "error" in restored:
            raise RuntimeError(
                f"Codex thread policy restore failed: {restored['error']}")
        result["policy_restored"] = True
        return result


def run_same_thread_turn(thread_id: str, prompt: str, *,
                         codex_bin: str = "codex",
                         timeout_seconds: float = 1800) -> dict[str, Any]:
    if not THREAD_ID_RE.fullmatch(thread_id):
        raise ValueError("Invalid Codex thread id")
    wait_for_turn_handoff(thread_id)
    with AppServerSession(codex_bin=codex_bin,
                          timeout_seconds=timeout_seconds) as session:
        return session.resume_and_turn(thread_id, prompt)


def _status_token(status: dict[str, Any]) -> str:
    selection = status.get("selection") or {}
    enrichment = status.get("enrichment") or {}
    progress = status.get("progress") or {}
    payload = {
        "next_action": status.get("next_action"),
        "people_revision": selection.get("review_revision"),
        "selection": selection.get("sha256"),
        "enrichment_status": enrichment.get("status"),
        "enrichment_updated_at": enrichment.get("updated_at"),
        "worth_pending": progress.get("worth_pending"),
        "linkedin_pending": progress.get("linkedin_pending"),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class BridgeController:
    """Deterministic event filter around the expensive same-thread wake."""

    def __init__(self, thread_id: str, *,
                 status_reader: Callable[[], dict[str, Any]] = workflow_status,
                 waker: Callable[[str, str], dict[str, Any]] = run_same_thread_turn):
        self.thread_id = thread_id
        self.status_reader = status_reader
        self.waker = waker
        self.last_token = ""

    def handle(self, event: str, *,
               on_dispatch: Callable[[dict[str, Any]], object] | None = None
               ) -> dict[str, Any]:
        if event == "smoke":
            if on_dispatch is not None:
                on_dispatch({
                    "event": event,
                    "action": "smoke",
                    "state": "dispatching",
                })
            self.waker(self.thread_id, SMOKE_PROMPT)
            return {"event": event, "woke": True, "stop": False}
        status = self.status_reader()
        action = str(status.get("next_action") or "")
        token = _status_token(status)
        if action not in WAKE_ACTIONS:
            return {"event": event, "action": action, "woke": False, "stop": False}
        if token == self.last_token:
            return {"event": event, "action": action, "woke": False, "stop": False}
        if on_dispatch is not None:
            on_dispatch({
                "event": event,
                "action": action,
                "state": "dispatching",
            })
        self.waker(self.thread_id, WAKE_PROMPT)
        self.last_token = token
        return {
            "event": event,
            "action": action,
            "woke": True,
            "stop": action in STOP_AFTER_ACTIONS,
        }


def serve_bridge(thread_id: str, *, socket_path: Path | str = DEFAULT_SOCKET) -> int:
    if not THREAD_ID_RE.fullmatch(thread_id):
        raise SystemExit("Invalid or missing Codex thread id")
    path = _socket_path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(str(path))
    path.chmod(0o600)
    socket_inode = path.stat().st_ino
    server.settimeout(1.0)
    controller = BridgeController(thread_id)
    stopping = False

    def request_stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        while not stopping:
            try:
                payload = server.recv(4096)
            except socket.timeout:
                continue
            try:
                event = str((json.loads(payload) or {}).get("event") or "")
            except (json.JSONDecodeError, AttributeError):
                continue
            if event == "stop":
                break
            if event not in {"state_changed", "smoke"}:
                continue
            try:
                result = controller.handle(
                    event,
                    on_dispatch=lambda payload: print(
                        json.dumps(payload, sort_keys=True), flush=True),
                )
                print(json.dumps(result, sort_keys=True), flush=True)
            except Exception as exc:
                # A failed wake must not launch repeated turns for the same
                # datagram. Keep only a terse local diagnostic; never log
                # prompts, thread history, environment values, or form data.
                print(json.dumps({
                    "event": event,
                    "error": f"{type(exc).__name__}: {exc}",
                }, sort_keys=True), file=sys.stderr, flush=True)
                continue
            if result.get("stop"):
                break
    finally:
        server.close()
        try:
            if path.stat().st_ino == socket_inode:
                path.unlink()
        except FileNotFoundError:
            pass
    return 0


def stop_bridge(*, socket_path: Path | str = DEFAULT_SOCKET,
                timeout_seconds: float = 3) -> bool:
    path = _socket_path(socket_path)
    if not path.exists():
        return True
    if not notify_bridge("stop", socket_path=path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return True
    deadline = time.monotonic() + timeout_seconds
    while path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    return not path.exists()


def start_bridge(thread_id: str, *, socket_path: Path | str = DEFAULT_SOCKET,
                 timeout_seconds: float = 5) -> dict[str, Any]:
    if not THREAD_ID_RE.fullmatch(thread_id):
        raise ValueError("Invalid or missing CODEX_THREAD_ID")
    codex_bin = shutil.which("codex")
    if not codex_bin:
        raise FileNotFoundError("codex CLI not found")
    path = _socket_path(socket_path)
    if not stop_bridge(socket_path=path):
        raise RuntimeError(f"Existing Deep Context agent bridge did not stop: {path}")
    env = os.environ.copy()
    env["POWERPACKS_AGENT_THREAD_ID"] = thread_id
    env["POWERPACKS_AGENT_BRIDGE_SOCKET"] = str(path)
    DEFAULT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with DEFAULT_LOG.open("w", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            [sys.executable, "-m",
             "packs.ingestion.primitives.deep_context.review_agent_bridge", "serve"],
            cwd=REPO_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
        )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.exists():
            return {
                "primitive": "deep_context_review_agent_bridge",
                "status": "started",
                "pid": process.pid,
                "socket": str(path),
                "log": str(DEFAULT_LOG),
                "thread_id": thread_id,
            }
        if process.poll() is not None:
            raise RuntimeError(
                f"Deep Context agent bridge exited with status {process.returncode}")
        time.sleep(0.05)
    process.terminate()
    raise TimeoutError("Deep Context agent bridge did not create its socket")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deep Context same-thread Codex bridge")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--socket", default=str(DEFAULT_SOCKET))
    stop = sub.add_parser("stop")
    stop.add_argument("--socket", default=str(DEFAULT_SOCKET))
    wake = sub.add_parser("wake")
    wake.add_argument("--socket", default=str(DEFAULT_SOCKET))
    smoke = sub.add_parser("smoke")
    smoke.add_argument("--socket", default=str(DEFAULT_SOCKET))
    serve = sub.add_parser("serve")
    serve.add_argument("--socket",
                       default=os.environ.get("POWERPACKS_AGENT_BRIDGE_SOCKET",
                                              str(DEFAULT_SOCKET)))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "start":
        thread_id = str(os.environ.get("CODEX_THREAD_ID") or "").strip()
        print(json.dumps(start_bridge(thread_id, socket_path=args.socket), indent=2))
        return 0
    if args.command == "stop":
        stopped = stop_bridge(socket_path=args.socket)
        print(json.dumps({
            "primitive": "deep_context_review_agent_bridge",
            "status": "stopped" if stopped else "failed",
            "socket": str(_socket_path(args.socket)),
        }, indent=2))
        return 0 if stopped else 1
    if args.command in {"wake", "smoke"}:
        sent = notify_bridge(
            "smoke" if args.command == "smoke" else "state_changed",
            socket_path=args.socket)
        print(json.dumps({
            "primitive": "deep_context_review_agent_bridge",
            "status": "notified" if sent else "not_running",
            "socket": str(_socket_path(args.socket)),
        }, indent=2))
        return 0 if sent else 1
    thread_id = str(os.environ.get("POWERPACKS_AGENT_THREAD_ID") or "").strip()
    return serve_bridge(thread_id, socket_path=args.socket)


if __name__ == "__main__":
    sys.exit(main())
