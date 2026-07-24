#!/usr/bin/env python3
"""wacli BINARY CLIENT for WhatsApp metadata sync (openclaw/wacli).

This is the lower layer of the WhatsApp discovery vertical (parallels
`gmail/msgvault/sync.py`): it owns the wacli GO binary lifecycle — download the
pinned fork, authenticate + render the login QR, run one metadata sync, deepen
recent shallow history in paced batches, refresh contacts/group info, and read
raw metadata rows out of the local wacli SQLite store. It is invoked in-process
by `extract_whatsapp.WhatsAppExtractor` (the extractor composes this client);
the wacli binary itself is still a subprocess (external tool).

The store lives under `.powerpacks/messages/wacli` (wacli keeps its own sync
state there). Every SQLite read selects only local metadata columns; it never
selects message body columns.

Stdlib-only.

Usage (the standalone lifecycle subcommands skills/tests invoke by path):
    whatsapp_wacli.py status       # show install/auth/store state
    whatsapp_wacli.py auth         # authenticate WhatsApp without syncing/exporting
    whatsapp_wacli.py ensure-wacli # download/refresh the pinned wacli binary
    whatsapp_wacli.py logout       # invalidate the session (re-link flow)

The discovery `run`/`export` entry points (install → auth → sync → deepen →
export contacts) live in `extract_whatsapp.py`, which imports this client.

Changelog:
- 2026-07-24 (dedup): the local `parse_last_json` fork was deleted; its
  scan-forward recovery was promoted into `common/jsonio.parse_last_json`,
  which this module now imports (results are `{}` rather than `None` when no
  object decodes — every consumer here already coerced non-dicts to `{}`).
  `run_command` is PINNED as deliberately divergent from `common/proc.run_cmd`
  (see the reasons at its definition); its never-passed `env` and
  `stream_to_stderr` parameters were dropped. The CLI lost its
  `set_defaults(func=...)`/`args.func(args)` dispatcher: the four subcommands
  are payload-returning functions with explicit parameters, dispatched inline
  by `main()`.
- 2026-07-23 (extractor split): the `Contact` dataclass, the store→CSV/JSONL
  parse/write logic, the `WhatsAppWacli` orchestrator (now `WhatsAppExtractor`),
  and the `run`/`export` CLI subcommands moved to `extract_whatsapp.py`. This
  module keeps the wacli binary lifecycle (install/auth/QR/sync/history-depth/
  group-info) and its standalone `status`/`auth`/`ensure-wacli`/`logout`
  subcommands. The dead `store_message_count` helper was dropped. Import is
  one-directional: `extract_whatsapp` → `whatsapp_wacli`.
- 2026-07-23 (in-process): the outer `run` entry moved onto a class the WhatsApp
  channel calls in-process instead of spawning this file. The wacli GO BINARY is
  still invoked as a subprocess (external tool).
- 2026-07-23: whatsapp_wacli.README.md sidecar folded into this docstring.
- 2026-07-23: The isolated WhatsApp wrapper skill was retired; user-facing
  rerun hints now point at $import-messages and the status/User-Agent
  identifiers name this primitive directly.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import platform
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import emit, now_iso, parse_last_json, write_json  # noqa: E402
from packs.ingestion.primitives.common.manifests import write_stage_manifest  # noqa: E402
from packs.ingestion.primitives.common.paths import MESSAGES_OUT_DIR  # noqa: E402
from packs.shared.csv_io import CsvIO  # noqa: E402


DEFAULT_OUT_DIR = MESSAGES_OUT_DIR
DEFAULT_STORE = DEFAULT_OUT_DIR / "wacli"
DEFAULT_HISTORY_DEPTH_DIR = DEFAULT_OUT_DIR / "history-depth"
DEFAULT_MAX_MESSAGES = int(os.environ.get("POWERPACKS_WACLI_MAX_MESSAGES", "0"))
# Store-size target used after the first sync: existing messages + headroom for
# new ones (headroom = max(1000, budget // 10) via effective_max_messages).
# The primitive chooses full vs incremental from the local store; there is no
# user-facing sync mode.
DEFAULT_INCREMENTAL_BUDGET = int(os.environ.get("POWERPACKS_WACLI_INCREMENTAL_BUDGET", "20000"))
DEFAULT_QR_PNG = DEFAULT_OUT_DIR / "wacli-login-qr.png"
DEFAULT_QR_HTML = DEFAULT_OUT_DIR / "wacli-login-qr.html"
DEFAULT_GROUP_PARTICIPANTS_CACHE = DEFAULT_OUT_DIR / "wacli.group-participants.json"
DEFAULT_IDLE_EXIT = os.environ.get("POWERPACKS_WACLI_IDLE_EXIT", "30s")
# A newly paired account keeps the auth process alive while whatsmeow completes
# its initial account bootstrap. That can take hours on a large archive.
DEFAULT_AUTH_TIMEOUT = int(os.environ.get("POWERPACKS_WACLI_AUTH_TIMEOUT", "10800"))
DEFAULT_SYNC_TIMEOUT = int(os.environ.get("POWERPACKS_WACLI_SYNC_TIMEOUT", "10800"))
DEFAULT_HISTORY_DEPTH_MAX_COUNT = int(os.environ.get("POWERPACKS_WACLI_DEPTH_MAX_COUNT", "20"))
DEFAULT_HISTORY_DEPTH_COUNT = int(os.environ.get("POWERPACKS_WACLI_DEPTH_COUNT", "500"))
DEFAULT_HISTORY_DEPTH_REQUESTS = int(os.environ.get("POWERPACKS_WACLI_DEPTH_REQUESTS", "10"))
DEFAULT_HISTORY_DEPTH_NO_GROWTH_LIMIT = int(os.environ.get("POWERPACKS_WACLI_DEPTH_NO_GROWTH_LIMIT", "1"))
DEFAULT_HISTORY_DEPTH_REQUEST_DELAY = os.environ.get("POWERPACKS_WACLI_DEPTH_REQUEST_DELAY", "10s")
DEFAULT_HISTORY_DEPTH_BATCH_SIZE = int(os.environ.get("POWERPACKS_WACLI_DEPTH_BATCH_SIZE", "10"))
DEFAULT_HISTORY_DEPTH_MAX_IN_FLIGHT = int(
    os.environ.get("POWERPACKS_WACLI_DEPTH_MAX_IN_FLIGHT", "10")
)
DEFAULT_HISTORY_DEPTH_RESPONSE_WAIT = os.environ.get("POWERPACKS_WACLI_DEPTH_RESPONSE_WAIT", "10s")
DEFAULT_HISTORY_DEPTH_BATCH_DELAY = os.environ.get(
    "POWERPACKS_WACLI_DEPTH_BATCH_DELAY",
    "10s",
)
DEFAULT_HISTORY_DEPTH_TIMEOUT_BACKOFF = os.environ.get(
    "POWERPACKS_WACLI_DEPTH_TIMEOUT_BACKOFF",
    "1m",
)
DEFAULT_HISTORY_DEPTH_BUDGET_SECONDS = int(os.environ.get("POWERPACKS_WACLI_DEPTH_BUDGET_SECONDS", "6300"))
DEFAULT_HISTORY_DEPTH_ATTEMPT_TIMEOUT = int(
    os.environ.get(
        "POWERPACKS_WACLI_DEPTH_ATTEMPT_TIMEOUT",
        str(DEFAULT_HISTORY_DEPTH_BUDGET_SECONDS),
    )
)
DEFAULT_HISTORY_DEPTH_LOOKBACK_YEARS = 3
HISTORY_DEPTH_POLICY_VERSION = 4
DEFAULT_DEVICE_PLATFORM = os.environ.get("POWERPACKS_WACLI_DEVICE_PLATFORM", "DESKTOP")
DEFAULT_DEVICE_LABEL = os.environ.get("POWERPACKS_WACLI_DEVICE_LABEL", "Mac OS")
DEFAULT_FULL_SYNC_DAYS = os.environ.get("POWERPACKS_WACLI_FULL_SYNC_DAYS", "3650")
# Written into the store when OUR flow pairs (which always sends RequireFullSync).
# A linked session missing this marker was paired the old way — upstream wacli or
# a pre-full-sync build — and would pull years more history if re-linked. There's
# no reliable way to read "was RequireFullSync sent?" back out of session.db, so
# we stamp it at pair time instead.
PAIRING_MARKER_NAME = ".powerpacks-pairing.json"
# Pinned powerset-co fork of wacli that forces a full history sync at pairing
# (RequireFullSync). We download a prebuilt binary from the fork's GitHub Release
# for this tag — no Go toolchain on the user's machine — and keep it off the
# upstream Homebrew tap so we control the version.
WACLI_REPO = "powerset-co/wacli"
WACLI_PINNED_VERSION = os.environ.get("POWERPACKS_WACLI_VERSION", "v0.14.0-fullsync")
WACLI_RELEASE_BASE = os.environ.get(
    "POWERPACKS_WACLI_RELEASE_BASE",
    f"https://github.com/{WACLI_REPO}/releases/download",
)
WACLI_BIN_DIR = Path(os.environ.get("POWERPACKS_WACLI_BIN_DIR", str(Path.home() / ".powerpacks" / "bin")))
WACLI_PINNED_BIN = WACLI_BIN_DIR / "wacli"
# Records which pinned tag the installed binary was built from. `wacli --version`
# only reports the upstream semver (e.g. "0.13.0"), not our fork tag, so we can't
# read the pin off the binary — stamp it at install time and compare on every run
# so a bumped WACLI_PINNED_VERSION triggers a rebuild instead of silently using
# the stale binary.
WACLI_VERSION_STAMP = WACLI_BIN_DIR / ".wacli-version"
QR_REDACTION = "[whatsapp qr payload redacted]"
STATUS_PREFIX = "[whatsapp-wacli]"
MIN_PHONE_DIGITS = 7
MAX_PHONE_DIGITS = 15
BODY_COLUMN_NAMES = {
    "text",
    "display_text",
    "media_caption",
    "filename",
    "direct_path",
    "media_key",
    "file_sha256",
    "file_enc_sha256",
    "local_path",
}


@dataclass(frozen=True)
class HistoryDepthTarget:
    chat_jid: str
    chat_ref: str
    kind: str
    current_count: int
    current_latest_ts: int = 0
    state_changed: bool = False


@dataclass(frozen=True)
class HistoryDepthAttempt:
    returncode: int
    requests_sent: int
    responses_seen: int
    target_added: int
    unrelated_added: int
    after_count: int
    error_category: str
    retryable: bool
    after_latest_ts: int = 0
    messages_received: int = 0
    end_type: str = ""


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


def wacli_bin() -> str | None:
    """Resolve the wacli binary, preferring our pinned fork install so a stray
    PATH wacli (e.g. the upstream Homebrew tap) can never shadow it."""
    if WACLI_PINNED_BIN.exists() and os.access(WACLI_PINNED_BIN, os.X_OK):
        return str(WACLI_PINNED_BIN)
    return shutil.which("wacli")


def installed_wacli_version() -> str | None:
    try:
        return WACLI_VERSION_STAMP.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def wacli_pinned_current() -> bool:
    """True when the pinned fork binary is present AND was built from the version
    we currently pin to (so a bumped pin counts as needing a reinstall)."""
    return (
        WACLI_PINNED_BIN.exists()
        and os.access(WACLI_PINNED_BIN, os.X_OK)
        and installed_wacli_version() == WACLI_PINNED_VERSION
    )


def wacli_version(timeout: int = 30) -> dict[str, Any]:
    exe = wacli_bin()
    if not exe:
        raise PrimitiveFailed("wacli is not installed")
    result = run_command([exe, "--version"], timeout=timeout)
    version = (result.get("stdout") or "").strip()
    if result["returncode"] != 0 or not version:
        raise PrimitiveFailed(((result.get("stderr") or result.get("stdout") or "").strip())[-1000:])
    return {"path": exe, "version": version, "pinned": exe == str(WACLI_PINNED_BIN)}


def wacli_asset_name() -> str | None:
    """Release asset name for this platform, e.g. `wacli-darwin-arm64`, or None
    if we don't publish a prebuilt for it."""
    os_name = {"darwin": "darwin", "linux": "linux"}.get(platform.system().lower())
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "amd64": "amd64"}.get(platform.machine().lower())
    if not os_name or not arch:
        return None
    return f"wacli-{os_name}-{arch}"


def wacli_download_url() -> str | None:
    asset = wacli_asset_name()
    return f"{WACLI_RELEASE_BASE}/{WACLI_PINNED_VERSION}/{asset}" if asset else None


def download_file(url: str, dest: Path, *, timeout: int = 120) -> None:
    """Stream a URL to dest via a temp file + atomic replace (GitHub release URLs
    redirect to blob storage; urlopen follows redirects)."""
    tmp = dest.with_name(dest.name + ".download")
    request = urllib.request.Request(url, headers={"User-Agent": "powerpacks-whatsapp-wacli"})
    with urllib.request.urlopen(request, timeout=timeout) as response, tmp.open("wb") as handle:
        shutil.copyfileobj(response, handle)
    tmp.replace(dest)


def ensure_wacli_installed(*, install: bool = True) -> dict[str, Any]:
    # The pinned fork at the currently-pinned version is the only thing that gives
    # the full history sync. We download the prebuilt binary for this platform from
    # the fork's GitHub Release whenever it's missing or stale — no toolchain on the
    # machine, no prompt (the `install` flag is retained for signature compatibility
    # but no longer gates this).
    if wacli_pinned_current():
        return wacli_version()
    stale = WACLI_PINNED_BIN.exists()  # present, but a different (older) pinned tag
    url = wacli_download_url()
    if not url:
        raise PrimitiveBlocked({
            "status": "blocked_user_action",
            "message": (
                f"No prebuilt wacli for this platform "
                f"({platform.system()}/{platform.machine()}). Build it from "
                f"{WACLI_REPO} @ {WACLI_PINNED_VERSION}, place it at {WACLI_PINNED_BIN}, "
                f"and write {WACLI_PINNED_VERSION} to {WACLI_VERSION_STAMP}."
            ),
        })
    emit_status(f"{'Updating' if stale else 'Installing'} WhatsApp sync helper ({WACLI_PINNED_VERSION}).")
    WACLI_BIN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        download_file(url, WACLI_PINNED_BIN)
    except Exception as exc:
        raise PrimitiveBlocked({
            "status": "blocked_user_action",
            "message": (
                f"Failed to download the pinned wacli binary from {url}: {exc}. "
                "Check network access, then rerun $import-messages."
            ),
            "install_command": f"curl -fsSL {url} -o {WACLI_PINNED_BIN} && chmod +x {WACLI_PINNED_BIN}",
        }) from exc
    WACLI_PINNED_BIN.chmod(0o755)
    info = wacli_version()  # verify the download actually runs before trusting it
    WACLI_VERSION_STAMP.write_text(WACLI_PINNED_VERSION + "\n", encoding="utf-8")
    return info


def wacli_json(store: Path, args: list[str], *, timeout: int = 300) -> dict[str, Any]:
    cmd = [wacli_bin() or "wacli", "--store", str(store), "--json", *args]
    result = run_command(cmd, timeout=timeout)
    payload = result.get("json")
    if result["returncode"] != 0:
        raise PrimitiveFailed(((result.get("stderr") or result.get("stdout") or "").strip())[-1000:])
    return payload if isinstance(payload, dict) else {}


def auth_status(
    store: Path,
    *,
    include_linked_jid: bool = False,
) -> dict[str, Any]:
    payload = wacli_json(store, ["auth", "status"], timeout=60)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    status = {
        "authenticated": bool(data.get("authenticated")),
        "raw_success": payload.get("success"),
        "error": payload.get("error"),
    }
    if include_linked_jid:
        status["linked_jid"] = str(data.get("linked_jid") or "")
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


def pairing_marker_path(store: Path) -> Path:
    return store / PAIRING_MARKER_NAME


def write_pairing_marker(store: Path) -> None:
    """Record that this session was paired by our full-sync flow. Call on the
    not-authenticated -> authenticated transition (i.e. when WE just paired)."""
    write_json(pairing_marker_path(store), {
        "full_sync": True,
        "full_sync_days": DEFAULT_FULL_SYNC_DAYS,
        "wacli_version": WACLI_PINNED_VERSION,
        "device_platform": os.environ.get("WACLI_DEVICE_PLATFORM", DEFAULT_DEVICE_PLATFORM),
        "paired_at": now_iso(),
    })


def read_pairing_marker(store: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(pairing_marker_path(store).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def pairing_full_sync_status(store: Path, *, authenticated: bool) -> dict[str, Any]:
    """Whether the current WhatsApp link was set up with full history sync. A
    linked session with no full-sync marker predates our full-sync flow (upstream
    wacli or an old build), so re-linking would pull years more history."""
    if not authenticated:
        return {"state": "not_authenticated", "can_deepen": False}
    marker = read_pairing_marker(store)
    if marker and marker.get("full_sync"):
        return {
            "state": "full_sync",
            "can_deepen": False,
            "paired_wacli_version": marker.get("wacli_version"),
            "paired_at": marker.get("paired_at"),
        }
    return {
        "state": "pre_full_sync",
        "can_deepen": True,
        "hint": (
            "This WhatsApp link was set up before full history sync. Re-link "
            "(log out and re-scan the QR) to pull years more history."
        ),
    }


def linked_device_blocked(text: str) -> bool:
    lowered = text.lower()
    return (
        "can't link new devices right now" in lowered
        or "cannot link new devices right now" in lowered
        or ("link" in lowered and "device" in lowered and "try again later" in lowered)
        or "cannot link more devices" in lowered
    )


def write_qr_html(path: Path, png_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rel_png = html.escape(png_path.name, quote=True)
    path.write_text(
        "<!doctype html>\n"
        "<html><head><meta charset=\"utf-8\">"
        "<meta http-equiv=\"refresh\" content=\"2\">"
        "<title>WhatsApp QR</title>"
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;"
        "display:flex;align-items:center;justify-content:center;min-height:100vh;"
        "margin:0;background:#f7f7f7;color:#111}"
        "main{text-align:center}img{width:min(82vw,620px);height:auto;"
        "background:white;padding:24px;border-radius:12px}"
        "p{font-size:18px;margin:16px 0 0}</style>"
        "</head><body><main>"
        f"<img src=\"{rel_png}\" alt=\"WhatsApp QR code\">"
        "<p>Scan with WhatsApp > Settings > Linked Devices</p>"
        "</main></body></html>\n",
        encoding="utf-8",
    )


WA_QR_URL_MARKER = "wa.me/settings/linked_devices#2@"


def wa_qr_payload(text: str) -> str | None:
    """Return the exact QR payload to encode if text is a WhatsApp linked-device
    QR, else None. wacli <=0.11 emits a bare `2@...` ref; wacli 0.13 emits a
    `https://wa.me/settings/linked_devices#2@...` URL. WhatsApp must receive
    exactly what wacli emits, so encode the whole string either way (encoding
    only the trailing `2@` fragment of the 0.13 URL is what broke pairing)."""
    stripped = text.strip()
    if stripped.startswith("2@"):
        return stripped
    idx = stripped.find("https://wa.me/")
    if idx != -1 and WA_QR_URL_MARKER in stripped:
        return stripped[idx:].split()[0]
    return None


def redact_qr_payloads(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if wa_qr_payload(stripped) or '"event":"qr_code"' in stripped or '"event": "qr_code"' in stripped:
            lines.append(QR_REDACTION)
        else:
            lines.append(line)
    return "\n".join(lines)


def clear_qr_artifacts(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            continue


def update_qr_page(payload: str, png_path: Path, html_path: Path, *, open_page: bool) -> None:
    qrencode = shutil.which("qrencode")
    if not qrencode:
        raise PrimitiveBlocked({
            "status": "blocked_user_action",
            "message": "qrencode is required to render the WhatsApp QR page. Install it with `brew install qrencode`, then rerun $import-messages.",
            "install_command": "brew install qrencode",
        })
    png_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run([qrencode, "-s", "10", "-m", "4", "-o", str(png_path), payload], check=True)
    except subprocess.CalledProcessError as exc:
        raise PrimitiveFailed(f"failed to render WhatsApp QR page with qrencode: {exc}") from exc
    write_qr_html(html_path, png_path)
    if open_page and shutil.which("open"):
        subprocess.run(["open", str(html_path)], check=False)


def wacli_device_env() -> dict[str, str]:
    """Device identity WhatsApp records at PAIRING time only (re-pair to change it).

    Only PlatformType DESKTOP makes WhatsApp's Linked Devices list render the OS
    label we set (WACLI_DEVICE_LABEL). whatsmeow's other platform enum names are
    reverse-engineered guesses whose *numbers* WhatsApp maps to its own fixed
    device names, ignoring the label (e.g. CATALINA/12 currently shows as
    "Portal TV", not macOS). See tulir/whatsmeow discussion #469. DESKTOP + a
    "Mac OS" label registers as a desktop and displays as macOS. Pre-set
    WACLI_DEVICE_* values in the environment win.
    """
    env = dict(os.environ)
    env.setdefault("WACLI_DEVICE_PLATFORM", DEFAULT_DEVICE_PLATFORM)
    env.setdefault("WACLI_DEVICE_LABEL", DEFAULT_DEVICE_LABEL)
    env.setdefault("WACLI_DEVICE_FULL_SYNC_DAYS", DEFAULT_FULL_SYNC_DAYS)
    return env


def run_auth_with_qr_page(store: Path, *, timeout: int, idle_exit: str, open_qr_page: bool) -> dict[str, Any]:
    if not shutil.which("qrencode"):
        raise PrimitiveBlocked({
            "status": "blocked_user_action",
            "message": "qrencode is required to render the WhatsApp QR page. Install it with `brew install qrencode`, then rerun $import-messages.",
            "install_command": "brew install qrencode",
        })
    emit_status("WhatsApp needs a QR scan.")
    clear_qr_artifacts(DEFAULT_QR_HTML, DEFAULT_QR_PNG)
    cmd = [
        wacli_bin() or "wacli",
        "--store", str(store),
        "--events",
        "auth",
        "--qr-format", "text",
        "--follow=false",
        "--idle-exit", idle_exit,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=wacli_device_env())
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
            payload = wa_qr_payload(code) if isinstance(code, str) else None
            if event_name == "qr_code" and payload:
                update_qr_page(payload, DEFAULT_QR_PNG, DEFAULT_QR_HTML, open_page=open_qr_page and not opened)
                opened = True
                emit_status("Refreshed WhatsApp QR page.")
            elif event_name == "connected":
                if not connected:
                    # Give the initial archive bootstrap its own complete
                    # timeout window after the user finishes the QR step.
                    deadline = time.time() + timeout
                connected = True
            return
        stdout_payload = wa_qr_payload(text) if source == "stdout" else None
        if stdout_payload:
            update_qr_page(stdout_payload, DEFAULT_QR_PNG, DEFAULT_QR_HTML, open_page=open_qr_page and not opened)
            opened = True
            emit_status("Refreshed WhatsApp QR page.")

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
    joined = redact_qr_payloads("\n".join(output))
    if linked_device_blocked(joined):
        raise PrimitiveBlocked({
            "status": "blocked_user_action",
            "message": "WhatsApp cannot link new devices right now. Try again later in WhatsApp, then rerun $import-messages.",
            "command": command_text(cmd),
        })
    if returncode != 0:
        if not connected:
            raise PrimitiveBlocked({
                "status": "blocked_user_action",
                "message": "WhatsApp needs a QR scan. Scan it, then rerun $import-messages.",
                "command": command_text(cmd),
                "qr_page": str(DEFAULT_QR_HTML),
                "qr_png": str(DEFAULT_QR_PNG),
                "detail": joined[-2000:],
            })
        raise PrimitiveFailed(
            "WhatsApp connected, but its initial history sync did not finish. "
            "Rerun $import-messages to try again."
        )
    return {
        "command": command_text(cmd),
        "returncode": returncode,
        "qr_page": str(DEFAULT_QR_HTML),
        "qr_png": str(DEFAULT_QR_PNG),
        "connected_event": connected,
        "auth_bootstrap_sync_completed": connected and returncode == 0,
    }


def run_auth(store: Path, *, timeout: int, idle_exit: str, open_qr_page: bool = True) -> dict[str, Any]:
    return run_auth_with_qr_page(store, timeout=timeout, idle_exit=idle_exit, open_qr_page=open_qr_page)


def effective_max_messages(requested: int, existing: int) -> int:
    if requested <= 0:
        return 0
    return max(requested, existing + max(1000, requested // 10))


def resolve_effective_max(requested: int, existing: int) -> int:
    """Choose full on an empty store and incremental once it is populated."""
    if requested and requested > 0:
        return effective_max_messages(requested, existing)
    if existing > 0:
        return effective_max_messages(DEFAULT_INCREMENTAL_BUDGET, existing)
    return 0


def run_sync(store: Path, *, timeout: int, idle_exit: str, max_messages: int) -> dict[str, Any]:
    emit_status("Syncing WhatsApp Messages and Contacts.")
    cmd = [
        wacli_bin() or "wacli",
        "--store", str(store),
        "sync",
        "--once",
        "--idle-exit", idle_exit,
        "--refresh-contacts",
        "--refresh-groups",
        "--max-messages", str(max_messages),
    ]
    result = run_command(
        cmd,
        timeout=timeout,
        heartbeat_message="Syncing WhatsApp Messages and Contacts.",
    )
    text = f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}"
    if linked_device_blocked(text):
        raise PrimitiveBlocked({
            "status": "blocked_user_action",
            "message": "WhatsApp cannot link new devices right now. Try again later in WhatsApp, then rerun $import-messages.",
            "command": command_text(cmd),
        })
    if result["returncode"] != 0:
        detail = text.strip()[-2000:] or "no wacli output captured"
        raise PrimitiveFailed(
            f"sync failed rc={result['returncode']} timeout={timeout}s max_messages={max_messages}; "
            f"command={command_text(cmd)}; output={detail}"
        )
    return {"command": command_text(cmd), "returncode": result["returncode"], "max_messages": max_messages, "timeout": timeout}


def refresh_contacts(store: Path) -> dict[str, Any]:
    try:
        payload = wacli_json(store, ["contacts", "refresh"], timeout=300)
    except PrimitiveFailed as exc:
        return {"status": "warning", "error": str(exc)}
    return {"status": "ok", "payload": payload}


def group_chat_jids(store: Path) -> list[str]:
    db_path = store / "wacli.db"
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if not table_exists(conn, "chats"):
            return []
        rows = select_rows(conn, "SELECT jid FROM chats WHERE kind = 'group' OR jid LIKE '%@g.us' ORDER BY jid")
        return [str(row["jid"]) for row in rows if row["jid"]]
    finally:
        conn.close()


def refresh_group_info(store: Path, *, timeout: int, min_interval: float) -> dict[str, Any]:
    jids = group_chat_jids(store)
    cache_path = group_participants_cache_path(store)
    cache = {
        "version": 1,
        "updated_at": now_iso(),
        "groups": {},
    }
    summary = {
        "status": "ok",
        "group_chats": len(jids),
        "refreshed": 0,
        "not_participating": 0,
        "failed": 0,
        "cached_groups": 0,
        "cached_participants": 0,
    }
    for jid in jids:
        result = run_command(
            [wacli_bin() or "wacli", "--store", str(store), "--json", "groups", "info", "--jid", jid],
            timeout=timeout,
        )
        text = (result.get("stderr") or result.get("stdout") or "").lower()
        if result["returncode"] == 0:
            summary["refreshed"] += 1
            group = normalize_group_info_payload(result.get("json") or {})
            if group:
                cache["groups"][group["jid"]] = group
                summary["cached_groups"] += 1
                summary["cached_participants"] += len(group.get("participants") or [])
        elif "not participating" in text or "not a participant" in text:
            summary["not_participating"] += 1
        else:
            summary["failed"] += 1
        if min_interval > 0:
            time.sleep(min_interval)
    if summary["failed"]:
        summary["status"] = "warning"
    write_json(cache_path, cache)
    summary["cache"] = str(cache_path)
    return summary


def group_participants_cache_path(store: Path) -> Path:
    if store == DEFAULT_STORE:
        return DEFAULT_GROUP_PARTICIPANTS_CACHE
    return store.parent / f"{store.name}.group-participants.json"


def normalize_group_info_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return None
    jid = str(data.get("JID") or "")
    if not jid:
        return None
    participants = []
    for raw in data.get("Participants") or []:
        if not isinstance(raw, dict):
            continue
        phone = canonicalize_phone(str(raw.get("PhoneNumber") or ""))
        if not phone:
            phone = jid_to_phone(str(raw.get("JID") or "")) or ""
        if not phone:
            continue
        participants.append({
            "phone": phone,
            "name": clean_name(raw.get("DisplayName")),
        })
    return {
        "jid": jid,
        "name": clean_name(data.get("Name")),
        "participant_count": int(data.get("ParticipantCount") or len(participants)),
        "participants": participants,
    }


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not table_exists(conn, table):
        return set()
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


def assert_metadata_query(sql: str) -> None:
    lowered = sql.lower()
    for name in BODY_COLUMN_NAMES:
        if re.search(rf"\b{name}\b", lowered):
            raise PrimitiveFailed(f"internal error: query selects body column {name}")


def select_rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    assert_metadata_query(sql)
    return list(conn.execute(sql, params))


def canonicalize_phone(raw: str | None) -> str:
    value = (raw or "").strip()
    if "@" in value:
        return jid_to_phone(value) or ""
    digits = re.sub(r"[^\d]", "", value)
    if len(digits) < MIN_PHONE_DIGITS:
        return ""
    if value.startswith("+"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) <= MAX_PHONE_DIGITS:
        return f"+{digits}"
    return ""


def jid_to_phone(jid: str | None) -> str | None:
    value = (jid or "").strip()
    if not value or "@g.us" in value or "@lid" in value or "@newsletter" in value:
        return None
    match = re.match(r"(\d+)@", value)
    if not match:
        if "@" not in value:
            return canonicalize_phone(value) or None
        return None
    digits = match.group(1)
    if MIN_PHONE_DIGITS <= len(digits) <= MAX_PHONE_DIGITS:
        return f"+{digits}"
    return None


def clean_name(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def open_wacli_db(store: Path) -> sqlite3.Connection:
    db_path = store / "wacli.db"
    if not db_path.exists():
        raise PrimitiveFailed(f"wacli database not found at {db_path}")
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


HISTORY_DEPTH_HEADERS = [
    "chat_ref",
    "kind",
    "initial_count",
    "current_count",
    "current_latest_ts",
    "target_rows_added",
    "unrelated_rows_added",
    "attempts",
    "requests_sent",
    "responses_seen",
    "transient_failures",
    "no_growth_attempts",
    "outcome",
    "error_category",
    "updated_at",
]
HISTORY_DEPTH_TERMINAL_OUTCOMES = {
    "completed_threshold",
    "recovered",
    "server_zero",
    "gone",
    "out_of_scope",
}
HISTORY_DEPTH_MORE_REMAIN_END_TYPES = {
    "COMPLETE_BUT_MORE_MESSAGES_REMAIN_ON_PRIMARY",
    "COMPLETE_ON_DEMAND_SYNC_BUT_MORE_MSG_REMAIN_ON_PRIMARY",
}


def history_chat_ref(jid: str) -> str:
    return "wa-" + hashlib.sha256(jid.encode("utf-8")).hexdigest()[:16]


def history_depth_cutoff_ts(now: datetime | None = None) -> int:
    current = now or datetime.now(timezone.utc)
    try:
        cutoff = current.replace(year=current.year - DEFAULT_HISTORY_DEPTH_LOOKBACK_YEARS)
    except ValueError:
        cutoff = current.replace(
            year=current.year - DEFAULT_HISTORY_DEPTH_LOOKBACK_YEARS,
            day=28,
        )
    return int(cutoff.timestamp())


def history_depth_visible_predicates(conn: sqlite3.Connection, alias: str = "m") -> list[str]:
    columns = table_columns(conn, "messages")
    predicates: list[str] = []
    if "revoked" in columns:
        predicates.append(f"COALESCE({alias}.revoked, 0) = 0")
    if "deleted_for_me" in columns:
        predicates.append(f"COALESCE({alias}.deleted_for_me, 0) = 0")
    return predicates


def history_depth_direct_predicates(
    *,
    chat_alias: str = "c",
    message_alias: str = "m",
) -> list[str]:
    return [
        f"COALESCE({chat_alias}.kind, 'unknown') <> 'group'",
        f"{message_alias}.chat_jid NOT LIKE '%@g.us'",
        f"{message_alias}.chat_jid NOT LIKE '%@newsletter'",
        (
            f"({message_alias}.chat_jid LIKE '%@s.whatsapp.net' "
            f"OR {message_alias}.chat_jid LIKE '%@lid')"
        ),
    ]


def history_depth_chat_states(store: Path) -> dict[str, tuple[int, int]]:
    if not (store / "wacli.db").exists():
        return {}
    conn = open_wacli_db(store)
    try:
        if not table_exists(conn, "messages") or not table_exists(conn, "chats"):
            return {}
        visibility = history_depth_visible_predicates(conn)
        where_sql = " AND ".join([
            *history_depth_direct_predicates(),
            *visibility,
        ])
        rows = conn.execute(
            f"""
            SELECT m.chat_jid, COUNT(*) AS message_count, MAX(m.ts) AS latest_ts
            FROM messages m
            JOIN chats c ON c.jid = m.chat_jid
            WHERE {where_sql}
            GROUP BY m.chat_jid
            """,
        ).fetchall()
        return {
            str(row["chat_jid"]): (
                int(row["message_count"]),
                int(row["latest_ts"] or 0),
            )
            for row in rows
        }
    finally:
        conn.close()


def history_depth_total_count(store: Path) -> int:
    if not (store / "wacli.db").exists():
        return 0
    conn = open_wacli_db(store)
    try:
        if not table_exists(conn, "messages"):
            return 0
        row = conn.execute("SELECT COUNT(*) FROM messages").fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def history_depth_targets(
    store: Path,
    *,
    active_since_ts: int,
    max_count: int = DEFAULT_HISTORY_DEPTH_MAX_COUNT,
    before_states: dict[str, tuple[int, int]] | None = None,
    bootstrap: bool = False,
    resume_refs: set[str] | None = None,
    exclude_jids: set[str] | None = None,
) -> list[HistoryDepthTarget]:
    previous = before_states or {}
    resumable = resume_refs or set()
    excluded = exclude_jids or set()
    conn = open_wacli_db(store)
    try:
        visibility = history_depth_visible_predicates(conn)
        where_sql = " AND ".join([
            *history_depth_direct_predicates(),
            *visibility,
        ])
        rows = conn.execute(
            f"""
            SELECT
                m.chat_jid,
                c.kind,
                COUNT(*) AS message_count,
                MAX(m.ts) AS latest_ts
            FROM messages m
            JOIN chats c ON c.jid = m.chat_jid
            WHERE {where_sql}
            GROUP BY m.chat_jid, c.kind
            HAVING COUNT(*) <= ? AND MAX(m.ts) >= ?
            ORDER BY MAX(m.ts) DESC, m.chat_jid
            """,
            (max_count, active_since_ts),
        ).fetchall()
        targets: list[HistoryDepthTarget] = []
        for row in rows:
            chat_jid = str(row["chat_jid"])
            if chat_jid in excluded:
                continue
            chat_ref = history_chat_ref(chat_jid)
            current_state = (
                int(row["message_count"]),
                int(row["latest_ts"] or 0),
            )
            state_changed = (
                before_states is not None
                and previous.get(chat_jid) != current_state
            )
            if (
                bootstrap
                or chat_ref in resumable
                or state_changed
            ):
                targets.append(
                    HistoryDepthTarget(
                        chat_jid=chat_jid,
                        chat_ref=chat_ref,
                        kind=str(row["kind"]),
                        current_count=current_state[0],
                        current_latest_ts=current_state[1],
                        state_changed=state_changed,
                    )
                )
        return targets
    finally:
        conn.close()


def history_depth_counts(store: Path, chat_jid: str) -> tuple[int, int, int]:
    conn = open_wacli_db(store)
    try:
        visibility = history_depth_visible_predicates(conn)
        where_sql = " AND ".join(["m.chat_jid = ?", *visibility])
        target = conn.execute(
            f"SELECT COUNT(*), MAX(m.ts) FROM messages m WHERE {where_sql}",
            (chat_jid,),
        ).fetchone()
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()
        return (
            int(target[0] or 0),
            int(total[0] or 0),
            int(target[1] or 0),
        )
    finally:
        conn.close()


def history_depth_state_digest(states: dict[str, tuple[int, int]]) -> str:
    digest = hashlib.sha256()
    for chat_jid, (message_count, latest_ts) in sorted(states.items()):
        digest.update(history_chat_ref(chat_jid).encode("ascii"))
        digest.update(f":{message_count}:{latest_ts}\n".encode("ascii"))
    return digest.hexdigest()


def read_history_depth_results(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        return {
            str(row.get("chat_ref") or ""): dict(row)
            for row in CsvIO.dict_reader(handle)
            if row.get("chat_ref")
        }


def read_history_depth_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_history_depth_results(path: Path, rows: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HISTORY_DEPTH_HEADERS)
        writer.writeheader()
        for chat_ref in sorted(rows):
            row = rows[chat_ref]
            writer.writerow({key: row.get(key, "") for key in HISTORY_DEPTH_HEADERS})
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def result_int(row: dict[str, Any], key: str) -> int:
    try:
        return int(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def classify_history_backfill_error(
    *,
    returncode: int,
    stderr: str,
    requests_sent: int,
) -> tuple[str, bool]:
    if returncode == 0:
        return "none", False
    text = stderr.casefold()
    if returncode == 124 or "timed out" in text or "timeout" in text:
        return "timeout", True
    if any(token in text for token in (
        "no such host",
        "temporary failure in name resolution",
        "network is unreachable",
        "connection reset",
        "connection refused",
        "no route to host",
        "dial tcp",
        "i/o timeout",
        "websocket",
    )):
        return "connection", True
    if "database is locked" in text or "store is locked" in text or "lock wait" in text:
        return "store_lock", True
    if "not authenticated" in text or "logged out" in text:
        return "unauthenticated", False
    if "access denied" in text or "forbidden" in text or "no access" in text:
        return "access_limited", False
    if requests_sent > 0:
        return "request_error", False
    return "command_error", False


def history_backfill_json_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


@dataclass(frozen=True)
class WacliHistoryDepthAdapter:
    """Thin Powerpacks boundary around wacli's native batch command.

    wacli owns connection reuse, throttling, response correlation, and PN/LID
    preference/fallback. This adapter owns only command construction plus the
    privacy-safe conversion from raw command results to hashed stage rows.
    """

    store: Path
    count: int = DEFAULT_HISTORY_DEPTH_COUNT
    requests: int = DEFAULT_HISTORY_DEPTH_REQUESTS
    request_delay: str = DEFAULT_HISTORY_DEPTH_REQUEST_DELAY
    batch_size: int = DEFAULT_HISTORY_DEPTH_BATCH_SIZE
    max_in_flight: int = DEFAULT_HISTORY_DEPTH_MAX_IN_FLIGHT
    response_wait: str = DEFAULT_HISTORY_DEPTH_RESPONSE_WAIT
    batch_delay: str = DEFAULT_HISTORY_DEPTH_BATCH_DELAY
    timeout_backoff: str = DEFAULT_HISTORY_DEPTH_TIMEOUT_BACKOFF
    timeout: int = DEFAULT_HISTORY_DEPTH_ATTEMPT_TIMEOUT

    def command(self, targets: list[HistoryDepthTarget]) -> list[str]:
        cmd = [
            wacli_bin() or "wacli",
            "--store",
            str(self.store),
            "--json",
            "history",
            "backfill-batch",
            "--count",
            str(self.count),
            "--requests",
            str(self.requests),
            "--wait",
            self.response_wait,
            "--request-delay",
            self.request_delay,
            "--batch-size",
            str(self.batch_size),
            "--max-inflight",
            str(self.max_in_flight),
            "--batch-delay",
            self.batch_delay,
            "--timeout-backoff",
            self.timeout_backoff,
            "--idle-exit",
            "5s",
        ]
        for target in targets:
            cmd.extend(["--chat", target.chat_jid])
        return cmd

    def run(
        self,
        targets: list[HistoryDepthTarget],
    ) -> tuple[dict[str, HistoryDepthAttempt], int]:
        if not targets:
            return {}, 0
        before_counts = {
            target.chat_ref: history_depth_counts(self.store, target.chat_jid)[0]
            for target in targets
        }
        before_total = history_depth_total_count(self.store)
        result = run_command(
            self.command(targets),
            timeout=self.timeout,
            heartbeat_message="Deepening WhatsApp history.",
        )
        data = history_backfill_json_data(result.get("json"))
        raw_chats = data.get("chats") if isinstance(data.get("chats"), list) else []
        chat_results = {
            str(item.get("chat") or ""): item
            for item in raw_chats
            if isinstance(item, dict) and item.get("chat")
        }
        global_returncode = int(result.get("returncode") or 0)
        stderr = str(result.get("stderr") or "")
        attempts: dict[str, HistoryDepthAttempt] = {}
        total_target_added = 0
        for target in targets:
            after_count, _after_total, after_latest_ts = history_depth_counts(
                self.store,
                target.chat_jid,
            )
            target_added = max(0, after_count - before_counts[target.chat_ref])
            total_target_added += target_added
            chat_data = chat_results.get(target.chat_jid, {})
            requests_sent = result_int(chat_data, "requests_sent")
            responses_seen = result_int(chat_data, "responses_seen")
            messages_received = result_int(chat_data, "messages_received")
            chat_error = str(chat_data.get("error") or "")
            local_returncode = global_returncode
            missing_result = not chat_data
            if local_returncode == 0 and chat_error:
                local_returncode = (
                    124 if "timed out" in chat_error.casefold() else 1
                )
            error_text = "\n".join(
                part for part in (chat_error, stderr) if part
            )
            error_category, retryable = classify_history_backfill_error(
                returncode=local_returncode,
                stderr=error_text,
                requests_sent=requests_sent,
            )
            if missing_result and global_returncode == 0:
                error_category = "missing_result"
                retryable = True
            elif (
                local_returncode == 0
                and requests_sent > 0
                and responses_seen == 0
            ):
                # A request without a protocol response is not proof that the
                # server has no older history. Keep it resumable.
                error_category = "timeout"
                retryable = True
            attempts[target.chat_ref] = HistoryDepthAttempt(
                returncode=local_returncode,
                requests_sent=requests_sent,
                responses_seen=responses_seen,
                target_added=target_added,
                unrelated_added=0,
                after_count=after_count,
                error_category=error_category,
                retryable=retryable,
                after_latest_ts=after_latest_ts,
                messages_received=messages_received,
                end_type=str(chat_data.get("end_type") or ""),
            )
        after_total = history_depth_total_count(self.store)
        unrelated_added = max(
            0,
            (after_total - before_total) - total_target_added,
        )
        return attempts, unrelated_added


def run_history_backfill_batch_attempt(
    store: Path,
    targets: list[HistoryDepthTarget],
    *,
    count: int = DEFAULT_HISTORY_DEPTH_COUNT,
    requests: int = DEFAULT_HISTORY_DEPTH_REQUESTS,
    request_delay: str = DEFAULT_HISTORY_DEPTH_REQUEST_DELAY,
    batch_size: int = DEFAULT_HISTORY_DEPTH_BATCH_SIZE,
    max_in_flight: int = DEFAULT_HISTORY_DEPTH_MAX_IN_FLIGHT,
    response_wait: str = DEFAULT_HISTORY_DEPTH_RESPONSE_WAIT,
    batch_delay: str = DEFAULT_HISTORY_DEPTH_BATCH_DELAY,
    timeout_backoff: str = DEFAULT_HISTORY_DEPTH_TIMEOUT_BACKOFF,
    timeout: int = DEFAULT_HISTORY_DEPTH_ATTEMPT_TIMEOUT,
) -> tuple[dict[str, HistoryDepthAttempt], int]:
    return WacliHistoryDepthAdapter(
        store=store,
        count=count,
        requests=requests,
        request_delay=request_delay,
        batch_size=batch_size,
        max_in_flight=max_in_flight,
        response_wait=response_wait,
        batch_delay=batch_delay,
        timeout_backoff=timeout_backoff,
        timeout=timeout,
    ).run(targets)


def run_history_backfill_attempt(
    store: Path,
    target: HistoryDepthTarget,
    *,
    count: int = DEFAULT_HISTORY_DEPTH_COUNT,
    requests: int = DEFAULT_HISTORY_DEPTH_REQUESTS,
    request_delay: str = DEFAULT_HISTORY_DEPTH_REQUEST_DELAY,
    timeout: int = DEFAULT_HISTORY_DEPTH_ATTEMPT_TIMEOUT,
) -> HistoryDepthAttempt:
    attempts, unrelated_added = run_history_backfill_batch_attempt(
        store,
        [target],
        count=count,
        requests=requests,
        request_delay=request_delay,
        batch_size=1,
        max_in_flight=1,
        timeout=timeout,
    )
    attempt = attempts[target.chat_ref]
    return replace(attempt, unrelated_added=unrelated_added)


def history_depth_summary(
    *,
    targets: list[HistoryDepthTarget],
    rows: dict[str, dict[str, Any]],
    results_path: Path,
    progress_path: Path,
    active_since_ts: int,
    max_count: int,
    count: int,
    requests: int,
    request_delay: str,
    no_growth_limit: int,
    batch_size: int,
    max_in_flight: int,
    response_wait: str,
    batch_delay: str,
    timeout_backoff: str,
    time_budget_seconds: int,
    bootstrap: bool,
    source_total_messages: int,
    source_dm_state_sha256: str,
    recovered_pre_sync_changes: bool,
) -> dict[str, Any]:
    target_rows = [rows[target.chat_ref] for target in targets if target.chat_ref in rows]
    completed = sum(
        1 for row in target_rows if row.get("outcome") in HISTORY_DEPTH_TERMINAL_OUTCOMES
    )
    pending = len(targets) - completed
    return {
        "status": "completed" if pending == 0 else "partial",
        "policy": {
            "version": HISTORY_DEPTH_POLICY_VERSION,
            "active_since": datetime.fromtimestamp(active_since_ts, timezone.utc).isoformat(),
            "lookback_years": DEFAULT_HISTORY_DEPTH_LOOKBACK_YEARS,
            "selection": "bootstrap_recent_shallow" if bootstrap else "changed_recent_shallow",
            "recovered_pre_sync_changes": recovered_pre_sync_changes,
            "max_message_count": max_count,
            "count_per_request": count,
            "requests_per_attempt": requests,
            "request_delay": request_delay,
            "batch_size": batch_size,
            "max_in_flight": max_in_flight,
            "response_wait": response_wait,
            "batch_delay": batch_delay,
            "timeout_backoff": timeout_backoff,
            "time_budget_seconds": time_budget_seconds,
            "no_growth_limit": no_growth_limit,
            "native_batch_command": True,
            "one_connection_per_run": True,
            "one_command_per_run": True,
            "identity_strategy": "saved_preference_then_opposite_fallback",
            "identity_preference_store": "private_wacli_db",
            "retry_scope": "next_import",
        },
        "counts": {
            "eligible": len(targets),
            "completed": completed,
            "pending": pending,
            "with_real_request": sum(1 for row in target_rows if result_int(row, "requests_sent") > 0),
            "recovered_chats": sum(
                1
                for row in target_rows
                if row.get("outcome") in {"completed_threshold", "recovered"}
            ),
            "target_rows_added": sum(result_int(row, "target_rows_added") for row in target_rows),
            "unrelated_rows_added": sum(result_int(row, "unrelated_rows_added") for row in target_rows),
            "server_zero": sum(1 for row in target_rows if row.get("outcome") == "server_zero"),
            "transient_failures": sum(result_int(row, "transient_failures") for row in target_rows),
            "terminal_errors": sum(1 for row in target_rows if row.get("outcome") == "terminal_error"),
            "source_total_messages": source_total_messages,
        },
        "source": {
            "dm_state_sha256": source_dm_state_sha256,
        },
        "outputs": {
            "results_csv": str(results_path),
            "progress_jsonl": str(progress_path),
        },
        "privacy": {
            "powerpacks_queries_read_message_bodies": False,
            "raw_identifiers_persisted": False,
            "returned_history_persisted_locally_by_wacli": True,
            "llm_called": False,
            "network": "whatsapp_only",
        },
    }


def write_history_depth_manifest(out_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    return write_stage_manifest(out_dir / "manifest.json", payload)


def run_history_depth_stage(
    store: Path,
    *,
    out_dir: Path = DEFAULT_HISTORY_DEPTH_DIR,
    active_since_ts: int | None = None,
    max_count: int = DEFAULT_HISTORY_DEPTH_MAX_COUNT,
    count: int = DEFAULT_HISTORY_DEPTH_COUNT,
    requests: int = DEFAULT_HISTORY_DEPTH_REQUESTS,
    request_delay: str = DEFAULT_HISTORY_DEPTH_REQUEST_DELAY,
    no_growth_limit: int = DEFAULT_HISTORY_DEPTH_NO_GROWTH_LIMIT,
    batch_size: int = DEFAULT_HISTORY_DEPTH_BATCH_SIZE,
    max_in_flight: int = DEFAULT_HISTORY_DEPTH_MAX_IN_FLIGHT,
    response_wait: str = DEFAULT_HISTORY_DEPTH_RESPONSE_WAIT,
    batch_delay: str = DEFAULT_HISTORY_DEPTH_BATCH_DELAY,
    timeout_backoff: str = DEFAULT_HISTORY_DEPTH_TIMEOUT_BACKOFF,
    attempt_timeout: int = DEFAULT_HISTORY_DEPTH_ATTEMPT_TIMEOUT,
    time_budget_seconds: int = DEFAULT_HISTORY_DEPTH_BUDGET_SECONDS,
    before_states: dict[str, tuple[int, int]] | None = None,
    before_total_messages: int | None = None,
    cold_start: bool = False,
    exclude_jids: set[str] | None = None,
) -> dict[str, Any]:
    if active_since_ts is None:
        active_since_ts = history_depth_cutoff_ts()
    results_path = out_dir / "results.csv"
    progress_path = out_dir / "progress.jsonl"
    manifest_path = out_dir / "manifest.json"
    initialized = results_path.exists()
    rows: dict[str, dict[str, Any]] = read_history_depth_results(results_path)
    current_states = history_depth_chat_states(store)
    current_by_ref = {
        history_chat_ref(chat_jid): state
        for chat_jid, state in current_states.items()
    }
    excluded_refs = {history_chat_ref(jid) for jid in (exclude_jids or set())}
    for chat_ref, row in rows.items():
        if row.get("outcome") in HISTORY_DEPTH_TERMINAL_OUTCOMES:
            continue
        if chat_ref in excluded_refs:
            row["outcome"] = "out_of_scope"
            row["error_category"] = "none"
            row["updated_at"] = now_iso()
            continue
        current_state = current_by_ref.get(chat_ref)
        if current_state is None:
            row["outcome"] = "gone"
            row["error_category"] = "none"
            row["updated_at"] = now_iso()
        elif current_state[1] < active_since_ts:
            row["current_count"] = current_state[0]
            row["current_latest_ts"] = current_state[1]
            row["outcome"] = "out_of_scope"
            row["error_category"] = "none"
            row["updated_at"] = now_iso()
        elif current_state[0] > max_count:
            row["current_count"] = current_state[0]
            row["current_latest_ts"] = current_state[1]
            row["outcome"] = "completed_threshold"
            row["error_category"] = "none"
            row["updated_at"] = now_iso()
    previous_manifest = read_history_depth_manifest(manifest_path)
    previous_counts = (
        previous_manifest.get("counts")
        if isinstance(previous_manifest.get("counts"), dict)
        else {}
    )
    previous_source = (
        previous_manifest.get("source")
        if isinstance(previous_manifest.get("source"), dict)
        else {}
    )
    has_source_total = "source_total_messages" in previous_counts
    prior_source_digest = str(previous_source.get("dm_state_sha256") or "")
    has_source_digest = len(prior_source_digest) == 64
    previous_policy = (
        previous_manifest.get("policy")
        if isinstance(previous_manifest.get("policy"), dict)
        else {}
    )
    has_current_policy = (
        result_int(previous_policy, "version") == HISTORY_DEPTH_POLICY_VERSION
    )
    prior_source_total = (
        result_int(previous_counts, "source_total_messages")
        if has_source_total
        else None
    )
    pre_sync_states = before_states if before_states is not None else current_states
    pre_sync_digest = history_depth_state_digest(pre_sync_states)
    recovered_pre_sync_changes = (
        initialized
        and (
            (
                prior_source_total is not None
                and before_total_messages is not None
                and prior_source_total != before_total_messages
            )
            or (
                has_source_digest
                and prior_source_digest != pre_sync_digest
            )
        )
    )
    bootstrap = (
        cold_start
        or not initialized
        or not has_current_policy
        or not has_source_total
        or not has_source_digest
        or recovered_pre_sync_changes
    )
    # This watermark represents the post-account-sync state whose changed
    # chats are about to be durably seeded. Keep it fixed during targeted
    # backfill: if WhatsApp returns rows for another chat, the next invocation
    # sees the mismatch and performs one catch-up bootstrap.
    source_total_messages = history_depth_total_count(store)
    source_dm_state_sha256 = history_depth_state_digest(current_states)
    resume_refs = {
        chat_ref
        for chat_ref, row in rows.items()
        if row.get("outcome") not in HISTORY_DEPTH_TERMINAL_OUTCOMES
    }
    targets = history_depth_targets(
        store,
        active_since_ts=active_since_ts,
        max_count=max_count,
        before_states=before_states,
        bootstrap=bootstrap,
        resume_refs=resume_refs,
        exclude_jids=exclude_jids,
    )
    stage_started = time.monotonic()
    write_progress(progress_path, {
        "event": "history_depth_started",
        "eligible": len(targets),
        "selection": "bootstrap_recent_shallow" if bootstrap else "changed_recent_shallow",
        "recovered_pre_sync_changes": recovered_pre_sync_changes,
    })

    def persist() -> dict[str, Any]:
        write_history_depth_results(results_path, rows)
        summary = history_depth_summary(
            targets=targets,
            rows=rows,
            results_path=results_path,
            progress_path=progress_path,
            active_since_ts=active_since_ts,
            max_count=max_count,
            count=count,
            requests=requests,
            request_delay=request_delay,
            no_growth_limit=no_growth_limit,
            batch_size=batch_size,
            max_in_flight=max_in_flight,
            response_wait=response_wait,
            batch_delay=batch_delay,
            timeout_backoff=timeout_backoff,
            time_budget_seconds=time_budget_seconds,
            bootstrap=bootstrap,
            source_total_messages=source_total_messages,
            source_dm_state_sha256=source_dm_state_sha256,
            recovered_pre_sync_changes=recovered_pre_sync_changes,
        )
        return write_history_depth_manifest(out_dir, summary)

    for target in targets:
        row = rows.get(target.chat_ref)
        if row is None:
            rows[target.chat_ref] = {
                "chat_ref": target.chat_ref,
                "kind": target.kind,
                "initial_count": target.current_count,
                "current_count": target.current_count,
                "current_latest_ts": target.current_latest_ts,
                "target_rows_added": 0,
                "unrelated_rows_added": 0,
                "attempts": 0,
                "requests_sent": 0,
                "responses_seen": 0,
                "transient_failures": 0,
                "no_growth_attempts": 0,
                "outcome": "pending",
                "error_category": "none",
                "updated_at": now_iso(),
            }
        elif (
            row.get("outcome") in {"gone", "out_of_scope"}
            or result_int(row, "current_count") != target.current_count
            or result_int(row, "current_latest_ts") != target.current_latest_ts
            or target.state_changed
        ):
            row["current_count"] = target.current_count
            row["current_latest_ts"] = target.current_latest_ts
            row["no_growth_attempts"] = 0
            row["outcome"] = "pending"
            row["error_category"] = "none"

    # Persist every selected target before the first network request so budget
    # exhaustion or interruption cannot lose unvisited work.
    summary = persist()

    def target_needs_attempt(candidate: HistoryDepthTarget) -> bool:
        candidate_row = rows.get(candidate.chat_ref)
        if candidate_row is None:
            return True
        return not (
            candidate_row.get("outcome") in HISTORY_DEPTH_TERMINAL_OUTCOMES
            and result_int(candidate_row, "current_count") == candidate.current_count
        )

    attempt_targets = [target for target in targets if target_needs_attempt(target)]
    if attempt_targets:
        elapsed = time.monotonic() - stage_started
        if time_budget_seconds > 0 and elapsed >= time_budget_seconds:
            write_progress(progress_path, {"event": "history_depth_budget_exhausted"})
            return persist()
        command_timeout = attempt_timeout
        if time_budget_seconds > 0:
            command_timeout = min(
                command_timeout,
                max(1, int(time_budget_seconds - elapsed)),
            )
        write_progress(progress_path, {
            "event": "history_depth_batch_started",
            "targets": len(attempt_targets),
            "batch_size": batch_size,
            "max_in_flight": max_in_flight,
        })
        if len(attempt_targets) == 1:
            target = attempt_targets[0]
            attempt = run_history_backfill_attempt(
                store,
                target,
                count=count,
                requests=requests,
                request_delay=request_delay,
                timeout=command_timeout,
            )
            attempts = {target.chat_ref: attempt}
            batch_unrelated_added = attempt.unrelated_added
            attempts[target.chat_ref] = replace(attempt, unrelated_added=0)
        else:
            attempts, batch_unrelated_added = run_history_backfill_batch_attempt(
                store,
                attempt_targets,
                count=count,
                requests=requests,
                request_delay=request_delay,
                batch_size=batch_size,
                max_in_flight=max_in_flight,
                response_wait=response_wait,
                batch_delay=batch_delay,
                timeout_backoff=timeout_backoff,
                timeout=command_timeout,
            )
        for index, target in enumerate(attempt_targets):
            row = rows.get(target.chat_ref)
            if row is None:
                raise PrimitiveFailed("history depth target was not seeded")
            attempt = attempts.get(target.chat_ref)
            if attempt is None:
                attempt = HistoryDepthAttempt(
                    returncode=0,
                    requests_sent=0,
                    responses_seen=0,
                    target_added=0,
                    unrelated_added=0,
                    after_count=target.current_count,
                    error_category="missing_result",
                    retryable=True,
                    after_latest_ts=target.current_latest_ts,
                )
            if index == 0 and batch_unrelated_added:
                attempt = replace(
                    attempt,
                    unrelated_added=attempt.unrelated_added + batch_unrelated_added,
                )

            row["attempts"] = result_int(row, "attempts") + 1
            row["requests_sent"] = result_int(row, "requests_sent") + attempt.requests_sent
            row["responses_seen"] = result_int(row, "responses_seen") + attempt.responses_seen
            row["target_rows_added"] = result_int(row, "target_rows_added") + attempt.target_added
            row["unrelated_rows_added"] = result_int(row, "unrelated_rows_added") + attempt.unrelated_added
            row["current_count"] = attempt.after_count
            if attempt.after_latest_ts:
                row["current_latest_ts"] = attempt.after_latest_ts
            row["error_category"] = attempt.error_category
            row["updated_at"] = now_iso()

            if attempt.returncode == 0 and attempt.target_added > 0:
                row["no_growth_attempts"] = 0
                if attempt.after_count > max_count:
                    row["outcome"] = "completed_threshold"
                elif attempt.end_type == "COMPLETE_AND_NO_MORE_MESSAGE_REMAIN_ON_PRIMARY":
                    row["outcome"] = "recovered"
                else:
                    row["outcome"] = "pending"
            elif attempt.retryable:
                row["transient_failures"] = result_int(row, "transient_failures") + 1
                if attempt.target_added > 0:
                    row["no_growth_attempts"] = 0
                row["outcome"] = (
                    "completed_threshold"
                    if attempt.after_count > max_count
                    else "pending"
                )
            elif (
                attempt.returncode == 0
                and attempt.responses_seen > 0
                and attempt.messages_received == 0
            ):
                if attempt.end_type in HISTORY_DEPTH_MORE_REMAIN_END_TYPES:
                    row["no_growth_attempts"] = 0
                    row["outcome"] = "pending"
                else:
                    row["no_growth_attempts"] = result_int(row, "no_growth_attempts") + 1
                    row["outcome"] = (
                        "server_zero"
                        if result_int(row, "no_growth_attempts") >= no_growth_limit
                        else "pending"
                    )
            elif attempt.returncode == 0:
                row["outcome"] = "pending"
            else:
                row["outcome"] = "terminal_error"

            write_progress(progress_path, {
                "event": "history_depth_attempt",
                "chat_ref": target.chat_ref,
                "attempt": result_int(row, "attempts"),
                "requests_sent": attempt.requests_sent,
                "responses_seen": attempt.responses_seen,
                "messages_received": attempt.messages_received,
                "target_added": attempt.target_added,
                "unrelated_added": attempt.unrelated_added,
                "outcome": row["outcome"],
                "error_category": attempt.error_category,
            })
            summary = persist()

    write_progress(progress_path, {
        "event": "history_depth_completed",
        "eligible": summary["counts"]["eligible"],
        "completed": summary["counts"]["completed"],
        "pending": summary["counts"]["pending"],
    })
    return summary


def store_stats(store: Path) -> dict[str, Any]:
    try:
        return wacli_json(store, ["store", "stats"], timeout=60)
    except PrimitiveFailed as exc:
        return {"status": "warning", "error": str(exc)}


def ensure_wacli_report() -> dict[str, Any]:
    """Download/refresh the pinned wacli binary to the current pin. Idempotent
    (a no-op when already current). Called by $update-powerpacks so a pin bump
    reaches the machine without running an import."""
    already_current = wacli_pinned_current()
    info = ensure_wacli_installed()
    return {
        "status": "ok",
        "action": "current" if already_current else "downloaded",
        "pinned_version": WACLI_PINNED_VERSION,
        "wacli": info,
    }


def status_report(store: Path) -> dict[str, Any]:
    """Install / auth / pairing / doctor / store-size snapshot for one store."""
    wacli_info = ensure_wacli_installed(install=False)
    status = auth_status(store, include_linked_jid=True)
    doctor = wacli_json(store, ["doctor"], timeout=60)
    stats = store_stats(store)
    return {
        "status": "ok",
        "wacli": wacli_info,
        "auth": status,
        "pairing": pairing_full_sync_status(store, authenticated=bool(status.get("authenticated"))),
        "doctor": doctor,
        "store_stats": stats,
    }


def logout_report(store: Path) -> dict[str, Any]:
    """Invalidate the WhatsApp session so the next auth issues a fresh QR. Backs
    the pre-full-sync re-link flow: an old (upstream/pre-full-sync) link is logged
    out here, then discovery re-pairs with full history sync. Idempotent on an
    already-logged-out store."""
    ensure_wacli_installed(install=False)
    authenticated_before = bool(auth_status(store).get("authenticated"))
    result: dict[str, Any] = {}
    if authenticated_before:
        result = wacli_json(store, ["auth", "logout"], timeout=60)
    marker_removed = False
    marker = pairing_marker_path(store)
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
    wacli_info = ensure_wacli_installed(install=install)
    doctor = wacli_json(store, ["doctor"], timeout=60)
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
        write_pairing_marker(store)  # we just paired with full sync
    pairing = pairing_full_sync_status(store, authenticated=linked)
    if pairing.get("state") == "pre_full_sync":
        emit_status(pairing["hint"])
    return {
        "status": "linked" if linked else "blocked_user_action",
        "pairing": pairing,
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


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store", default=str(DEFAULT_STORE), help="wacli store directory")


def build_parser() -> argparse.ArgumentParser:
    """The standalone lifecycle CLI surface: status / ensure-wacli / auth /
    logout. Every subcommand except `ensure-wacli` takes `--store` (installing
    the binary never touches a store)."""
    parser = argparse.ArgumentParser(description="Manage the openclaw/wacli WhatsApp client (install, auth, status, logout)")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="show wacli install/auth/store status")
    add_common_args(status)

    sub.add_parser("ensure-wacli", help="download/refresh the pinned wacli binary to the current pin (idempotent)")

    auth = sub.add_parser("auth", help="authenticate WhatsApp without syncing or exporting metadata")
    add_common_args(auth)
    auth.add_argument("--idle-exit", default=DEFAULT_IDLE_EXIT)
    auth.add_argument("--auth-timeout", type=int, default=DEFAULT_AUTH_TIMEOUT)
    auth.add_argument("--no-install", action="store_true", help="deprecated no-op; the pinned wacli fork always auto-downloads when missing or stale")
    auth.add_argument("--no-open-qr-page", action="store_true", help="render QR artifacts without opening the local browser page")

    logout = sub.add_parser("logout", help="invalidate the WhatsApp session (re-link flow); next discovery shows a fresh QR")
    add_common_args(logout)
    return parser


def main() -> int:
    """The wacli client's standalone lifecycle CLI (status/auth/ensure-wacli/
    logout): parse, build the subcommand's payload, emit it, map status to the
    exit code (20 blocked, 1 failed or not-linked-on-status, else 0). One
    envelope and one error mapping for every subcommand. The discovery
    `run`/`export` entry points live in `extract_whatsapp.py`."""
    args = build_parser().parse_args()
    envelope: dict[str, Any] = {"primitive": "messages/whatsapp_wacli", "command": args.command}
    store = Path(args.store) if hasattr(args, "store") else None
    if store is not None:
        envelope["store"] = str(store)
    try:
        if args.command == "status":
            payload = status_report(store)
            emit({**envelope, **payload})
            return 0 if payload["auth"].get("authenticated") else 1
        if args.command == "ensure-wacli":
            emit({**envelope, **ensure_wacli_report()})
            return 0
        if args.command == "auth":
            payload = auth_report(
                store,
                idle_exit=args.idle_exit,
                auth_timeout=args.auth_timeout,
                install=not args.no_install,
                open_qr_page=not args.no_open_qr_page,
            )
            emit({**envelope, **payload})
            return 0 if payload["status"] == "linked" else 20
        if args.command == "logout":
            emit({**envelope, **logout_report(store)})
            return 0
        return 2
    except PrimitiveBlocked as exc:
        emit({**envelope, **exc.payload})
        return exc.code
    except Exception as exc:
        emit({**envelope, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
