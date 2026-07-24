#!/usr/bin/env python3
"""Typed stage-manifest contract, shared across ingestion stages.

The stage-agnostic pieces that used to live in `discover/common.py` but are the
manifest contract for ANY stage's typed payload (discover verticals today; the
home a cross-stage payload should reach for without importing a sibling stage's
`common`):

- ``StagePayload`` — the base for the per-vertical typed manifest dataclasses (see
  each vertical's ``models.py``). A payload is a dataclass, not an ad-hoc dict, so a
  stage cannot invent fields on the fly.
- ``write_stage_manifest`` — writes one stage's manifest (fingerprinted, no-op when
  unchanged), accepting the typed StagePayload or its dict form.
- ``manifest_fingerprints`` / ``artifact_fingerprint`` / ``collect_artifact_paths`` /
  ``stable_manifest_signature`` — the size/mtime/sha256 fingerprint helpers
  write_stage_manifest builds on. The output-artifact key list is a superset of the
  discover verticals' output names; keys a stage does not emit are simply absent, so
  the helper is stage-agnostic (only ``input`` + ``artifacts`` are fingerprinted for
  stages that do not use the discover-specific output keys).

Note: ``imports/common.py`` keeps its OWN fingerprint chain + ``write_manifest``.
Those diverge on purpose — a different ``collect_artifact_paths`` (dedups, and
matches by on-disk existence rather than absolute-path prefix) and a source-derived
manifest path — and are NOT this contract; do not fold them together.

Changelog:
  2026-07-23 (audit class-sharing): moved here from discover/common.py so stages
    outside discover can share the typed-manifest base without a cross-stage import.
    discover/common.py now re-exports StagePayload + write_stage_manifest.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
import sys

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import now_iso, read_json, sha256_file, write_json  # noqa: E402


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
