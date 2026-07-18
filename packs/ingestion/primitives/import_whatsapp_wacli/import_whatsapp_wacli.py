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
import html
import json
import os
import queue
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from packs.shared.csv_io import CsvIO
except ModuleNotFoundError:  # pragma: no cover - direct script fallback
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.shared.csv_io import CsvIO


DEFAULT_OUT_DIR = Path(".powerpacks/messages")
DEFAULT_STORE = DEFAULT_OUT_DIR / "wacli"
DEFAULT_OUTPUT_CSV = DEFAULT_OUT_DIR / "wacli.contacts.csv"
DEFAULT_OUTPUT_JSONL = DEFAULT_OUT_DIR / "wacli.contacts.jsonl"
DEFAULT_MANIFEST = DEFAULT_OUTPUT_CSV.with_suffix(DEFAULT_OUTPUT_CSV.suffix + ".manifest.json")
DEFAULT_PROGRESS_JSONL = DEFAULT_MANIFEST.with_suffix(DEFAULT_MANIFEST.suffix + ".progress.jsonl")
DEFAULT_NAME_FALLBACK_CSV = DEFAULT_OUT_DIR / "contacts.csv"
DEFAULT_MAX_MESSAGES = int(os.environ.get("POWERPACKS_WACLI_MAX_MESSAGES", "0"))
# Store-size target used by incremental sync: existing messages + headroom for
# new ones (headroom = max(1000, budget // 10) via effective_max_messages). A
# rerun therefore pulls only the delta, not the whole history. Tunable — higher
# is safer for very active accounts between syncs, but slower.
DEFAULT_INCREMENTAL_BUDGET = int(os.environ.get("POWERPACKS_WACLI_INCREMENTAL_BUDGET", "20000"))
SYNC_MODES = ("auto", "full", "incremental")
DEFAULT_QR_PNG = DEFAULT_OUT_DIR / "wacli-login-qr.png"
DEFAULT_QR_HTML = DEFAULT_OUT_DIR / "wacli-login-qr.html"
DEFAULT_MAX_GROUP_PARTICIPANTS = int(os.environ.get("POWERPACKS_WACLI_MAX_GROUP_PARTICIPANTS", "30"))
DEFAULT_GROUP_PARTICIPANTS_CACHE = DEFAULT_OUT_DIR / "wacli.group-participants.json"
DEFAULT_IDLE_EXIT = os.environ.get("POWERPACKS_WACLI_IDLE_EXIT", "30s")
DEFAULT_AUTH_TIMEOUT = int(os.environ.get("POWERPACKS_WACLI_AUTH_TIMEOUT", "600"))
DEFAULT_SYNC_TIMEOUT = int(os.environ.get("POWERPACKS_WACLI_SYNC_TIMEOUT", "900"))
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
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
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


def install_remediation(text: str) -> str:
    lowered = text.lower()
    if "xcode-select" in lowered or "command line tools" in lowered:
        return "Install Command Line Tools with `xcode-select --install`, then rerun $import-whatsapp."
    if "permission denied" in lowered:
        return "Homebrew hit a permission error. Fix the Homebrew permission issue above, then rerun $import-whatsapp."
    return "Install failed. Fix the Homebrew error above, then rerun $import-whatsapp."


def wacli_version(timeout: int = 30) -> dict[str, Any]:
    exe = shutil.which("wacli")
    if not exe:
        raise PrimitiveFailed("wacli is not installed")
    result = run_command([exe, "--version"], timeout=timeout)
    version = (result.get("stdout") or "").strip()
    if result["returncode"] != 0 or not version:
        raise PrimitiveFailed(((result.get("stderr") or result.get("stdout") or "").strip())[-1000:])
    return {"path": exe, "version": version}


def ensure_wacli_installed(*, install: bool = True) -> dict[str, Any]:
    if shutil.which("wacli"):
        return wacli_version()
    if not install:
        raise PrimitiveBlocked({
            "status": "blocked_user_action",
            "message": "wacli is not installed. Install it, then rerun $import-whatsapp.",
            "install_command": "brew install steipete/tap/wacli",
        })
    brew = shutil.which("brew")
    if not brew:
        raise PrimitiveBlocked({
            "status": "blocked_user_action",
            "message": "Homebrew is required to install wacli automatically.",
            "install_command": "brew install steipete/tap/wacli",
        })
    emit_status("Installing WhatsApp sync helper.")
    result = run_command([brew, "install", "steipete/tap/wacli"], timeout=900)
    if result["returncode"] != 0:
        detail = (result.get("stderr") or result.get("stdout") or "").strip()
        raise PrimitiveBlocked({
            "status": "blocked_user_action",
            "message": install_remediation(detail),
            "detail": detail[-4000:],
            "install_command": "brew install steipete/tap/wacli",
        })
    return wacli_version()


def wacli_json(store: Path, args: list[str], *, timeout: int = 300) -> dict[str, Any]:
    cmd = ["wacli", "--store", str(store), "--json", *args]
    result = run_command(cmd, timeout=timeout)
    payload = result.get("json")
    if result["returncode"] != 0:
        raise PrimitiveFailed(((result.get("stderr") or result.get("stdout") or "").strip())[-1000:])
    return payload if isinstance(payload, dict) else {}


def auth_status(store: Path) -> dict[str, Any]:
    payload = wacli_json(store, ["auth", "status"], timeout=60)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    status = {
        "authenticated": bool(data.get("authenticated")),
        "raw_success": payload.get("success"),
        "error": payload.get("error"),
    }
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


def redact_qr_payloads(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("2@") or '"event":"qr_code"' in stripped or '"event": "qr_code"' in stripped:
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
        "wacli",
        "--store", str(store),
        "--events",
        "auth",
        "--qr-format", "text",
        "--follow=false",
        "--idle-exit", idle_exit,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    output: list[str] = []
    opened = False
    connected = False
    interrupted_bootstrap_sync = False
    started = time.time()

    lines: queue.Queue[tuple[str, str]] = queue.Queue()

    def read_stream(name: str, stream: Any) -> None:
        for line in stream:
            lines.put((name, line))

    stdout_thread = threading.Thread(target=read_stream, args=("stdout", proc.stdout), daemon=True)
    stderr_thread = threading.Thread(target=read_stream, args=("stderr", proc.stderr), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    def handle_line(source: str, text: str) -> None:
        nonlocal opened, connected, interrupted_bootstrap_sync
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
            if event_name == "qr_code" and isinstance(code, str) and code.startswith("2@"):
                update_qr_page(code, DEFAULT_QR_PNG, DEFAULT_QR_HTML, open_page=open_qr_page and not opened)
                opened = True
                emit_status("Refreshed WhatsApp QR page.")
            elif event_name == "connected":
                connected = True
                if not interrupted_bootstrap_sync:
                    proc.send_signal(signal.SIGINT)
                    interrupted_bootstrap_sync = True
            return
        if source == "stdout" and text.startswith("2@"):
            update_qr_page(text, DEFAULT_QR_PNG, DEFAULT_QR_HTML, open_page=open_qr_page and not opened)
            opened = True
            emit_status("Refreshed WhatsApp QR page.")

    try:
        while proc.poll() is None:
            if time.time() - started > timeout:
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
    if returncode != 0 and not connected:
        raise PrimitiveBlocked({
            "status": "blocked_user_action",
            "message": "WhatsApp needs a QR scan. Scan it, then rerun $import-whatsapp.",
            "command": command_text(cmd),
            "qr_page": str(DEFAULT_QR_HTML),
            "qr_png": str(DEFAULT_QR_PNG),
            "detail": joined[-2000:],
        })
    return {
        "command": command_text(cmd),
        "returncode": returncode,
        "qr_page": str(DEFAULT_QR_HTML),
        "qr_png": str(DEFAULT_QR_PNG),
        "connected_event": connected,
        "auth_bootstrap_sync_interrupted": interrupted_bootstrap_sync,
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


def resolve_effective_max(mode: str, requested: int, existing: int) -> int:
    """Resolve the wacli --max-messages target from the sync mode.

    `full` always re-backfills everything (0 = unlimited). `incremental` targets
    existing + headroom so a rerun pulls only the delta. `auto` (default) is
    smart: a first run (empty store) backfills fully, later runs go incremental.
    An explicit positive ``requested`` (someone passed --max-messages N) always
    wins over the mode."""
    if requested and requested > 0:
        return effective_max_messages(requested, existing)
    if mode == "full":
        return 0
    if mode == "incremental":
        return effective_max_messages(DEFAULT_INCREMENTAL_BUDGET, existing)
    # auto: full on the first run, incremental once the store is populated.
    if existing > 0:
        return effective_max_messages(DEFAULT_INCREMENTAL_BUDGET, existing)
    return 0


def run_sync(store: Path, *, timeout: int, idle_exit: str, max_messages: int) -> dict[str, Any]:
    emit_status("Syncing WhatsApp Messages and Contacts.")
    cmd = [
        "wacli",
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
            ["wacli", "--store", str(store), "--json", "groups", "info", "--jid", jid],
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
) -> dict[str, Any]:
    return {
        "primitive": "import_whatsapp_wacli",
        "status": "completed",
        "message": f"Imported {csv_rows} WhatsApp contacts.",
        "completed_at": now_iso(),
        "elapsed_ms": elapsed_ms,
        "store": str(store),
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


def cmd_status(args: argparse.Namespace) -> int:
    store = Path(args.store)
    try:
        wacli_info = ensure_wacli_installed(install=False)
        status = auth_status(store)
        doctor = wacli_json(store, ["doctor"], timeout=60)
        stats = store_stats(store)
        emit({
            "primitive": "import_whatsapp_wacli",
            "command": "status",
            "status": "ok",
            "store": str(store),
            "wacli": wacli_info,
            "auth": status,
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
        emit({
            "primitive": "import_whatsapp_wacli",
            "command": "auth",
            "status": "linked" if linked else "blocked_user_action",
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
            status = auth_status(store)
            if not status.get("authenticated"):
                raise PrimitiveBlocked({
                    "status": "blocked_user_action",
                    "message": "WhatsApp needs a QR scan. Scan it, then rerun $import-whatsapp.",
                    "store": str(store),
                })
        auth_summary["authenticated_after"] = status.get("authenticated")
        write_progress(progress_jsonl, {"event": "authenticated", "auth": auth_summary})

        stats_before_sync = store_stats(store)
        existing_messages = store_message_count(stats_before_sync) or 0
        sync_mode = getattr(args, "sync_mode", "auto")
        effective_max_messages_value = resolve_effective_max(
            sync_mode, args.max_messages, existing_messages
        )
        sync_summary = run_sync(
            store,
            timeout=args.sync_timeout,
            idle_exit=args.idle_exit,
            max_messages=effective_max_messages_value,
        )
        sync_summary["sync_mode"] = sync_mode
        sync_summary["incremental"] = effective_max_messages_value > 0
        sync_summary["requested_max_messages"] = args.max_messages
        sync_summary["existing_messages_before_sync"] = existing_messages
        write_progress(progress_jsonl, {"event": "synced", "sync": sync_summary})
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
        )
        payload["command"] = "run"
        payload["auth"] = auth_summary
        payload["sync"] = sync_summary
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
    run.add_argument("--sync-mode", choices=SYNC_MODES, default="auto",
                     help="auto: full on first run, incremental after; "
                          "full: always re-backfill everything; "
                          "incremental: only pull new messages since last sync")
    run.add_argument("--idle-exit", default=DEFAULT_IDLE_EXIT)
    run.add_argument("--auth-timeout", type=int, default=DEFAULT_AUTH_TIMEOUT)
    run.add_argument("--sync-timeout", type=int, default=DEFAULT_SYNC_TIMEOUT)
    run.add_argument("--group-info-timeout", type=int, default=60)
    run.add_argument("--group-info-interval", type=float, default=0.2)
    run.add_argument("--no-install", action="store_true", help="fail instead of installing wacli with Homebrew")
    run.add_argument("--no-open-qr-page", action="store_true", help="render QR artifacts without opening the local browser page")
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="show wacli install/auth/store status")
    add_common_args(status)
    status.set_defaults(func=cmd_status)

    auth = sub.add_parser("auth", help="authenticate WhatsApp without syncing or exporting metadata")
    add_common_args(auth)
    auth.add_argument("--idle-exit", default=DEFAULT_IDLE_EXIT)
    auth.add_argument("--auth-timeout", type=int, default=DEFAULT_AUTH_TIMEOUT)
    auth.add_argument("--no-install", action="store_true", help="fail instead of installing wacli with Homebrew")
    auth.add_argument("--no-open-qr-page", action="store_true", help="render QR artifacts without opening the local browser page")
    auth.set_defaults(func=cmd_auth)

    export = sub.add_parser("export", help="export metadata from an existing wacli store without syncing")
    add_common_args(export)
    add_output_args(export)
    export.set_defaults(func=cmd_export)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
