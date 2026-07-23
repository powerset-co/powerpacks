#!/usr/bin/env python3
"""Shared helpers for the per-source discovery/import primitives.

Changelog:
  2026-07-23 (audit batch 16): deleted the orchestrator-only ledger/step
    machinery (DEFAULT_LEDGER, load/save_ledger, mark_step, begin_step,
    check_artifact_paths, child_error, artifact_dir helpers, csv upsert
    helpers, sha, truthy_env) after `discover.py` was
    removed; renamed the progress prefix from [discover-contacts] (retired
    skill) to [discover].
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_BASE_DIR = Path(".powerpacks/network-import")
DEFAULT_DIRECTORY_CSV = DEFAULT_BASE_DIR / "directory.csv"
DEFAULT_MSGVAULT_DB = Path.home() / ".msgvault" / "msgvault.db"
DEFAULT_CHILD_TIMEOUT_SECONDS = int(os.environ.get("POWERPACKS_IMPORT_NETWORK_CHILD_TIMEOUT_SECONDS", str(6 * 60 * 60)))
GMAIL_INTERACTION_CALCULATION_VERSION = "msgvault-interactions-v2"

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.shared.csv_io import CsvIO  # noqa: E402


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def emit_progress(message: str) -> None:
    print(f"[discover] {message}", file=sys.stderr, flush=True)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        reader = CsvIO.dict_reader(handle)
        fields = list(reader.fieldnames or [])
        rows = [{str(key): value or "" for key, value in row.items() if key is not None} for row in reader]
    return fields, rows


def write_csv_rows(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    import io

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, "") for field in fieldnames})
    content = buffer.getvalue().encode("utf-8")
    if path.exists():
        try:
            if path.read_bytes() == content:
                return
        except OSError:
            pass
    path.write_bytes(content)


def parse_jsonish(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return default


def read_accounts(path: Path) -> dict[str, Any]:
    return read_json(path, {}) or {}


def account_channel(accounts: dict[str, Any], name: str) -> dict[str, Any]:
    for key in ("accounts", "channels"):
        group = accounts.get(key)
        if isinstance(group, dict) and isinstance(group.get(name), dict):
            return group[name]
    return {}


def account_config(accounts: dict[str, Any], name: str) -> dict[str, Any]:
    channel = account_channel(accounts, name)
    cfg = channel.get("config")
    return cfg if isinstance(cfg, dict) else {}


def channel_is_linked(accounts: dict[str, Any], name: str) -> bool:
    channel = account_channel(accounts, name)
    status = str(channel.get("status") or "").strip().lower()
    if status == "linked":
        return True
    return bool(channel.get("linked") is True) and not bool(channel.get("skipped"))


def ordered_unique(values: list[Any]) -> list[str]:
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def collect_artifact_paths(value: Any) -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            paths.extend(collect_artifact_paths(item))
    elif isinstance(value, list):
        for item in value:
            paths.extend(collect_artifact_paths(item))
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith(".powerpacks/") or text.startswith("/"):
            paths.append(text)
    return paths


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_fingerprint(path_text: str, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    path = Path(str(path_text or ""))
    if not path_text or not path.exists() or not path.is_file():
        return {"path": str(path_text or ""), "exists": False}
    stat = path.stat()
    existing = existing or {}
    mtime_ns = stat.st_mtime_ns
    if (
        existing.get("path") == str(path)
        and existing.get("exists") is True
        and existing.get("size") == stat.st_size
        and existing.get("mtime_ns") == mtime_ns
        and existing.get("sha256")
    ):
        return dict(existing)
    return {"path": str(path), "exists": True, "size": stat.st_size, "mtime_ns": mtime_ns, "sha256": sha256_file(path)}


def manifest_fingerprints(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing or {}
    existing_inputs = existing.get("input_artifacts") if isinstance(existing.get("input_artifacts"), dict) else {}
    existing_outputs = existing.get("output_artifacts") if isinstance(existing.get("output_artifacts"), dict) else {}
    input_paths = collect_artifact_paths(payload.get("input") or {})
    output_paths = collect_artifact_paths({
        "artifacts": payload.get("artifacts") or {},
        "contacts_csv": payload.get("contacts_csv"),
        "linkedin_resolution_queue_csv": payload.get("linkedin_resolution_queue_csv"),
        "source_csv": payload.get("source_csv"),
        "review_csv": payload.get("review_csv"),
    })
    return {
        "input_artifacts": {path: artifact_fingerprint(path, existing_inputs.get(path) if isinstance(existing_inputs, dict) else None) for path in input_paths},
        "output_artifacts": {path: artifact_fingerprint(path, existing_outputs.get(path) if isinstance(existing_outputs, dict) else None) for path in output_paths},
    }


@dataclass
class StagePayload:
    """Base for the TYPED per-vertical stage-manifest payloads (see each
    vertical's models.py). A payload is a dataclass, not an ad-hoc dict, so a
    stage cannot invent fields on the fly; `to_payload()` is what
    write_stage_manifest consumes (None-valued optionals are dropped so
    optional fields do not add empty keys)."""

    def to_payload(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def stable_manifest_signature(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the whole manifest payload without volatile timestamp fields."""
    signature = dict(payload)
    signature.pop("updated_at", None)
    signature.pop("created_at", None)
    return signature


def write_stage_manifest(path: Path, payload: "dict[str, Any] | StagePayload") -> dict[str, Any]:
    """Write one stage's manifest (fingerprinted, no-op when unchanged).

    Accepts the vertical's typed StagePayload (preferred — see
    <vertical>/models.py) or its dict form."""
    if isinstance(payload, StagePayload):
        payload = payload.to_payload()
    existing = read_json(path, {}) or {}
    payload = dict(payload)
    payload["fingerprints"] = payload.get("fingerprints") or manifest_fingerprints(payload, existing.get("fingerprints") if isinstance(existing.get("fingerprints"), dict) else None)
    if existing and stable_manifest_signature(existing) == stable_manifest_signature(payload):
        return existing
    payload["updated_at"] = payload.get("updated_at") or now_iso()
    write_json(path, payload)
    return payload


def unique_strings(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raw = [values]
    elif isinstance(values, list):
        raw = values
    else:
        raw = [values]
    out: list[str] = []
    for value in raw:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def source_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (value or "").strip().lower()).strip("-._")
    return slug or "source"


def parse_last_json(stdout: str) -> dict[str, Any]:
    text = (stdout or "").strip()
    if not text:
        return {}
    decoder = json.JSONDecoder()
    idx = 0
    last: dict[str, Any] = {}
    while idx < len(text):
        while idx < len(text) and text[idx].isspace():
            idx += 1
        if idx >= len(text):
            break
        try:
            value, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            break
        if isinstance(value, dict):
            last = value
        idx = end
    return last


def run_cmd(cmd: list[str], *, timeout: int | None = None) -> tuple[int, dict[str, Any], str]:
    effective_timeout = DEFAULT_CHILD_TIMEOUT_SECONDS if timeout is None else timeout
    proc = subprocess.Popen(
        cmd,
        cwd=Path(__file__).resolve().parents[4],
        # Discovery primitives are automation-only. Inheriting a terminal here
        # lets tools such as msgvault start an implicit browser OAuth flow and
        # wait for a hidden callback until the six-hour child timeout. Explicit
        # authorization belongs to the setup primitive and its consent gate.
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
        emit_progress(timeout_message)
    for thread in threads:
        thread.join(timeout=1)
    payload = parse_last_json("".join(stdout_chunks))
    stderr = "".join(stderr_chunks)
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()
    return code, payload, stderr


def py_cmd(script: str, *args: str) -> list[str]:
    return [sys.executable, script, *args]
