#!/usr/bin/env python3
"""WAHA session + QR auth primitive for the WhatsApp pack.

Talks to a running WAHA container (started by `waha_runtime`) over HTTP and:

- waits for the WAHA HTTP server to become healthy
- creates / restarts a session with NOWEB store enabled
- fetches the QR code as PNG (via WAHA's image endpoint) and as raw value text
- waits until the session reaches status `WORKING`
- can stop/delete the session

Stdlib-only. No `requests`, no `qrcode` library — WAHA itself renders the PNG
that the user scans with their phone.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = os.environ.get("POWERPACKS_WAHA_BASE_URL", "http://127.0.0.1:3000")
DEFAULT_API_KEY = os.environ.get("POWERPACKS_WAHA_API_KEY", "powerpacks-local")
DEFAULT_SESSION = os.environ.get("POWERPACKS_WAHA_SESSION", "default")
DEFAULT_QR_DIR = Path(os.environ.get(
    "POWERPACKS_WAHA_QR_DIR",
    str(Path(".powerpacks/messages/whatsapp")),
))


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _request(
    base_url: str,
    api_key: str,
    method: str,
    path: str,
    *,
    json_body: Any = None,
    timeout: int = 15,
    accept: str = "application/json",
) -> tuple[int, bytes, dict[str, str]]:
    url = base_url.rstrip("/") + path
    headers = {"X-Api-Key": api_key, "Accept": accept}
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, body, dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read()
        except Exception:
            pass
        return exc.code, body, dict(exc.headers.items()) if exc.headers else {}
    except urllib.error.URLError as exc:
        raise ConnectionError(str(exc.reason)) from exc


def _decode_json(body: bytes) -> Any:
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def wait_for_healthy(base_url: str, api_key: str, timeout: int) -> dict[str, Any]:
    deadline = time.time() + timeout
    last_error = None
    attempts = 0
    while time.time() < deadline:
        attempts += 1
        try:
            status, body, _ = _request(base_url, api_key, "GET", "/api/sessions", timeout=10)
            if status == 200:
                return {"healthy": True, "attempts": attempts, "elapsed_ms": int((timeout - (deadline - time.time())) * 1000)}
            last_error = f"HTTP {status}"
        except ConnectionError as exc:
            last_error = str(exc)
        time.sleep(1.5)
    return {"healthy": False, "attempts": attempts, "error": last_error or "timed out"}


def session_state(base_url: str, api_key: str, session: str) -> dict[str, Any]:
    try:
        status, body, _ = _request(base_url, api_key, "GET", f"/api/sessions/{session}", timeout=10)
    except ConnectionError as exc:
        return {"reachable": False, "error": str(exc)}
    payload = _decode_json(body)
    if status != 200:
        return {"reachable": True, "exists": False, "http_status": status, "payload": payload}
    if not isinstance(payload, dict):
        return {"reachable": True, "exists": True, "payload": payload}
    return {
        "reachable": True,
        "exists": True,
        "name": payload.get("name") or session,
        "status": payload.get("status"),
        "engine": (payload.get("engine") or {}).get("engine") if isinstance(payload.get("engine"), dict) else None,
        "me": payload.get("me"),
        "payload": payload,
    }


def stop_session(base_url: str, api_key: str, session: str) -> dict[str, Any]:
    logs: list[dict[str, Any]] = []
    for method, path in (("PUT", f"/api/sessions/{session}/stop"), ("DELETE", f"/api/sessions/{session}")):
        try:
            status, body, _ = _request(base_url, api_key, method, path, timeout=10)
            logs.append({"method": method, "path": path, "status": status})
        except ConnectionError as exc:
            logs.append({"method": method, "path": path, "error": str(exc)})
    return {"actions": logs}


def start_session(base_url: str, api_key: str, session: str) -> dict[str, Any]:
    body = {
        "name": session,
        "config": {
            "noweb": {
                "store": {"enabled": True, "full_sync": True},
            },
        },
    }
    try:
        status, raw, _ = _request(base_url, api_key, "POST", "/api/sessions/start", json_body=body, timeout=15)
    except ConnectionError as exc:
        return {"started": False, "error": str(exc)}
    if status == 422:
        # Stale session. Force-stop and retry once.
        stop_logs = stop_session(base_url, api_key, session)
        time.sleep(2)
        try:
            status, raw, _ = _request(base_url, api_key, "POST", "/api/sessions/start", json_body=body, timeout=15)
        except ConnectionError as exc:
            return {"started": False, "error": str(exc), "stop_logs": stop_logs}
    payload = _decode_json(raw)
    if status not in (200, 201):
        return {"started": False, "http_status": status, "payload": payload}
    return {"started": True, "http_status": status, "payload": payload}


def fetch_qr_image(base_url: str, api_key: str, session: str, dest: Path) -> dict[str, Any]:
    try:
        status, body, headers = _request(
            base_url, api_key, "GET",
            f"/api/{session}/auth/qr?format=image",
            timeout=30,
            accept="image/png",
        )
    except ConnectionError as exc:
        return {"saved": False, "error": str(exc)}
    if status != 200 or not body:
        return {"saved": False, "http_status": status, "bytes": len(body)}
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return {
        "saved": True,
        "path": str(dest),
        "bytes": len(body),
        "content_type": headers.get("Content-Type"),
    }


def fetch_qr_value(base_url: str, api_key: str, session: str, dest: Path) -> dict[str, Any]:
    try:
        status, body, _ = _request(
            base_url, api_key, "GET",
            f"/api/{session}/auth/qr?format=raw",
            timeout=15,
        )
    except ConnectionError as exc:
        return {"saved": False, "error": str(exc)}
    payload = _decode_json(body)
    if status != 200 or not isinstance(payload, dict):
        return {"saved": False, "http_status": status, "payload": payload}
    value = payload.get("value") or payload.get("qr") or ""
    if not value:
        return {"saved": False, "http_status": status, "payload": payload}
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(value, encoding="utf-8")
    return {"saved": True, "path": str(dest), "length": len(value), "value": value}


def open_file(path: Path) -> bool:
    if sys.platform == "darwin":
        cmd = ["open", str(path)]
    elif shutil.which("xdg-open"):
        cmd = ["xdg-open", str(path)]
    elif sys.platform.startswith("win"):
        cmd = ["cmd", "/c", "start", "", str(path)]
    else:
        return False
    try:
        subprocess.run(cmd, capture_output=True, timeout=5)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def cmd_status(args: argparse.Namespace) -> int:
    state = session_state(args.base_url, args.api_key, args.session)
    payload = {
        "primitive": "waha_session",
        "command": "status",
        "checked_at": now_iso(),
        "base_url": args.base_url,
        "session": args.session,
        "state": state,
    }
    emit(payload)
    if state.get("status") == "WORKING":
        return 0
    return 1


def cmd_health(args: argparse.Namespace) -> int:
    health = wait_for_healthy(args.base_url, args.api_key, args.timeout)
    payload = {
        "primitive": "waha_session",
        "command": "health",
        "checked_at": now_iso(),
        "base_url": args.base_url,
        "result": health,
    }
    emit(payload)
    return 0 if health.get("healthy") else 1


def cmd_start(args: argparse.Namespace) -> int:
    health = wait_for_healthy(args.base_url, args.api_key, args.health_timeout)
    if not health.get("healthy"):
        emit({
            "primitive": "waha_session",
            "command": "start",
            "status": "failed",
            "reason": "WAHA HTTP not healthy",
            "health": health,
        })
        return 1

    pre_state = session_state(args.base_url, args.api_key, args.session)
    started_payload = None
    if args.force or pre_state.get("status") not in {"WORKING", "STARTING", "SCAN_QR_CODE"}:
        if pre_state.get("exists") and args.force:
            stop_session(args.base_url, args.api_key, args.session)
            time.sleep(1)
        started_payload = start_session(args.base_url, args.api_key, args.session)
        if not started_payload.get("started"):
            emit({
                "primitive": "waha_session",
                "command": "start",
                "status": "failed",
                "reason": "could not start WAHA session",
                "details": started_payload,
            })
            return 1

    qr_image_path = args.qr_dir / "qr.png"
    qr_value_path = args.qr_dir / "qr.txt"
    image_meta = {"saved": False, "skipped": True}
    value_meta = {"saved": False, "skipped": True}

    # Pull QR best-effort. If the session is already WORKING the QR endpoints
    # may be unavailable; that's fine.
    image_meta = fetch_qr_image(args.base_url, args.api_key, args.session, qr_image_path)
    value_meta = fetch_qr_value(args.base_url, args.api_key, args.session, qr_value_path)

    opened = False
    if args.open and image_meta.get("saved"):
        opened = open_file(qr_image_path)

    final_state = session_state(args.base_url, args.api_key, args.session)

    payload = {
        "primitive": "waha_session",
        "command": "start",
        "status": "ok",
        "started_at": now_iso(),
        "base_url": args.base_url,
        "session": args.session,
        "pre_state": pre_state,
        "start_response": started_payload,
        "qr": {
            "image": image_meta,
            "value": value_meta,
            "opened": opened,
            "instructions": "WhatsApp > Settings > Linked Devices > Link a Device, then scan the PNG.",
        },
        "state": final_state,
    }
    emit(payload)
    if args.wait:
        return _wait_until_working(args, payload)
    return 0


def _wait_until_working(args: argparse.Namespace, prelude: dict[str, Any] | None = None) -> int:
    deadline = time.time() + args.wait_timeout
    last_state: dict[str, Any] = {}
    last_qr_refresh = 0.0
    while time.time() < deadline:
        last_state = session_state(args.base_url, args.api_key, args.session)
        status = last_state.get("status")
        if status == "WORKING":
            emit({
                "primitive": "waha_session",
                "command": "wait",
                "status": "working",
                "checked_at": now_iso(),
                "session": args.session,
                "state": last_state,
            })
            return 0
        if status == "FAILED":
            emit({
                "primitive": "waha_session",
                "command": "wait",
                "status": "failed",
                "checked_at": now_iso(),
                "session": args.session,
                "state": last_state,
            })
            return 1
        # Refresh QR every 15s in case the user is mid-scan.
        if args.qr_dir and time.time() - last_qr_refresh > 15:
            fetch_qr_image(args.base_url, args.api_key, args.session, args.qr_dir / "qr.png")
            fetch_qr_value(args.base_url, args.api_key, args.session, args.qr_dir / "qr.txt")
            last_qr_refresh = time.time()
        time.sleep(args.poll_interval)
    emit({
        "primitive": "waha_session",
        "command": "wait",
        "status": "timeout",
        "session": args.session,
        "state": last_state,
        "timeout": args.wait_timeout,
    })
    return 1


def cmd_wait(args: argparse.Namespace) -> int:
    return _wait_until_working(args)


def cmd_stop(args: argparse.Namespace) -> int:
    logs = stop_session(args.base_url, args.api_key, args.session)
    final = session_state(args.base_url, args.api_key, args.session)
    emit({
        "primitive": "waha_session",
        "command": "stop",
        "status": "stopped",
        "session": args.session,
        "logs": logs,
        "state": final,
    })
    return 0


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key", default=DEFAULT_API_KEY)
    parser.add_argument("--session", default=DEFAULT_SESSION)


def main() -> None:
    parser = argparse.ArgumentParser(description="WAHA session + QR auth")
    sub = parser.add_subparsers(dest="command", required=True)

    health = sub.add_parser("health", help="Wait for WAHA HTTP to become healthy")
    add_common_args(health)
    health.add_argument("--timeout", type=int, default=180)
    health.set_defaults(func=cmd_health)

    status = sub.add_parser("status", help="Show current session status")
    add_common_args(status)
    status.set_defaults(func=cmd_status)

    start = sub.add_parser("start", help="Create session and emit QR code artifacts")
    add_common_args(start)
    start.add_argument("--qr-dir", type=Path, default=DEFAULT_QR_DIR)
    start.add_argument("--force", action="store_true", help="Stop and recreate session even if it exists")
    start.add_argument("--open", action="store_true", help="Open the saved QR PNG in the system viewer")
    start.add_argument("--wait", action="store_true", help="After starting, poll until WORKING")
    start.add_argument("--wait-timeout", type=int, default=180)
    start.add_argument("--poll-interval", type=float, default=3.0)
    start.add_argument("--health-timeout", type=int, default=180)
    start.set_defaults(func=cmd_start)

    wait = sub.add_parser("wait", help="Poll session status until WORKING (or timeout)")
    add_common_args(wait)
    wait.add_argument("--qr-dir", type=Path, default=DEFAULT_QR_DIR)
    wait.add_argument("--wait-timeout", type=int, default=180)
    wait.add_argument("--poll-interval", type=float, default=3.0)
    wait.set_defaults(func=cmd_wait)

    stop = sub.add_parser("stop", help="Stop and delete the session")
    add_common_args(stop)
    stop.set_defaults(func=cmd_stop)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
