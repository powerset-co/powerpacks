#!/usr/bin/env python3
"""Isolated WhatsApp metadata import through openclaw/wacli.

This flow uses a separate wacli store under `.powerpacks/messages`.
The export reads only local metadata columns from wacli's SQLite database; it
never selects message body columns.

Stdlib-only.
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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from packs.ingestion.primitives.import_contacts_pipeline.common import write_manifest
    from packs.shared.csv_io import CsvIO
except ModuleNotFoundError:  # pragma: no cover - direct script fallback
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.primitives.import_contacts_pipeline.common import write_manifest
    from packs.shared.csv_io import CsvIO


DEFAULT_OUT_DIR = Path(".powerpacks/messages")
DEFAULT_STORE = DEFAULT_OUT_DIR / "wacli"
DEFAULT_OUTPUT_CSV = DEFAULT_OUT_DIR / "wacli.contacts.csv"
DEFAULT_OUTPUT_JSONL = DEFAULT_OUT_DIR / "wacli.contacts.jsonl"
DEFAULT_MANIFEST = DEFAULT_OUTPUT_CSV.with_suffix(DEFAULT_OUTPUT_CSV.suffix + ".manifest.json")
DEFAULT_PROGRESS_JSONL = DEFAULT_MANIFEST.with_suffix(DEFAULT_MANIFEST.suffix + ".progress.jsonl")
DEFAULT_NAME_FALLBACK_CSV = DEFAULT_OUT_DIR / "contacts.csv"
DEFAULT_HISTORY_DEPTH_DIR = DEFAULT_OUT_DIR / "history-depth"
DEFAULT_MAX_MESSAGES = int(os.environ.get("POWERPACKS_WACLI_MAX_MESSAGES", "0"))
# Store-size target used after the first sync: existing messages + headroom for
# new ones (headroom = max(1000, budget // 10) via effective_max_messages).
# The primitive chooses full vs incremental from the local store; there is no
# user-facing sync mode.
DEFAULT_INCREMENTAL_BUDGET = int(os.environ.get("POWERPACKS_WACLI_INCREMENTAL_BUDGET", "20000"))
DEFAULT_QR_PNG = DEFAULT_OUT_DIR / "wacli-login-qr.png"
DEFAULT_QR_HTML = DEFAULT_OUT_DIR / "wacli-login-qr.html"
DEFAULT_MAX_GROUP_PARTICIPANTS = int(os.environ.get("POWERPACKS_WACLI_MAX_GROUP_PARTICIPANTS", "30"))
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
DEFAULT_HISTORY_DEPTH_CHAT_DELAY = float(os.environ.get("POWERPACKS_WACLI_DEPTH_CHAT_DELAY", "5"))
DEFAULT_HISTORY_DEPTH_BATCH_SIZE = int(os.environ.get("POWERPACKS_WACLI_DEPTH_BATCH_SIZE", "10"))
DEFAULT_HISTORY_DEPTH_BATCH_PAUSE_SECONDS = float(
    os.environ.get("POWERPACKS_WACLI_DEPTH_BATCH_PAUSE_SECONDS", "90")
)
DEFAULT_HISTORY_DEPTH_TIMEOUTS_BEFORE_BREAK = int(
    os.environ.get("POWERPACKS_WACLI_DEPTH_TIMEOUTS_BEFORE_BREAK", "2")
)
DEFAULT_HISTORY_DEPTH_ATTEMPT_TIMEOUT = int(os.environ.get("POWERPACKS_WACLI_DEPTH_ATTEMPT_TIMEOUT", "900"))
DEFAULT_HISTORY_DEPTH_BUDGET_SECONDS = int(os.environ.get("POWERPACKS_WACLI_DEPTH_BUDGET_SECONDS", "6300"))
DEFAULT_HISTORY_DEPTH_LOOKBACK_YEARS = 3
HISTORY_DEPTH_POLICY_VERSION = 3
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
WACLI_PINNED_VERSION = os.environ.get("POWERPACKS_WACLI_VERSION", "v0.13.2-fullsync")
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
STATUS_PREFIX = "[import-whatsapp]"
GROUP_SEPARATOR = " | "
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

CSV_HEADERS = [
    "phone",
    "name",
    "source",
    "is_in_group_chats",
    "group_names",
    "message_count",
    "imessage_message_count",
    "whatsapp_message_count",
    "last_message",
    "imessage_last_message",
    "whatsapp_last_message",
    "skip",
    "match_status",
    "matched_person_id",
    "matched_name",
    "matched_linkedin_url",
    "match_confidence",
    "match_method",
    "match_reason",
]


@dataclass
class Contact:
    phone: str
    name: str = ""
    source: str = "whatsapp"
    is_in_group_chats: bool = False
    group_names: set[str] = field(default_factory=set)
    message_count: int | None = None
    last_message: str | None = None


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


class PrimitiveBlocked(Exception):
    def __init__(self, payload: dict[str, Any], code: int = 20) -> None:
        super().__init__(payload.get("message") or payload.get("status") or "blocked")
        self.payload = payload
        self.code = code


class PrimitiveFailed(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def emit_status(message: str) -> None:
    print(f"{STATUS_PREFIX} {message}", file=sys.stderr, flush=True)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_progress(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"timestamp": now_iso(), **payload}, sort_keys=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def parse_last_json(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    idx = 0
    last: Any = None
    while idx < len(text):
        while idx < len(text) and text[idx].isspace():
            idx += 1
        if idx >= len(text):
            break
        try:
            obj, idx = decoder.raw_decode(text, idx)
            last = obj
        except json.JSONDecodeError:
            nxt = text.find("{", idx + 1)
            if nxt == -1:
                break
            idx = nxt
    return last if isinstance(last, dict) else None


def run_command(
    cmd: list[str],
    *,
    timeout: int,
    env: dict[str, str] | None = None,
    stream_to_stderr: bool = False,
    heartbeat_message: str | None = None,
    heartbeat_interval: float = 120.0,
) -> dict[str, Any]:
    if not stream_to_stderr and not heartbeat_message:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
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
        env=env,
    )
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def reader(stream: Any, chunks: list[str]) -> None:
        for line in iter(stream.readline, ""):
            chunks.append(line)
            if stream_to_stderr:
                print(line, end="", file=sys.stderr, flush=True)

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
    request = urllib.request.Request(url, headers={"User-Agent": "powerpacks-import-whatsapp"})
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
                "Check network access, then rerun $import-whatsapp."
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
            "message": "qrencode is required to render the WhatsApp QR page. Install it with `brew install qrencode`, then rerun $import-whatsapp.",
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
            "message": "qrencode is required to render the WhatsApp QR page. Install it with `brew install qrencode`, then rerun $import-whatsapp.",
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
            "message": "WhatsApp cannot link new devices right now. Try again later in WhatsApp, then rerun $import-whatsapp.",
            "command": command_text(cmd),
        })
    if returncode != 0:
        if not connected:
            raise PrimitiveBlocked({
                "status": "blocked_user_action",
                "message": "WhatsApp needs a QR scan. Scan it, then rerun $import-whatsapp.",
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


def store_message_count(stats: dict[str, Any]) -> int | None:
    data = stats.get("data") if isinstance(stats.get("data"), dict) else {}
    value = data.get("messages")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
            "message": "WhatsApp cannot link new devices right now. Try again later in WhatsApp, then rerun $import-whatsapp.",
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


def read_group_participants_cache(store: Path) -> dict[str, Any]:
    path = group_participants_cache_path(store)
    if not path.exists():
        return {"groups": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"groups": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("groups"), dict):
        return {"groups": {}}
    return payload


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


def best_contact_name(row: dict[str, Any]) -> str:
    for key in ("full_name", "system_name", "push_name", "business_name", "first_name"):
        name = clean_name(row.get(key))
        if name:
            return name
    return ""


def epoch_to_iso(value: Any) -> str | None:
    if value in (None, "", 0):
        return None
    try:
        ts = float(value)
        if ts <= 0:
            return None
        if ts > 1e12:
            ts /= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return None


def serialize_groups(groups: set[str]) -> str:
    cleaned = sorted({clean_name(group) for group in groups if clean_name(group)}, key=str.casefold)
    return GROUP_SEPARATOR.join(cleaned)


def add_contact(contacts: dict[str, Contact], incoming: Contact) -> None:
    phone = canonicalize_phone(incoming.phone)
    if not phone:
        return
    existing = contacts.get(phone)
    if not existing:
        incoming.phone = phone
        contacts[phone] = incoming
        return
    if not existing.name and incoming.name:
        existing.name = incoming.name
    existing.is_in_group_chats = existing.is_in_group_chats or incoming.is_in_group_chats
    existing.group_names.update(incoming.group_names)
    if incoming.message_count is not None:
        existing.message_count = incoming.message_count
    if incoming.last_message and (not existing.last_message or incoming.last_message > existing.last_message):
        existing.last_message = incoming.last_message


def load_lid_map(store: Path) -> dict[str, str]:
    db_path = store / "session.db"
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if not table_exists(conn, "whatsmeow_lid_map"):
            return {}
        rows = select_rows(conn, "SELECT lid, pn FROM whatsmeow_lid_map")
        mapping: dict[str, str] = {}
        for row in rows:
            lid = str(row["lid"] or "")
            pn = str(row["pn"] or "")
            if not lid or not pn:
                continue
            mapping[lid] = pn
            if "@" not in lid:
                mapping[f"{lid}@lid"] = pn
        return mapping
    finally:
        conn.close()


def phone_for_jid(jid: str, contacts_by_jid: dict[str, dict[str, Any]], lid_map: dict[str, str]) -> str:
    contact = contacts_by_jid.get(jid) or {}
    mapped_jid = lid_map.get(jid) or ""
    mapped_contact = contacts_by_jid.get(mapped_jid) or {}
    return (
        canonicalize_phone(contact.get("phone"))
        or canonicalize_phone(mapped_contact.get("phone"))
        or jid_to_phone(mapped_jid)
        or jid_to_phone(jid)
        or ""
    )


def name_for_jid(jid: str, contacts_by_jid: dict[str, dict[str, Any]], lid_map: dict[str, str]) -> str:
    return best_contact_name(contacts_by_jid.get(jid) or {}) or best_contact_name(contacts_by_jid.get(lid_map.get(jid) or "") or {})


def names_by_phone(contacts_by_jid: dict[str, dict[str, Any]], lid_map: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for jid, row in contacts_by_jid.items():
        phone = phone_for_jid(jid, contacts_by_jid, lid_map)
        name = best_contact_name(row)
        if phone and name and phone not in out:
            out[phone] = name
    return out


def load_name_fallbacks(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    out: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in CsvIO.dict_reader(handle):
            phone = canonicalize_phone(row.get("phone"))
            name = clean_name(
                row.get("name")
                or row.get("display_name")
                or row.get("full_name")
                or row.get("contact_name")
            )
            if phone and name and phone not in out:
                out[phone] = name
    return out


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
    "server_zero",
    "gone",
    "out_of_scope",
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


def run_history_backfill_attempt(
    store: Path,
    target: HistoryDepthTarget,
    *,
    count: int = DEFAULT_HISTORY_DEPTH_COUNT,
    requests: int = DEFAULT_HISTORY_DEPTH_REQUESTS,
    request_delay: str = DEFAULT_HISTORY_DEPTH_REQUEST_DELAY,
    timeout: int = DEFAULT_HISTORY_DEPTH_ATTEMPT_TIMEOUT,
) -> HistoryDepthAttempt:
    before_target, before_total, _before_latest_ts = history_depth_counts(
        store,
        target.chat_jid,
    )
    cmd = [
        wacli_bin() or "wacli",
        "--store",
        str(store),
        "--json",
        "history",
        "backfill",
        "--chat",
        target.chat_jid,
        "--count",
        str(count),
        "--requests",
        str(requests),
        "--wait",
        "1m",
        "--request-delay",
        request_delay,
        "--idle-exit",
        "5s",
    ]
    result = run_command(
        cmd,
        timeout=timeout,
        heartbeat_message="Deepening WhatsApp history.",
    )
    after_target, after_total, after_latest_ts = history_depth_counts(
        store,
        target.chat_jid,
    )
    target_added = max(0, after_target - before_target)
    unrelated_added = max(0, (after_total - before_total) - target_added)
    data = history_backfill_json_data(result.get("json"))
    requests_sent = result_int(data, "requests_sent")
    responses_seen = result_int(data, "responses_seen")
    messages_received = result_int(data, "messages_received")
    stderr = str(result.get("stderr") or "")
    if requests_sent == 0:
        requests_sent = len(re.findall(r"Requesting \d+ older messages", stderr))
    if responses_seen == 0:
        responses_seen = stderr.count("On-demand history sync:")
    error_category, retryable = classify_history_backfill_error(
        returncode=int(result.get("returncode") or 0),
        stderr=stderr,
        requests_sent=requests_sent,
    )
    if (
        int(result.get("returncode") or 0) == 0
        and requests_sent > 0
        and responses_seen == 0
    ):
        # An idle-exit without a protocol response is not proof that the
        # server has no older history. Keep it resumable and let batch pacing
        # treat it like the response timeout it effectively was.
        error_category = "timeout"
        retryable = True
    return HistoryDepthAttempt(
        returncode=int(result.get("returncode") or 0),
        requests_sent=requests_sent,
        responses_seen=responses_seen,
        target_added=target_added,
        unrelated_added=unrelated_added,
        after_count=after_target,
        error_category=error_category,
        retryable=retryable,
        after_latest_ts=after_latest_ts,
        messages_received=messages_received,
    )


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
    chat_delay: float,
    batch_size: int,
    batch_pause_seconds: float,
    timeouts_before_break: int,
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
            "chat_delay_seconds": chat_delay,
            "batch_size": batch_size,
            "batch_pause_seconds": batch_pause_seconds,
            "timeouts_before_break": timeouts_before_break,
            "time_budget_seconds": time_budget_seconds,
            "no_growth_limit": no_growth_limit,
            "sequential": True,
            "one_command_per_chat_per_run": True,
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
    return write_manifest(out_dir.name, payload, import_dir=out_dir.parent)


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
    chat_delay: float = DEFAULT_HISTORY_DEPTH_CHAT_DELAY,
    batch_size: int = DEFAULT_HISTORY_DEPTH_BATCH_SIZE,
    batch_pause_seconds: float = DEFAULT_HISTORY_DEPTH_BATCH_PAUSE_SECONDS,
    timeouts_before_break: int = DEFAULT_HISTORY_DEPTH_TIMEOUTS_BEFORE_BREAK,
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
            chat_delay=chat_delay,
            batch_size=batch_size,
            batch_pause_seconds=batch_pause_seconds,
            timeouts_before_break=timeouts_before_break,
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
    batch_attempted = 0
    consecutive_zero_response_timeouts = 0
    for index, target in enumerate(targets):
        if time_budget_seconds > 0 and time.monotonic() - stage_started >= time_budget_seconds:
            write_progress(progress_path, {"event": "history_depth_budget_exhausted"})
            return persist()
        row = rows.get(target.chat_ref)
        if row is None:
            raise PrimitiveFailed("history depth target was not seeded")
        if (
            row.get("outcome") in HISTORY_DEPTH_TERMINAL_OUTCOMES
            and result_int(row, "current_count") == target.current_count
        ):
            continue

        last_attempt: HistoryDepthAttempt | None = None
        while True:
            if time_budget_seconds > 0 and time.monotonic() - stage_started >= time_budget_seconds:
                write_progress(progress_path, {"event": "history_depth_budget_exhausted"})
                return persist()
            attempt = run_history_backfill_attempt(
                store,
                target,
                count=count,
                requests=requests,
                request_delay=request_delay,
                timeout=attempt_timeout,
            )
            last_attempt = attempt
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
                row["outcome"] = (
                    "completed_threshold"
                    if attempt.after_count > max_count
                    else "pending"
                )
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
            # One wacli command already contains up to `requests` native
            # history requests. Any unfinished work resumes on the next
            # import, never as an immediate same-chat retry.
            break

        if last_attempt is None:
            continue
        batch_attempted += 1
        zero_response_timeout = (
            last_attempt.error_category == "timeout"
            and last_attempt.requests_sent > 0
            and last_attempt.responses_seen == 0
        )
        consecutive_zero_response_timeouts = (
            consecutive_zero_response_timeouts + 1
            if zero_response_timeout
            else 0
        )
        pause_reason = ""
        if (
            timeouts_before_break > 0
            and consecutive_zero_response_timeouts >= timeouts_before_break
        ):
            pause_reason = "consecutive_timeouts"
        elif batch_size > 0 and batch_attempted >= batch_size:
            pause_reason = "batch_complete"

        if index + 1 < len(targets) and pause_reason and batch_pause_seconds > 0:
            remaining = len(targets) - index - 1
            write_progress(progress_path, {
                "event": "history_depth_batch_paused",
                "reason": pause_reason,
                "pause_seconds": batch_pause_seconds,
                "remaining": remaining,
            })
            emit_status(
                "WhatsApp deeper-history requests paused for "
                f"{int(batch_pause_seconds)} seconds; {remaining} conversations remain."
            )
            time.sleep(batch_pause_seconds)
            write_progress(progress_path, {
                "event": "history_depth_batch_resumed",
                "remaining": remaining,
            })
            batch_attempted = 0
            consecutive_zero_response_timeouts = 0
        elif index + 1 < len(targets) and chat_delay > 0:
            time.sleep(chat_delay)

    write_progress(progress_path, {
        "event": "history_depth_completed",
        "eligible": summary["counts"]["eligible"],
        "completed": summary["counts"]["completed"],
        "pending": summary["counts"]["pending"],
    })
    return summary


def load_contacts_by_jid(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    contacts: dict[str, dict[str, Any]] = {}
    if not table_exists(conn, "contacts"):
        return contacts
    for row in select_rows(
        conn,
        "SELECT jid, phone, push_name, full_name, first_name, business_name, system_name FROM contacts",
    ):
        item = dict(row)
        contacts[str(item.get("jid") or "")] = item
    return contacts


def load_message_stats(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    if not table_exists(conn, "messages"):
        return {}
    columns = table_columns(conn, "messages")
    where = []
    if "revoked" in columns:
        where.append("revoked = 0")
    if "deleted_for_me" in columns:
        where.append("deleted_for_me = 0")
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    rows = select_rows(
        conn,
        f"SELECT chat_jid, COUNT(*) AS message_count, MAX(ts) AS last_ts FROM messages{where_sql} GROUP BY chat_jid",
    )
    return {
        str(row["chat_jid"]): {
            "message_count": int(row["message_count"] or 0),
            "last_message": epoch_to_iso(row["last_ts"]),
        }
        for row in rows
    }


def group_participant_counts(conn: sqlite3.Connection) -> dict[str, int]:
    if not table_exists(conn, "group_participants"):
        return {}
    rows = select_rows(conn, "SELECT group_jid, COUNT(*) AS participant_count FROM group_participants GROUP BY group_jid")
    return {str(row["group_jid"]): int(row["participant_count"] or 0) for row in rows}


def export_contacts_from_store(
    store: Path,
    *,
    include_left_groups: bool = False,
    max_group_participants: int = DEFAULT_MAX_GROUP_PARTICIPANTS,
    name_fallback_csv: Path | None = DEFAULT_NAME_FALLBACK_CSV,
) -> tuple[dict[str, Contact], dict[str, Any]]:
    conn = open_wacli_db(store)
    try:
        contacts_by_jid = load_contacts_by_jid(conn)
        lid_map = load_lid_map(store)
        contact_names_by_phone = names_by_phone(contacts_by_jid, lid_map)
        name_fallbacks_by_phone = load_name_fallbacks(name_fallback_csv)
        for phone, name in name_fallbacks_by_phone.items():
            contact_names_by_phone.setdefault(phone, name)
        message_stats = load_message_stats(conn)
        participant_counts = group_participant_counts(conn)
        participant_cache = read_group_participants_cache(store)
        cached_groups = participant_cache.get("groups") if isinstance(participant_cache.get("groups"), dict) else {}
        cached_group_jids = set(cached_groups)
        contacts: dict[str, Contact] = {}
        group_names: dict[str, str] = {}
        active_group_jids: set[str] = set()
        skipped_large_group_jids: set[str] = set()
        diagnostics: dict[str, Any] = {
            "direct_chats": 0,
            "group_chats": 0,
            "groups_seen": 0,
            "left_groups_skipped": 0,
            "group_participants": 0,
            "group_participants_skipped_large": 0,
            "group_participants_skipped_large_members": 0,
            "group_participant_cache_groups": len(cached_group_jids),
            "group_participant_cache_rows": sum(
                len(group.get("participants") or [])
                for group in cached_groups.values()
                if isinstance(group, dict)
            ),
            "message_stats_chats": len(message_stats),
            "lid_map_rows": len(lid_map),
            "name_fallback_rows": len(name_fallbacks_by_phone),
            "queries_read_message_body_columns": False,
        }

        for group_jid, group in cached_groups.items():
            if not isinstance(group, dict):
                continue
            participant_count = int(group.get("participant_count") or len(group.get("participants") or []))
            if max_group_participants > 0 and participant_count > max_group_participants:
                diagnostics["group_participants_skipped_large"] += 1
                diagnostics["group_participants_skipped_large_members"] += participant_count
                continue
            group_name = clean_name(group.get("name")) or str(group_jid)
            group_names[str(group_jid)] = group_name
            active_group_jids.add(str(group_jid))
            for participant in group.get("participants") or []:
                if not isinstance(participant, dict):
                    continue
                phone = canonicalize_phone(participant.get("phone"))
                if not phone:
                    continue
                diagnostics["group_participants"] += 1
                add_contact(contacts, Contact(
                    phone=phone,
                    name=clean_name(participant.get("name")) or contact_names_by_phone.get(phone, ""),
                    is_in_group_chats=True,
                    group_names={group_name},
                ))

        if table_exists(conn, "groups"):
            for row in select_rows(conn, "SELECT jid, name, left_at FROM groups"):
                jid = str(row["jid"] or "")
                if not jid:
                    continue
                diagnostics["groups_seen"] += 1
                group_names[jid] = clean_name(row["name"]) or jid
                left_at = row["left_at"]
                if left_at and not include_left_groups:
                    diagnostics["left_groups_skipped"] += 1
                    continue
                active_group_jids.add(jid)

        if table_exists(conn, "chats"):
            for row in select_rows(conn, "SELECT jid, kind, name, last_message_ts FROM chats"):
                jid = str(row["jid"] or "")
                kind = str(row["kind"] or "unknown")
                name = clean_name(row["name"])
                if kind == "group" or "@g.us" in jid:
                    diagnostics["group_chats"] += 1
                    group_names.setdefault(jid, name or jid)
                    if include_left_groups or jid in active_group_jids:
                        active_group_jids.add(jid)
                    continue

                phone = phone_for_jid(jid, contacts_by_jid, lid_map)
                if not phone:
                    continue
                diagnostics["direct_chats"] += 1
                contact_row = contacts_by_jid.get(jid) or {}
                stats = message_stats.get(jid) or {}
                last_message = stats.get("last_message") or epoch_to_iso(row["last_message_ts"])
                add_contact(contacts, Contact(
                    phone=phone,
                    name=name or best_contact_name(contact_row) or contact_names_by_phone.get(phone, ""),
                    message_count=stats.get("message_count"),
                    last_message=last_message,
                ))

        if table_exists(conn, "group_participants"):
            for row in select_rows(conn, "SELECT group_jid, user_jid FROM group_participants"):
                group_jid = str(row["group_jid"] or "")
                if group_jid in cached_group_jids:
                    continue
                if group_jid not in active_group_jids:
                    continue
                participant_count = participant_counts.get(group_jid, 0)
                if max_group_participants > 0 and participant_count > max_group_participants:
                    if group_jid not in skipped_large_group_jids:
                        skipped_large_group_jids.add(group_jid)
                        diagnostics["group_participants_skipped_large"] += 1
                        diagnostics["group_participants_skipped_large_members"] += participant_count
                    continue
                user_jid = str(row["user_jid"] or "")
                phone = phone_for_jid(user_jid, contacts_by_jid, lid_map)
                if not phone:
                    continue
                diagnostics["group_participants"] += 1
                group_name = group_names.get(group_jid) or group_jid
                add_contact(contacts, Contact(
                    phone=phone,
                    name=name_for_jid(user_jid, contacts_by_jid, lid_map) or contact_names_by_phone.get(phone, ""),
                    is_in_group_chats=True,
                    group_names={group_name},
                ))

        diagnostics.update({
            "contacts_exported": len(contacts),
            "contacts_with_message_count": sum(1 for item in contacts.values() if item.message_count is not None),
            "contacts_in_groups": sum(1 for item in contacts.values() if item.is_in_group_chats),
        })
        return contacts, diagnostics
    finally:
        conn.close()


def sorted_contacts(contacts: dict[str, Contact]) -> list[Contact]:
    return sorted(
        contacts.values(),
        key=lambda item: ((item.message_count or 0), item.last_message or "", item.phone),
        reverse=True,
    )


def write_csv(path: Path, contacts: dict[str, Contact]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted_contacts(contacts)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS)
        writer.writeheader()
        for contact in rows:
            writer.writerow({
                "phone": contact.phone,
                "name": contact.name,
                "source": contact.source,
                "is_in_group_chats": "true" if contact.is_in_group_chats else "false",
                "group_names": serialize_groups(contact.group_names),
                "message_count": "" if contact.message_count is None else str(contact.message_count),
                "imessage_message_count": "",
                "whatsapp_message_count": "" if contact.message_count is None else str(contact.message_count),
                "last_message": contact.last_message or "",
                "imessage_last_message": "",
                "whatsapp_last_message": contact.last_message or "",
                "skip": "",
                "match_status": "",
                "matched_person_id": "",
                "matched_name": "",
                "matched_linkedin_url": "",
                "match_confidence": "",
                "match_method": "",
                "match_reason": "",
            })
    return len(rows)


def write_jsonl(path: Path | None, contacts: dict[str, Contact]) -> int:
    if path is None:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted_contacts(contacts)
    with path.open("w", encoding="utf-8") as handle:
        for contact in rows:
            handle.write(json.dumps({
                "phone": contact.phone,
                "name": contact.name,
                "sources": [contact.source],
                "is_in_group_chats": contact.is_in_group_chats,
                "group_names": sorted(contact.group_names, key=str.casefold),
                "message_count": contact.message_count,
                "imessage_message_count": None,
                "whatsapp_message_count": contact.message_count,
                "last_message": contact.last_message,
                "imessage_last_message": None,
                "whatsapp_last_message": contact.last_message,
                "skip": False,
                "match": {
                    "status": None,
                    "person_id": None,
                    "name": None,
                    "linkedin_url": None,
                    "confidence": None,
                    "method": None,
                    "reason": None,
                },
            }, sort_keys=True) + "\n")
    return len(rows)


def store_stats(store: Path) -> dict[str, Any]:
    try:
        return wacli_json(store, ["store", "stats"], timeout=60)
    except PrimitiveFailed as exc:
        return {"status": "warning", "error": str(exc)}


def completed_payload(
    *,
    store: Path,
    output_csv: Path,
    output_jsonl: Path | None,
    manifest: Path,
    progress_jsonl: Path | None,
    wacli_info: dict[str, Any],
    doctor: dict[str, Any],
    stats: dict[str, Any],
    refresh: dict[str, Any],
    group_info: dict[str, Any],
    diagnostics: dict[str, Any],
    csv_rows: int,
    jsonl_rows: int,
    elapsed_ms: int,
    pairing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "primitive": "import_whatsapp_wacli",
        "status": "completed",
        "message": f"Imported {csv_rows} WhatsApp contacts.",
        "completed_at": now_iso(),
        "elapsed_ms": elapsed_ms,
        "store": str(store),
        "pairing": pairing or {},
        "artifacts": {
            "csv": str(output_csv),
            "jsonl": str(output_jsonl) if output_jsonl else None,
            "manifest": str(manifest),
            "progress_jsonl": str(progress_jsonl) if progress_jsonl else None,
        },
        "wacli": wacli_info,
        "doctor": doctor,
        "refresh_contacts": refresh,
        "group_info_refresh": group_info,
        "store_stats": stats,
        "counts": {
            "csv_rows": csv_rows,
            "jsonl_rows": jsonl_rows,
            "contacts": csv_rows,
            "with_message_count": diagnostics.get("contacts_with_message_count", 0),
            "in_group_chats": diagnostics.get("contacts_in_groups", 0),
        },
        "diagnostics": diagnostics,
        "privacy": {
            "export_reads_message_bodies": False,
            "export_reads_columns": "contacts, chats, groups, group_participants, and aggregate message metadata only",
        },
    }


def cmd_ensure_wacli(args: argparse.Namespace) -> int:
    """Download/refresh the pinned wacli binary to the current pin. Idempotent
    (a no-op when already current). Called by $update-powerpacks so a pin bump
    reaches the machine without running an import."""
    try:
        already_current = wacli_pinned_current()
        info = ensure_wacli_installed()
        emit({
            "primitive": "import_whatsapp_wacli",
            "command": "ensure-wacli",
            "status": "ok",
            "action": "current" if already_current else "downloaded",
            "pinned_version": WACLI_PINNED_VERSION,
            "wacli": info,
        })
        return 0
    except PrimitiveBlocked as exc:
        emit({"primitive": "import_whatsapp_wacli", "command": "ensure-wacli", **exc.payload})
        return exc.code
    except Exception as exc:
        emit({
            "primitive": "import_whatsapp_wacli",
            "command": "ensure-wacli",
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        })
        return 1


def cmd_status(args: argparse.Namespace) -> int:
    store = Path(args.store)
    try:
        wacli_info = ensure_wacli_installed(install=False)
        status = auth_status(store, include_linked_jid=True)
        doctor = wacli_json(store, ["doctor"], timeout=60)
        stats = store_stats(store)
        emit({
            "primitive": "import_whatsapp_wacli",
            "command": "status",
            "status": "ok",
            "store": str(store),
            "wacli": wacli_info,
            "auth": status,
            "pairing": pairing_full_sync_status(store, authenticated=bool(status.get("authenticated"))),
            "doctor": doctor,
            "store_stats": stats,
        })
        return 0 if status.get("authenticated") else 1
    except PrimitiveBlocked as exc:
        emit({"primitive": "import_whatsapp_wacli", "command": "status", **exc.payload})
        return exc.code
    except Exception as exc:
        emit({
            "primitive": "import_whatsapp_wacli",
            "command": "status",
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "store": str(store),
        })
        return 1


def cmd_logout(args: argparse.Namespace) -> int:
    """Invalidate the WhatsApp session so the next auth issues a fresh QR. Backs
    the pre-full-sync re-link flow: an old (upstream/pre-full-sync) link is logged
    out here, then discovery re-pairs with full history sync. Idempotent on an
    already-logged-out store."""
    store = Path(args.store)
    try:
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
        emit({
            "primitive": "import_whatsapp_wacli",
            "command": "logout",
            "status": "ok",
            "store": str(store),
            "authenticated_before": authenticated_before,
            "authenticated_after": bool(auth_status(store).get("authenticated")),
            "marker_removed": marker_removed,
            "result": result,
        })
        return 0
    except PrimitiveBlocked as exc:
        emit({"primitive": "import_whatsapp_wacli", "command": "logout", **exc.payload})
        return exc.code
    except Exception as exc:
        emit({
            "primitive": "import_whatsapp_wacli",
            "command": "logout",
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "store": str(store),
        })
        return 1


def cmd_auth(args: argparse.Namespace) -> int:
    store = Path(args.store)
    store.mkdir(parents=True, exist_ok=True)
    try:
        wacli_info = ensure_wacli_installed(install=not args.no_install)
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
                timeout=args.auth_timeout,
                idle_exit=args.idle_exit,
                open_qr_page=not getattr(args, "no_open_qr_page", False),
            ))
        status_after = auth_status(store)
        auth_summary["authenticated_after"] = status_after.get("authenticated")
        linked = bool(status_after.get("authenticated"))
        if not status_before.get("authenticated") and linked:
            write_pairing_marker(store)  # we just paired with full sync
        pairing = pairing_full_sync_status(store, authenticated=linked)
        if pairing.get("state") == "pre_full_sync":
            emit_status(pairing["hint"])
        emit({
            "primitive": "import_whatsapp_wacli",
            "command": "auth",
            "status": "linked" if linked else "blocked_user_action",
            "pairing": pairing,
            "message": (
                "WhatsApp account is linked. No WhatsApp sync or export was run."
                if linked
                else "WhatsApp needs a QR scan. Scan it, then rerun the auth command."
            ),
            "store": str(store),
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
        })
        return 0 if linked else 20
    except PrimitiveBlocked as exc:
        emit({"primitive": "import_whatsapp_wacli", "command": "auth", **exc.payload, "store": str(store)})
        return exc.code
    except Exception as exc:
        emit({
            "primitive": "import_whatsapp_wacli",
            "command": "auth",
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "store": str(store),
        })
        return 1


def cmd_export(args: argparse.Namespace) -> int:
    started = time.time()
    store = Path(args.store)
    output_csv = Path(args.output_csv)
    output_jsonl = Path(args.output_jsonl) if args.output_jsonl else None
    manifest = Path(args.manifest)
    name_fallback_csv = Path(args.name_fallback_csv) if getattr(args, "name_fallback_csv", None) else None
    try:
        contacts, diagnostics = export_contacts_from_store(
            store,
            include_left_groups=args.include_left_groups,
            max_group_participants=args.max_group_participants,
            name_fallback_csv=name_fallback_csv,
        )
        csv_rows = write_csv(output_csv, contacts)
        jsonl_rows = write_jsonl(output_jsonl, contacts)
        payload = completed_payload(
            store=store,
            output_csv=output_csv,
            output_jsonl=output_jsonl,
            manifest=manifest,
            progress_jsonl=None,
            wacli_info=wacli_version(),
            doctor={},
            stats={},
            refresh={},
            group_info={},
            diagnostics=diagnostics,
            csv_rows=csv_rows,
            jsonl_rows=jsonl_rows,
            elapsed_ms=int((time.time() - started) * 1000),
        )
        payload["command"] = "export"
        write_json(manifest, payload)
        emit(payload)
        return 0
    except Exception as exc:
        payload = {
            "primitive": "import_whatsapp_wacli",
            "command": "export",
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "store": str(store),
        }
        write_json(manifest, payload)
        emit(payload)
        return 1


def cmd_run(args: argparse.Namespace) -> int:
    started = time.time()
    store = Path(args.store)
    output_csv = Path(args.output_csv)
    output_jsonl = Path(args.output_jsonl) if args.output_jsonl else None
    manifest = Path(args.manifest)
    progress_jsonl = Path(args.progress_jsonl) if args.progress_jsonl else None
    name_fallback_csv = Path(args.name_fallback_csv) if getattr(args, "name_fallback_csv", None) else None
    store.mkdir(parents=True, exist_ok=True)
    write_progress(progress_jsonl, {"event": "started", "store": str(store)})

    try:
        wacli_info = ensure_wacli_installed(install=not args.no_install)
        write_progress(progress_jsonl, {"event": "wacli_ready", "wacli": wacli_info})
        existing_messages_at_start = history_depth_total_count(store)
        doctor = wacli_json(store, ["doctor"], timeout=60)
        status = auth_status(store)
        auth_summary: dict[str, Any] = {"authenticated_before": status.get("authenticated")}
        if not status.get("authenticated"):
            auth_summary.update(run_auth(
                store,
                timeout=args.auth_timeout,
                idle_exit=args.idle_exit,
                open_qr_page=not getattr(args, "no_open_qr_page", False),
            ))
            status = auth_status(store, include_linked_jid=True)
            if not status.get("authenticated"):
                raise PrimitiveBlocked({
                    "status": "blocked_user_action",
                    "message": "WhatsApp needs a QR scan. Scan it, then rerun $import-whatsapp.",
                    "store": str(store),
                })
        auth_summary["authenticated_after"] = status.get("authenticated")
        if not auth_summary.get("authenticated_before") and status.get("authenticated"):
            write_pairing_marker(store)  # we just paired with full sync
        pairing = pairing_full_sync_status(store, authenticated=bool(status.get("authenticated")))
        if pairing.get("state") == "pre_full_sync":
            emit_status(pairing["hint"])
        write_progress(progress_jsonl, {"event": "authenticated", "auth": auth_summary, "pairing": pairing})

        cold_start = existing_messages_at_start == 0
        before_states = history_depth_chat_states(store)
        before_total_messages = history_depth_total_count(store)
        effective_max_messages_value = (
            0
            if cold_start
            else resolve_effective_max(args.max_messages, before_total_messages)
        )
        sync_summary = run_sync(
            store,
            timeout=args.sync_timeout,
            idle_exit=args.idle_exit,
            max_messages=effective_max_messages_value,
        )
        sync_summary["strategy"] = "cold_full" if cold_start else "incremental"
        sync_summary["incremental"] = not cold_start
        sync_summary["requested_max_messages"] = args.max_messages
        sync_summary["existing_messages_at_start"] = existing_messages_at_start
        sync_summary["existing_messages_before_sync"] = before_total_messages
        write_progress(progress_jsonl, {"event": "synced", "sync": sync_summary})
        emit_status("Deepening recent shallow WhatsApp conversations sequentially.")
        doctor_data = doctor.get("data") if isinstance(doctor.get("data"), dict) else {}
        linked_jid = str(
            status.get("linked_jid")
            or doctor_data.get("linked_jid")
            or ""
        )
        history_depth = run_history_depth_stage(
            store,
            out_dir=manifest.parent / "history-depth",
            before_states=before_states,
            before_total_messages=before_total_messages,
            cold_start=cold_start,
            exclude_jids={linked_jid} if linked_jid else set(),
        )
        write_progress(
            progress_jsonl,
            {
                "event": "history_depth_completed",
                "status": history_depth.get("status"),
                "counts": history_depth.get("counts"),
            },
        )
        group_info = refresh_group_info(
            store,
            timeout=args.group_info_timeout,
            min_interval=args.group_info_interval,
        )
        write_progress(progress_jsonl, {"event": "group_info_refreshed", "group_info_refresh": group_info})
        refresh = refresh_contacts(store)
        stats = store_stats(store)
        contacts, diagnostics = export_contacts_from_store(
            store,
            include_left_groups=args.include_left_groups,
            max_group_participants=args.max_group_participants,
            name_fallback_csv=name_fallback_csv,
        )
        csv_rows = write_csv(output_csv, contacts)
        jsonl_rows = write_jsonl(output_jsonl, contacts)
        elapsed_ms = int((time.time() - started) * 1000)
        emit_status("WhatsApp sync finished.")
        payload = completed_payload(
            store=store,
            output_csv=output_csv,
            output_jsonl=output_jsonl,
            manifest=manifest,
            progress_jsonl=progress_jsonl,
            wacli_info=wacli_info,
            doctor=doctor,
            stats=stats,
            refresh=refresh,
            group_info=group_info,
            diagnostics=diagnostics,
            csv_rows=csv_rows,
            jsonl_rows=jsonl_rows,
            elapsed_ms=elapsed_ms,
            pairing=pairing,
        )
        payload["command"] = "run"
        payload["auth"] = auth_summary
        payload["sync"] = sync_summary
        payload["history_depth"] = history_depth
        write_json(manifest, payload)
        write_progress(progress_jsonl, {"event": "completed", "counts": payload["counts"]})
        emit(payload)
        return 0
    except PrimitiveBlocked as exc:
        payload = {
            "primitive": "import_whatsapp_wacli",
            "command": "run",
            **exc.payload,
            "store": str(store),
            "artifacts": {"manifest": str(manifest), "progress_jsonl": str(progress_jsonl) if progress_jsonl else None},
        }
        write_json(manifest, payload)
        write_progress(progress_jsonl, {"event": "blocked", "message": payload.get("message")})
        emit(payload)
        return exc.code
    except Exception as exc:
        status_after_failure: dict[str, Any] = {}
        try:
            status_after_failure = auth_status(store)
        except Exception as status_exc:
            status_after_failure = {"error": f"{type(status_exc).__name__}: {status_exc}"}
        payload = {
            "primitive": "import_whatsapp_wacli",
            "command": "run",
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "store": str(store),
            "auth_after_failure": status_after_failure,
            "artifacts": {"manifest": str(manifest), "progress_jsonl": str(progress_jsonl) if progress_jsonl else None},
        }
        write_json(manifest, payload)
        write_progress(progress_jsonl, {"event": "failed", "error": payload["error"]})
        emit(payload)
        return 1


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store", default=str(DEFAULT_STORE), help="wacli store directory")


def add_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    parser.add_argument("--output-jsonl", default=str(DEFAULT_OUTPUT_JSONL))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--include-left-groups", action="store_true", help="include groups wacli marks as left")
    parser.add_argument("--max-group-participants", type=int, default=DEFAULT_MAX_GROUP_PARTICIPANTS,
                        help="Export participants only for groups at or below this size; set <=0 to disable the cap")
    parser.add_argument("--name-fallback-csv", default=str(DEFAULT_NAME_FALLBACK_CSV),
                        help="Optional contacts CSV used only to fill missing names by phone")


def main() -> int:
    parser = argparse.ArgumentParser(description="Import WhatsApp metadata through openclaw/wacli")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="install wacli if needed, authenticate, sync once, and export metadata")
    add_common_args(run)
    add_output_args(run)
    run.add_argument("--progress-jsonl", default=str(DEFAULT_PROGRESS_JSONL))
    run.add_argument("--max-messages", type=int, default=DEFAULT_MAX_MESSAGES)
    run.add_argument("--idle-exit", default=DEFAULT_IDLE_EXIT)
    run.add_argument("--auth-timeout", type=int, default=DEFAULT_AUTH_TIMEOUT)
    run.add_argument("--sync-timeout", type=int, default=DEFAULT_SYNC_TIMEOUT)
    run.add_argument("--group-info-timeout", type=int, default=60)
    run.add_argument("--group-info-interval", type=float, default=0.2)
    run.add_argument("--no-install", action="store_true", help="deprecated no-op; the pinned wacli fork always auto-downloads when missing or stale")
    run.add_argument("--no-open-qr-page", action="store_true", help="render QR artifacts without opening the local browser page")
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="show wacli install/auth/store status")
    add_common_args(status)
    status.set_defaults(func=cmd_status)

    ensure = sub.add_parser("ensure-wacli", help="download/refresh the pinned wacli binary to the current pin (idempotent)")
    ensure.set_defaults(func=cmd_ensure_wacli)

    auth = sub.add_parser("auth", help="authenticate WhatsApp without syncing or exporting metadata")
    add_common_args(auth)
    auth.add_argument("--idle-exit", default=DEFAULT_IDLE_EXIT)
    auth.add_argument("--auth-timeout", type=int, default=DEFAULT_AUTH_TIMEOUT)
    auth.add_argument("--no-install", action="store_true", help="deprecated no-op; the pinned wacli fork always auto-downloads when missing or stale")
    auth.add_argument("--no-open-qr-page", action="store_true", help="render QR artifacts without opening the local browser page")
    auth.set_defaults(func=cmd_auth)

    export = sub.add_parser("export", help="export metadata from an existing wacli store without syncing")
    add_common_args(export)
    add_output_args(export)
    export.set_defaults(func=cmd_export)

    logout = sub.add_parser("logout", help="invalidate the WhatsApp session (re-link flow); next discovery shows a fresh QR")
    add_common_args(logout)
    logout.set_defaults(func=cmd_logout)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
