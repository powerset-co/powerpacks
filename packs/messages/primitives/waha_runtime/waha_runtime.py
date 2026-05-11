#!/usr/bin/env python3
"""Docker + WAHA container lifecycle primitive for the WhatsApp pack.

This primitive is intentionally narrow:

- check that Docker is installed and the daemon is reachable
- pull and start a local WAHA container (Chrome/WEBJS engine by default)
- stop / remove the container
- report container status

It does not call the WAHA HTTP API. Session, QR auth, and contact extraction
live in sibling primitives (`waha_session`, `extract_whatsapp_contacts`).

Stdlib-only. Exits non-zero with a JSON manifest on diagnostics so an agent
can decide whether to ask the user to install Docker.
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


DEFAULT_CONTAINER_NAME = os.environ.get("POWERPACKS_WAHA_CONTAINER", "powerpacks-waha")
DEFAULT_PORT = int(os.environ.get("POWERPACKS_WAHA_PORT", "3000"))
DEFAULT_API_KEY = os.environ.get("POWERPACKS_WAHA_API_KEY", "powerpacks-local")
DEFAULT_ENGINE = os.environ.get("POWERPACKS_WAHA_ENGINE", "WEBJS")
DEFAULT_IMAGE = os.environ.get("POWERPACKS_WAHA_IMAGE", "devlikeapro/waha:chrome-2026.3.4")
DEFAULT_SESSIONS_DIR = Path(os.environ.get(
    "POWERPACKS_WAHA_SESSIONS_DIR",
    str(Path.home() / ".powerpacks" / "waha-sessions-chrome"),
))


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout or "", result.stderr or ""
    except FileNotFoundError as exc:
        return 127, "", f"command not found: {cmd[0]} ({exc})"
    except subprocess.TimeoutExpired as exc:
        return 124, "", f"timeout after {exc.timeout}s"


def docker_state() -> dict[str, Any]:
    """Inspect the local Docker installation and daemon."""
    state: dict[str, Any] = {
        "binary": shutil.which("docker"),
        "installed": False,
        "daemon_ok": False,
        "version": None,
        "platform": sys.platform,
        "uname_machine": None,
        "alternatives": _docker_install_hints(),
        "error": None,
    }
    code, out, _ = run(["uname", "-m"], timeout=5)
    if code == 0:
        state["uname_machine"] = out.strip() or None

    if not state["binary"]:
        state["error"] = "docker binary not found on PATH"
        return state

    state["installed"] = True
    code, out, err = run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=10)
    if code == 0 and out.strip():
        state["daemon_ok"] = True
        state["version"] = out.strip()
        return state

    code2, out2, err2 = run(["docker", "info"], timeout=10)
    if code2 == 0:
        state["daemon_ok"] = True
        return state

    state["error"] = (err.strip() or err2.strip() or out2.strip() or "docker daemon not reachable")
    return state


def _docker_install_hints() -> list[dict[str, str]]:
    """Curated install paths the skill can surface to the user."""
    return [
        {
            "name": "Docker Desktop (macOS, GUI)",
            "url": "https://www.docker.com/products/docker-desktop/",
            "install_cmd": "brew install --cask docker",
            "start_cmd": "open -a Docker",
        },
        {
            "name": "Colima (macOS, lightweight)",
            "url": "https://github.com/abiosoft/colima",
            "install_cmd": "brew install colima docker",
            "start_cmd": "colima start --memory 2 --vm-type vz --vz-rosetta",
        },
        {
            "name": "Docker Engine (Linux)",
            "url": "https://docs.docker.com/engine/install/",
            "install_cmd": "curl -fsSL https://get.docker.com | sh",
            "start_cmd": "sudo systemctl start docker",
        },
    ]


def container_state(container_name: str) -> dict[str, Any]:
    code, out, err = run([
        "docker", "inspect",
        "--format",
        "{{.State.Status}}|{{.State.Running}}|{{.Config.Image}}|{{(index (index .NetworkSettings.Ports \"3000/tcp\") 0).HostPort}}",
        container_name,
    ], timeout=10)
    if code != 0:
        return {"name": container_name, "exists": False, "running": False, "error": err.strip() or None}
    parts = (out.strip().split("|") + ["", "", "", ""])[:4]
    status, running, image, port = parts
    return {
        "name": container_name,
        "exists": True,
        "running": running.lower() == "true",
        "status": status or None,
        "image": image or None,
        "host_port": port or None,
    }


def cmd_check(args: argparse.Namespace) -> int:
    docker = docker_state()
    container = container_state(args.container_name) if docker["installed"] else {
        "name": args.container_name, "exists": False, "running": False, "error": "docker not installed",
    }
    manifest = {
        "primitive": "waha_runtime",
        "command": "check",
        "checked_at": now_iso(),
        "docker": docker,
        "container": container,
        "image": args.image,
        "engine": args.engine,
        "session_dir": str(args.session_dir),
        "ready_to_start": bool(docker["installed"] and docker["daemon_ok"]),
    }
    emit(manifest)
    if not manifest["ready_to_start"]:
        return 1
    return 0


def _ensure_session_dir(session_dir: Path) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)


def _platform_args(machine: str | None) -> list[str]:
    """Pin linux/amd64 for WAHA on Apple Silicon."""
    if (machine or "").lower() in {"arm64", "aarch64"}:
        return ["--platform", "linux/amd64"]
    return []


def cmd_up(args: argparse.Namespace) -> int:
    docker = docker_state()
    if not docker["installed"] or not docker["daemon_ok"]:
        emit({
            "primitive": "waha_runtime",
            "command": "up",
            "status": "failed",
            "error": "docker not available",
            "docker": docker,
        })
        return 1

    _ensure_session_dir(args.session_dir)
    pre_state = container_state(args.container_name)

    # Reuse a healthy container unless --recreate is set.
    if pre_state.get("running") and not args.recreate:
        emit({
            "primitive": "waha_runtime",
            "command": "up",
            "status": "already_running",
            "container": pre_state,
            "image": args.image,
            "engine": args.engine,
            "session_dir": str(args.session_dir),
        })
        return 0

    if pre_state.get("exists"):
        run(["docker", "rm", "-f", args.container_name], timeout=20)

    pull_logs: list[str] = []
    code, out, err = run(["docker", "image", "inspect", args.image], timeout=15)
    if code != 0:
        platform_args = _platform_args(docker.get("uname_machine"))
        pull_cmd = ["docker", "pull", *platform_args, args.image]
        pull_logs.append(" ".join(pull_cmd))
        code_pull, out_pull, err_pull = run(pull_cmd, timeout=args.pull_timeout)
        pull_logs.append(out_pull.strip())
        if err_pull.strip():
            pull_logs.append(err_pull.strip())
        if code_pull != 0:
            emit({
                "primitive": "waha_runtime",
                "command": "up",
                "status": "failed",
                "error": "failed to pull WAHA image",
                "image": args.image,
                "logs": pull_logs,
            })
            return code_pull

    platform_args = _platform_args(docker.get("uname_machine"))
    run_cmd = [
        "docker", "run", "-d",
        *platform_args,
        "--name", args.container_name,
        "-p", f"127.0.0.1:{args.port}:3000",
        "-v", f"{args.session_dir}:/app/.sessions",
        "-e", f"WAHA_DEFAULT_ENGINE={args.engine}",
        "-e", f"WHATSAPP_DEFAULT_ENGINE={args.engine}",
        "-e", "WHATSAPP_RESTART_ALL_SESSIONS=true",
        "-e", f"WAHA_API_KEY={args.api_key}",
        args.image,
    ]
    code, out, err = run(run_cmd, timeout=args.start_timeout)
    if code != 0:
        emit({
            "primitive": "waha_runtime",
            "command": "up",
            "status": "failed",
            "error": err.strip() or "docker run failed",
            "command_line": run_cmd,
            "logs": pull_logs,
        })
        return code

    post_state = container_state(args.container_name)
    emit({
        "primitive": "waha_runtime",
        "command": "up",
        "status": "started",
        "container": post_state,
        "image": args.image,
        "engine": args.engine,
        "port": args.port,
        "api_key": args.api_key,
        "base_url": f"http://127.0.0.1:{args.port}",
        "session_dir": str(args.session_dir),
        "logs": pull_logs,
    })
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    docker = docker_state()
    if not docker["installed"]:
        emit({
            "primitive": "waha_runtime",
            "command": "down",
            "status": "noop",
            "reason": "docker not installed",
        })
        return 0

    pre_state = container_state(args.container_name)
    if not pre_state.get("exists"):
        emit({
            "primitive": "waha_runtime",
            "command": "down",
            "status": "noop",
            "reason": "container not present",
            "container": pre_state,
        })
        return 0

    run(["docker", "rm", "-f", args.container_name], timeout=30)
    purged = False
    if args.purge_session and args.session_dir.exists():
        shutil.rmtree(args.session_dir, ignore_errors=True)
        purged = True

    emit({
        "primitive": "waha_runtime",
        "command": "down",
        "status": "stopped",
        "container": pre_state,
        "session_dir_purged": purged,
        "session_dir": str(args.session_dir),
    })
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    docker = docker_state()
    container = container_state(args.container_name) if docker["installed"] else {
        "name": args.container_name, "exists": False, "running": False,
    }
    payload = {
        "primitive": "waha_runtime",
        "command": "status",
        "checked_at": now_iso(),
        "docker": docker,
        "container": container,
        "base_url": f"http://127.0.0.1:{args.port}",
        "image": args.image,
        "engine": args.engine,
        "session_dir": str(args.session_dir),
    }
    emit(payload)
    return 0 if container.get("running") else 1


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--container-name", default=DEFAULT_CONTAINER_NAME)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--api-key", default=DEFAULT_API_KEY)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--engine", default=DEFAULT_ENGINE)
    parser.add_argument("--session-dir", type=Path, default=DEFAULT_SESSIONS_DIR)


def main() -> None:
    parser = argparse.ArgumentParser(description="WAHA Docker container lifecycle")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="Inspect Docker + WAHA container readiness")
    add_common_args(check)
    check.set_defaults(func=cmd_check)

    up = sub.add_parser("up", help="Start (or reuse) the local WAHA container")
    add_common_args(up)
    up.add_argument("--recreate", action="store_true", help="Force-remove and recreate the container")
    up.add_argument("--pull-timeout", type=int, default=600)
    up.add_argument("--start-timeout", type=int, default=300)
    up.set_defaults(func=cmd_up)

    down = sub.add_parser("down", help="Stop and remove the WAHA container")
    add_common_args(down)
    down.add_argument("--purge-session", action="store_true", help="Also remove persisted session credentials")
    down.set_defaults(func=cmd_down)

    status = sub.add_parser("status", help="Print Docker + container status as JSON")
    add_common_args(status)
    status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
