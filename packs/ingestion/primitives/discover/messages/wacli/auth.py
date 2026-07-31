"""Link (and unlink) the WhatsApp account: auth state, the QR run, the reports.

`auth_status` is the cheap read — one `wacli auth status --json` call, plus the
QR artifact paths when the store is not linked yet. `run_auth` is the expensive
one: it starts `wacli auth --events`, follows the event stream, re-renders the
login QR every time WhatsApp rotates it, and then keeps waiting while whatsmeow
does the initial account bootstrap (hours on a large archive) — a `connected`
event restarts the timeout window, and a non-zero exit before `connected` is a
"scan the QR" block rather than a failure.

`auth_report` and `logout_report` are the `auth` / `logout` subcommand payloads:
link without syncing or exporting anything, and invalidate the session so the
next auth issues a fresh QR (the pre-full-sync re-link flow).

Changelog:
  2026-07-30 (wacli split): extracted from the single-file `whatsapp_wacli.py`.
    The auth-status JSON is now parsed once into `payloads.AuthStatus`; QR
    rendering/redaction moved to `qr.py` and the device identity + full-sync
    marker to `pairing.py`. Emitted payloads unchanged.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[6]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.discover.messages.wacli import binary, pairing, qr, runtime  # noqa: E402
from packs.ingestion.primitives.discover.messages.wacli.paths import (  # noqa: E402
    DEFAULT_QR_HTML,
    DEFAULT_QR_PNG,
)
from packs.ingestion.primitives.discover.messages.wacli.payloads import AuthStatus  # noqa: E402
from packs.ingestion.primitives.discover.messages.wacli.runtime import (  # noqa: E402
    PrimitiveBlocked,
    PrimitiveFailed,
)
from packs.ingestion.primitives.discover.messages.wacli.util import linked_device_blocked  # noqa: E402

DEFAULT_IDLE_EXIT = os.environ.get("POWERPACKS_WACLI_IDLE_EXIT", "30s")
# A newly paired account keeps the auth process alive while whatsmeow completes
# its initial account bootstrap. That can take hours on a large archive.
DEFAULT_AUTH_TIMEOUT = int(os.environ.get("POWERPACKS_WACLI_AUTH_TIMEOUT", "10800"))


def auth_status(
    store: Path,
    *,
    include_linked_jid: bool = False,
) -> dict[str, Any]:
    parsed = AuthStatus.from_payload(binary.wacli_json(store, ["auth", "status"], timeout=60))
    status = {
        "authenticated": parsed.authenticated,
        "raw_success": parsed.raw_success,
        "error": parsed.error,
    }
    if include_linked_jid:
        status["linked_jid"] = parsed.linked_jid
    if not status["authenticated"]:
        if DEFAULT_QR_HTML.exists():
            status["qr_page"] = str(DEFAULT_QR_HTML)
        if DEFAULT_QR_PNG.exists():
            status["qr_png"] = str(DEFAULT_QR_PNG)
            status["qr_updated_at"] = datetime.fromtimestamp(
                DEFAULT_QR_PNG.stat().st_mtime,
                timezone.utc,
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return status


def run_auth_with_qr_page(store: Path, *, timeout: int, idle_exit: str, open_qr_page: bool) -> dict[str, Any]:
    if not shutil.which("qrencode"):
        raise PrimitiveBlocked({
            "status": "blocked_user_action",
            "message": "qrencode is required to render the WhatsApp QR page. Install it with `brew install qrencode`, then rerun $import-messages.",
            "install_command": "brew install qrencode",
        })
    runtime.emit_status("WhatsApp needs a QR scan.")
    qr.clear_qr_artifacts(DEFAULT_QR_HTML, DEFAULT_QR_PNG)
    cmd = [
        binary.wacli_bin() or "wacli",
        "--store", str(store),
        "--events",
        "auth",
        "--qr-format", "text",
        "--follow=false",
        "--idle-exit", idle_exit,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=pairing.wacli_device_env())
    output: list[str] = []
    opened = False
    connected = False
    deadline = time.time() + timeout

    lines: queue.Queue[tuple[str, str]] = queue.Queue()

    def read_stream(name: str, stream: Any) -> None:
        for line in stream:
            lines.put((name, line))

    stdout_thread = threading.Thread(target=read_stream, args=("stdout", proc.stdout), daemon=True)
    stderr_thread = threading.Thread(target=read_stream, args=("stderr", proc.stderr), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    def handle_line(source: str, text: str) -> None:
        nonlocal opened, connected, deadline
        output.append(text)
        event = None
        if source == "stderr" and text.startswith("{"):
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                event = None
        if isinstance(event, dict):
            event_name = event.get("event")
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            code = data.get("code")
            payload = qr.wa_qr_payload(code) if isinstance(code, str) else None
            if event_name == "qr_code" and payload:
                qr.update_qr_page(payload, DEFAULT_QR_PNG, DEFAULT_QR_HTML, open_page=open_qr_page and not opened)
                opened = True
                runtime.emit_status("Refreshed WhatsApp QR page.")
            elif event_name == "connected":
                if not connected:
                    # Give the initial archive bootstrap its own complete
                    # timeout window after the user finishes the QR step.
                    deadline = time.time() + timeout
                connected = True
            return
        stdout_payload = qr.wa_qr_payload(text) if source == "stdout" else None
        if stdout_payload:
            qr.update_qr_page(stdout_payload, DEFAULT_QR_PNG, DEFAULT_QR_HTML, open_page=open_qr_page and not opened)
            opened = True
            runtime.emit_status("Refreshed WhatsApp QR page.")

    try:
        while proc.poll() is None:
            if time.time() > deadline:
                proc.kill()
                output.append(f"command timed out after {timeout}s")
                break
            try:
                source, line = lines.get(timeout=0.1)
            except queue.Empty:
                continue
            text = line.strip()
            if text:
                handle_line(source, text)
    finally:
        returncode = proc.wait()
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
    while True:
        try:
            source, line = lines.get_nowait()
        except queue.Empty:
            break
        text = line.strip()
        if text:
            handle_line(source, text)
    joined = qr.redact_qr_payloads("\n".join(output))
    if linked_device_blocked(joined):
        raise PrimitiveBlocked({
            "status": "blocked_user_action",
            "message": "WhatsApp cannot link new devices right now. Try again later in WhatsApp, then rerun $import-messages.",
            "command": runtime.command_text(cmd),
        })
    if returncode != 0:
        if not connected:
            raise PrimitiveBlocked({
                "status": "blocked_user_action",
                "message": "WhatsApp needs a QR scan. Scan it, then rerun $import-messages.",
                "command": runtime.command_text(cmd),
                "qr_page": str(DEFAULT_QR_HTML),
                "qr_png": str(DEFAULT_QR_PNG),
                "detail": joined[-2000:],
            })
        raise PrimitiveFailed(
            "WhatsApp connected, but its initial history sync did not finish. "
            "Rerun $import-messages to try again."
        )
    return {
        "command": runtime.command_text(cmd),
        "returncode": returncode,
        "qr_page": str(DEFAULT_QR_HTML),
        "qr_png": str(DEFAULT_QR_PNG),
        "connected_event": connected,
        "auth_bootstrap_sync_completed": connected and returncode == 0,
    }


def run_auth(store: Path, *, timeout: int, idle_exit: str, open_qr_page: bool = True) -> dict[str, Any]:
    return run_auth_with_qr_page(store, timeout=timeout, idle_exit=idle_exit, open_qr_page=open_qr_page)


def auth_report(
    store: Path,
    *,
    idle_exit: str = DEFAULT_IDLE_EXIT,
    auth_timeout: int = DEFAULT_AUTH_TIMEOUT,
    install: bool = True,
    open_qr_page: bool = True,
) -> dict[str, Any]:
    """Link the WhatsApp account (QR scan when needed) without syncing or
    exporting anything; `status` is `linked` or `blocked_user_action`."""
    store.mkdir(parents=True, exist_ok=True)
    wacli_info = binary.ensure_wacli_installed(install=install)
    doctor = binary.wacli_json(store, ["doctor"], timeout=60)
    status_before = auth_status(store)
    auth_summary: dict[str, Any] = {
        "authenticated_before": status_before.get("authenticated"),
        "ran_sync": False,
        "exported_contacts": False,
    }
    if not status_before.get("authenticated"):
        auth_summary.update(run_auth(
            store,
            timeout=auth_timeout,
            idle_exit=idle_exit,
            open_qr_page=open_qr_page,
        ))
    status_after = auth_status(store)
    auth_summary["authenticated_after"] = status_after.get("authenticated")
    linked = bool(status_after.get("authenticated"))
    if not status_before.get("authenticated") and linked:
        pairing.write_pairing_marker(store)  # we just paired with full sync
    pairing_state = pairing.pairing_full_sync_status(store, authenticated=linked)
    if pairing_state.get("state") == "pre_full_sync":
        runtime.emit_status(pairing_state["hint"])
    return {
        "status": "linked" if linked else "blocked_user_action",
        "pairing": pairing_state,
        "message": (
            "WhatsApp account is linked. No WhatsApp sync or export was run."
            if linked
            else "WhatsApp needs a QR scan. Scan it, then rerun the auth command."
        ),
        "wacli": wacli_info,
        "doctor": doctor,
        "auth": auth_summary,
        "qr_page": status_after.get("qr_page") or auth_summary.get("qr_page") or "",
        "qr_png": status_after.get("qr_png") or auth_summary.get("qr_png") or "",
        "privacy": {
            "reads_message_bodies": False,
            "syncs_messages": False,
            "exports_contacts": False,
        },
    }


def logout_report(store: Path) -> dict[str, Any]:
    """Invalidate the WhatsApp session so the next auth issues a fresh QR. Backs
    the pre-full-sync re-link flow: an old (upstream/pre-full-sync) link is logged
    out here, then discovery re-pairs with full history sync. Idempotent on an
    already-logged-out store."""
    binary.ensure_wacli_installed(install=False)
    authenticated_before = bool(auth_status(store).get("authenticated"))
    result: dict[str, Any] = {}
    if authenticated_before:
        result = binary.wacli_json(store, ["auth", "logout"], timeout=60)
    marker_removed = False
    marker = pairing.pairing_marker_path(store)
    if marker.exists():
        marker.unlink()
        marker_removed = True
    return {
        "status": "ok",
        "authenticated_before": authenticated_before,
        "authenticated_after": bool(auth_status(store).get("authenticated")),
        "marker_removed": marker_removed,
        "result": result,
    }
