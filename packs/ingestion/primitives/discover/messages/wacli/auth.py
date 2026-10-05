"""Link and unlink the WhatsApp account.

Flow: parse auth status -> QR authentication when needed -> report.
The scan starts wacli's history download, which runs on in its own session
(the phone sends history only while the device stays connected); linking
returns at the scan and `wait_for_history` waits for the download to finish.
Failure before linking requests a QR scan. Auth status and the QR run's result
retain typed fields until report serialization; the pairing state is the typed
`PairingStatus`.

Changelog:
  2026-10-05: linking returns at the scan; the history download it starts runs
    on detached, and callers that read the store `wait_for_history` first.
    (`--link-only` lost the history: the phone does not queue it for a device
    that disconnects.) `auth_bootstrap_sync_completed` and `returncode` are gone.
  2026-09-23 (typed rows): `run_auth_with_qr_page`/`run_auth` return the frozen
    `AuthRunResult` and `pairing_full_sync_status` returns `PairingStatus`, so
    `auth_report` reads typed fields instead of `.get(...)` on two dicts. Emitted
    values unchanged.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[6]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.discover.messages.wacli import binary, depth_db, pairing, qr, runtime  # noqa: E402
from packs.ingestion.primitives.discover.messages.wacli.paths import (  # noqa: E402
    DEFAULT_AUTH_LOG,
    DEFAULT_QR_HTML,
    DEFAULT_QR_PNG,
)
from packs.ingestion.primitives.discover.messages.wacli.payloads import AuthStatus  # noqa: E402
from packs.ingestion.primitives.discover.messages.wacli.runtime import PrimitiveBlocked  # noqa: E402
from packs.ingestion.primitives.discover.messages.wacli.util import linked_device_blocked  # noqa: E402

DEFAULT_IDLE_EXIT = os.environ.get("POWERPACKS_WACLI_IDLE_EXIT", "30s")
HISTORY_COUNT_SECONDS = 10
# How long one QR run waits for a scan.
DEFAULT_AUTH_TIMEOUT = int(os.environ.get("POWERPACKS_WACLI_AUTH_TIMEOUT", "10800"))


def auth_status(store: Path) -> AuthStatus:
    parsed = AuthStatus.from_payload(binary.wacli_json(store, ["auth", "status"], timeout=60))
    if parsed.authenticated:
        return parsed
    qr_page = str(DEFAULT_QR_HTML) if DEFAULT_QR_HTML.exists() else ""
    qr_png = ""
    qr_updated_at = ""
    if DEFAULT_QR_PNG.exists():
        qr_png = str(DEFAULT_QR_PNG)
        qr_updated_at = datetime.fromtimestamp(
            DEFAULT_QR_PNG.stat().st_mtime,
            timezone.utc,
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return replace(parsed, qr_page=qr_page, qr_png=qr_png, qr_updated_at=qr_updated_at)


@dataclass(frozen=True)
class AuthRunResult:
    """One `wacli auth` QR run's settled result, parsed once here so `auth_report`
    and the extractor read fields instead of a dict. `to_payload()` reproduces the
    emitted document key for key."""

    command: str
    qr_page: str
    qr_png: str
    connected_event: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "qr_page": self.qr_page,
            "qr_png": self.qr_png,
            "connected_event": self.connected_event,
        }


def run_auth_with_qr_page(store: Path, *, timeout: int, idle_exit: str, open_qr_page: bool) -> AuthRunResult:
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
    # The phone sends the history only while this device stays connected, so the
    # download wacli starts at the scan runs on in its own session after this returns.
    DEFAULT_AUTH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with DEFAULT_AUTH_LOG.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, text=True,
                                env=pairing.wacli_device_env(), start_new_session=True)
    output: list[str] = []
    opened = False
    connected = False
    deadline = time.time() + timeout

    def handle_line(text: str) -> None:
        nonlocal opened, connected
        output.append(text)
        if text.startswith("{"):
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                return
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            code = data.get("code")
            payload = qr.wa_qr_payload(code) if isinstance(code, str) else None
            if event.get("event") == "qr_code" and payload:
                qr.update_qr_page(payload, DEFAULT_QR_PNG, DEFAULT_QR_HTML, open_page=open_qr_page and not opened)
                opened = True
                runtime.emit_status("Refreshed WhatsApp QR page.")
            elif event.get("event") == "connected":
                connected = True
            return
        payload = qr.wa_qr_payload(text)
        if payload:
            qr.update_qr_page(payload, DEFAULT_QR_PNG, DEFAULT_QR_HTML, open_page=open_qr_page and not opened)
            opened = True
            runtime.emit_status("Refreshed WhatsApp QR page.")

    with DEFAULT_AUTH_LOG.open(encoding="utf-8") as log:
        pending = ""
        while not connected:
            chunk = log.readline()
            if chunk:
                pending += chunk
                if pending.endswith("\n"):
                    if pending.strip():
                        handle_line(pending.strip())
                    pending = ""
                continue
            if proc.poll() is not None:
                for line in (pending + log.read()).splitlines():
                    if line.strip():
                        handle_line(line.strip())
                break
            if time.time() > deadline:
                proc.kill()
                proc.wait()
                output.append(f"command timed out after {timeout}s")
                break
            time.sleep(0.2)
    joined = qr.redact_qr_payloads("\n".join(output))
    if linked_device_blocked(joined):
        raise PrimitiveBlocked({
            "status": "blocked_user_action",
            "message": "WhatsApp cannot link new devices right now. Try again later in WhatsApp, then rerun $import-messages.",
            "command": runtime.command_text(cmd),
        })
    if not connected:
        raise PrimitiveBlocked({
            "status": "blocked_user_action",
            "message": "WhatsApp needs a QR scan. Scan it, then rerun $import-messages.",
            "command": runtime.command_text(cmd),
            "qr_page": str(DEFAULT_QR_HTML),
            "qr_png": str(DEFAULT_QR_PNG),
            "detail": joined[-2000:],
        })
    return AuthRunResult(
        command=runtime.command_text(cmd),
        qr_page=str(DEFAULT_QR_HTML),
        qr_png=str(DEFAULT_QR_PNG),
        connected_event=connected,
    )


def run_auth(store: Path, *, timeout: int, idle_exit: str, open_qr_page: bool = True) -> AuthRunResult:
    return run_auth_with_qr_page(store, timeout=timeout, idle_exit=idle_exit, open_qr_page=open_qr_page)


def wait_for_history(store: Path, *, on_count: Callable[[int], None] | None = None) -> None:
    """Wait until no wacli holds the store: the history download started at the scan is done.
    `on_count(messages)` follows the download every HISTORY_COUNT_SECONDS."""
    with (store / "LOCK").open("a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if on_count:
                    on_count(depth_db.history_depth_total_count(store))
                time.sleep(HISTORY_COUNT_SECONDS)
        fcntl.flock(lock, fcntl.LOCK_UN)


def auth_report(
    store: Path,
    *,
    idle_exit: str = DEFAULT_IDLE_EXIT,
    auth_timeout: int = DEFAULT_AUTH_TIMEOUT,
    install: bool = True,
    open_qr_page: bool = True,
) -> dict[str, Any]:
    """Link the WhatsApp account (QR scan when needed) without exporting anything;
    a new link leaves its history download running. `status` is `linked` or
    `blocked_user_action`."""
    store.mkdir(parents=True, exist_ok=True)
    wacli_info = binary.ensure_wacli_installed(install=install)
    doctor = binary.wacli_json(store, ["doctor"], timeout=60)
    status_before = auth_status(store)
    auth_summary: dict[str, Any] = {
        "authenticated_before": status_before.authenticated,
        "ran_sync": False,
        "exported_contacts": False,
    }
    auth_run: AuthRunResult | None = None
    if not status_before.authenticated:
        auth_run = run_auth(
            store,
            timeout=auth_timeout,
            idle_exit=idle_exit,
            open_qr_page=open_qr_page,
        )
        auth_summary.update(auth_run.to_payload())
    status_after = auth_status(store)
    auth_summary["authenticated_after"] = status_after.authenticated
    linked = status_after.authenticated
    if not status_before.authenticated and linked:
        pairing.write_pairing_marker(store)  # we just paired with full sync
    pairing_state = pairing.pairing_full_sync_status(store, authenticated=linked)
    if pairing_state.pre_full_sync:
        runtime.emit_status(pairing_state.hint or "")
    return {
        "status": "linked" if linked else "blocked_user_action",
        "pairing": pairing_state.to_payload(),
        "message": (
            ("WhatsApp is linked. Your message history is downloading in the background."
             if auth_run else "WhatsApp is linked.")
            if linked
            else "WhatsApp needs a QR scan. Scan it, then rerun the auth command."
        ),
        "wacli": wacli_info,
        "doctor": doctor,
        "auth": auth_summary,
        "qr_page": status_after.qr_page or (auth_run.qr_page if auth_run else ""),
        "qr_png": status_after.qr_png or (auth_run.qr_png if auth_run else ""),
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
    authenticated_before = auth_status(store).authenticated
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
        "authenticated_after": auth_status(store).authenticated,
        "marker_removed": marker_removed,
        "result": result,
    }
