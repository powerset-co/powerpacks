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
- ``ArtifactStat`` / ``ArtifactFingerprints`` / ``ManifestDocument`` — a manifest read
  back as typed values. Readers never walk the `fingerprints` block by key: they ask
  the document for a declared artifact's stat (``document.output(path).rows``).
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
  2026-09-23 (typed manifest reads): added `ArtifactStat` + `ManifestDocument`,
    and their `_stat_group` parse. Every computed `fingerprints` entry was already
    a fixed shape, so a manifest READER no longer walks it with `.get`: status.py
    and friends read `document.status`, `document.updated_at`, and
    `document.output(declared).rows`. The parse stays here (the manifest writer's
    own module); `manifest_fingerprints` below still guesses for the unconverted
    stages.
  2026-07-26 (per-node IO stats): the `output_paths` parameter added below is gone
    again. A converted node now computes its whole `fingerprints` block from its
    declarations (`pipeline/contract.py:Node.artifact_stats`, which also counts
    `rows` per row-model artifact) and passes it in as `payload["fingerprints"]`,
    so nothing was left calling the parameter. The row-model knowledge stays in
    `contract.py`; this module stays generic.
  2026-07-25 (declared contract): `write_stage_manifest` also accepts the pydantic
    `StageManifest` payloads of `pipeline/contract.py` (anything with
    `to_payload()`), and both it and `manifest_fingerprints` took an optional
    `output_paths` — a converted Node passed its DECLARED output paths instead of
    letting `collect_artifact_paths` sniff strings and consult a key allowlist.
    `StagePayload` stays for the unconverted verticals (messages, twitter).
  2026-07-23 (audit class-sharing): moved here from discover/common.py so stages
    outside discover can share the typed-manifest base without a cross-stage import.
    discover/common.py now re-exports StagePayload + write_stage_manifest.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
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
    """Fingerprint a stage manifest's input and output artifact paths.

    The outputs are GUESSED out of the payload — any string under `artifacts` plus
    a hardcoded allowlist of key names — so a stage whose output key is not on that
    list goes silently unfingerprinted. That is the price of not declaring: a
    converted `pipeline/contract.py:Node` computes the block from its declared
    inputs/outputs (`Node.artifact_stats`, which also counts rows) and passes it in
    as `payload["fingerprints"]`, and only the unconverted stages (twitter, the
    wacli leaf) still land here."""
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


@dataclass(frozen=True)
class ArtifactStat:
    """One entry of a manifest's `fingerprints` block: a declared artifact's
    on-disk state. `from_record` is the parse of the JSON we wrote — the one
    place a raw record dict is read; every consumer gets attributes."""

    path: str
    exists: bool
    size: int = 0
    mtime_ns: int = 0
    sha256: str = ""
    rows: int | None = None

    @classmethod
    def from_record(cls, path: str, record: Any) -> "ArtifactStat":
        record = record if isinstance(record, dict) else {}
        rows = record.get("rows")
        return cls(
            path=str(record.get("path") or path),
            exists=record.get("exists") is True,
            size=int(record.get("size") or 0),
            mtime_ns=int(record.get("mtime_ns") or 0),
            sha256=str(record.get("sha256") or ""),
            rows=rows if isinstance(rows, int) else None,
        )

    def to_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {"path": self.path, "exists": self.exists}
        if self.exists:
            record.update({"size": self.size, "mtime_ns": self.mtime_ns, "sha256": self.sha256})
        if self.rows is not None:
            record["rows"] = self.rows
        return record


# The manifest keys that are the CONTRACT (typed above), not a stage's payload.
_MANIFEST_CONTRACT_KEYS = frozenset({"fingerprints", "updated_at", "created_at"})


@dataclass(frozen=True)
class ArtifactFingerprints:
    """A manifest's whole `fingerprints` block, typed by declared path."""

    input_artifacts: dict[str, ArtifactStat] = field(default_factory=dict)
    output_artifacts: dict[str, ArtifactStat] = field(default_factory=dict)

    @classmethod
    def from_record(cls, value: Any) -> "ArtifactFingerprints":
        record = value if isinstance(value, dict) else {}
        return cls(
            input_artifacts=_stat_group(record.get("input_artifacts")),
            output_artifacts=_stat_group(record.get("output_artifacts")),
        )

    def stats(self) -> list[ArtifactStat]:
        return [*self.input_artifacts.values(), *self.output_artifacts.values()]

    def to_record(self) -> dict[str, Any]:
        return {
            "input_artifacts": {path: stat.to_record() for path, stat in self.input_artifacts.items()},
            "output_artifacts": {path: stat.to_record() for path, stat in self.output_artifacts.items()},
        }


@dataclass(frozen=True)
class ManifestDocument:
    """A stage `manifest.json` read back as typed values: status, timestamp, and
    the declared artifacts' stats. `payload` carries the stage-specific fields for
    the stage's own typed payload to validate; nothing here keys off a raw dict."""

    path: Path
    status: str
    updated_at: str
    fingerprints: ArtifactFingerprints
    payload: dict[str, Any]

    @classmethod
    def read(cls, path: Path) -> "ManifestDocument":
        raw = read_json(path, {}) or {}
        raw = raw if isinstance(raw, dict) else {}
        return cls(
            path=Path(path),
            status=str(raw.get("status") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            fingerprints=ArtifactFingerprints.from_record(raw.get("fingerprints")),
            payload={key: value for key, value in raw.items() if key not in _MANIFEST_CONTRACT_KEYS},
        )

    def output(self, declared_path: str) -> ArtifactStat | None:
        return self.fingerprints.output_artifacts.get(declared_path)

    def input(self, declared_path: str) -> ArtifactStat | None:
        return self.fingerprints.input_artifacts.get(declared_path)

    @property
    def present(self) -> bool:
        return bool(self.status)


def _stat_group(value: Any) -> dict[str, ArtifactStat]:
    if not isinstance(value, dict):
        return {}
    return {str(key): ArtifactStat.from_record(str(key), record) for key, record in value.items()}


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


def write_stage_manifest(path: Path, payload: "dict[str, Any] | StagePayload | Any") -> dict[str, Any]:
    """Write one stage's manifest (fingerprinted, no-op when unchanged).

    Accepts the vertical's typed payload — the pydantic `StageManifest` of
    `pipeline/contract.py` (preferred) or the older `StagePayload` dataclass — or
    its dict form. A payload that already carries `fingerprints` keeps them: that
    is how a converted node's declaration-driven stats (`Node.artifact_stats`)
    reach the manifest instead of the guessed block below."""
    if isinstance(payload, StagePayload) or hasattr(payload, "to_payload"):
        payload = payload.to_payload()
    existing = read_json(path, {}) or {}
    payload = dict(payload)
    payload["fingerprints"] = payload.get("fingerprints") or manifest_fingerprints(payload, existing.get("fingerprints") if isinstance(existing.get("fingerprints"), dict) else None)
    if existing and stable_manifest_signature(existing) == stable_manifest_signature(payload):
        return existing
    payload["updated_at"] = payload.get("updated_at") or now_iso()
    write_json(path, payload)
    return payload
