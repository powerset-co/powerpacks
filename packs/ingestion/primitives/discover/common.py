#!/usr/bin/env python3
"""Discover-stage helpers: CSV I/O, account state, and stage manifests.

Holds only the things unique to the discover stage — the fingerprinted LF CSV
reader/writer, the linked-account state accessors (superset variant that reads
both `accounts` and `channels` groups), the artifact/manifest fingerprint
helpers, and the typed `StagePayload` + `write_stage_manifest` contract. The
cross-vertical json/proc/paths/contact-field helpers live in
`packs.ingestion.primitives.common`.

Changelog:
  2026-07-23 (audit batch 16): deleted the orchestrator-only ledger/step
    machinery after `discover.py` was removed; renamed the progress prefix from
    [discover-contacts] to [discover].
  2026-07-23 (audit consolidation): moved the cross-vertical helpers out — now_iso
    / emit / read_json / write_json / parse_last_json / unique_strings /
    sha256_file to common.jsonio, run_cmd / py_cmd / emit_progress to common.proc,
    DEFAULT_BASE_DIR / DEFAULT_DIRECTORY_CSV / DEFAULT_MSGVAULT_DB to common.paths,
    and deleted the local parse_jsonish (callers import it from
    schemas/people_schema). This module keeps only discover-stage code.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import csv
import io
import re
import sys
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import now_iso, read_json, sha256_file, write_json  # noqa: E402
from packs.shared.csv_io import CsvIO  # noqa: E402

GMAIL_INTERACTION_CALCULATION_VERSION = "msgvault-interactions-v2"


def read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read a CSV into `(fieldnames, rows)`, tolerating BOMs and bad bytes."""
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        reader = CsvIO.dict_reader(handle)
        fields = list(reader.fieldnames or [])
        rows = [{str(key): value or "" for key, value in row.items() if key is not None} for row in reader]
    return fields, rows


def write_csv_rows(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    """Write rows with LF line endings, skipping the write when bytes are unchanged."""
    path.parent.mkdir(parents=True, exist_ok=True)
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


def read_accounts(path: Path) -> dict[str, Any]:
    """Read the packaged accounts.json state, `{}` when missing/invalid."""
    return read_json(path, {}) or {}


def account_channel(accounts: dict[str, Any], name: str) -> dict[str, Any]:
    """Return a channel's record, checking both the `accounts` and `channels` groups."""
    for key in ("accounts", "channels"):
        group = accounts.get(key)
        if isinstance(group, dict) and isinstance(group.get(name), dict):
            return group[name]
    return {}


def account_config(accounts: dict[str, Any], name: str) -> dict[str, Any]:
    """Return a channel's `config` sub-dict, `{}` when absent."""
    channel = account_channel(accounts, name)
    cfg = channel.get("config")
    return cfg if isinstance(cfg, dict) else {}


def channel_is_linked(accounts: dict[str, Any], name: str) -> bool:
    """True when a channel is linked (status == linked, or linked flag without skip)."""
    channel = account_channel(accounts, name)
    status = str(channel.get("status") or "").strip().lower()
    if status == "linked":
        return True
    return bool(channel.get("linked") is True) and not bool(channel.get("skipped"))


def ordered_unique(values: list[Any]) -> list[str]:
    """Order-preserving de-dup of a list into stripped, non-empty strings."""
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def collect_artifact_paths(value: Any) -> list[str]:
    """Recursively collect `.powerpacks/`-relative or absolute path strings from a payload."""
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


def artifact_fingerprint(path_text: str, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Size/mtime/sha256 fingerprint of a file, reusing `existing` when unchanged."""
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
    """Fingerprint a stage manifest's input and output artifact paths."""
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


def source_slug(value: str) -> str:
    """Filesystem-safe slug for a source label (`source` when empty)."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (value or "").strip().lower()).strip("-._")
    return slug or "source"
