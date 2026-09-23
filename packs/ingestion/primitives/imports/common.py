#!/usr/bin/env python3
"""Shared helpers for import/enrich contact stages.

Changelog:
  2026-09-23 (typed manifest reads): added `ImportManifest`. The manifest is read
    once (`from_payload`) into attributes — status, updated_at, input, outputs,
    stats, and a typed `ArtifactFingerprints` block — so `import_manifest_current`
    and its callers (imports/status.py, both importers) no longer walk the document
    with `.get`. `fingerprint_matches` now takes an `ArtifactStat`, not a raw record.
    `write_manifest` still takes and returns the payload dict (that is the on-disk
    contract, shared with Deep Context / indexing callers); its internals read
    `existing` through the typed view.
  2026-07-23 (dead accounts.json registry): removed the account-registry readers
    `account_channel`/`account_config`/`linked_gmail_accounts`/`linkedin_csv_path`/
    `linkedin_source_user` — the `accounts.json` registry has no live writer of
    the gmail/linkedin_csv config they read, so every caller was dead. Dropped the
    now-unused `unique_strings`/`DEFAULT_BASE_DIR` imports with them.
  2026-07-23 (steps split): removed `load_gmail_import_steps` — the
    `importlib.util.spec_from_file_location` fossil that file-loaded
    gmail/import_steps.py as a synthetic module. `GmailImport` now lives in
    gmail/importer.py; consumers import it
    directly (normal package imports). The `importlib.util` / `sys` imports went
    with it.
  2026-07-24: removed the Gmail step-ledger constructor; imports persist only
    their output files and manifest.json.
  2026-07-23 (audit batch 18): import_steps.py moved home — from the discover
    package into this package's gmail/ vertical (it is import-stage code).
  2026-07-23 (audit batch 20A): package renamed `import_contacts_pipeline` →
    `imports` (Python reserved-word `import` could not be a package name). The
    dead local linkedin/importer.py was deleted and the Modal linkedin
    convert+enrich engine now lives at imports/linkedin/network_import.py.
  2026-07-23 (audit batch 21): directory helpers import updated from
    discover.directory → imports.directory (the module moved to this stage).
  2026-07-23 (audit consolidation): cross-vertical helpers now come from
    packs.ingestion.primitives.common — now_iso/read_json/write_json/
    unique_strings/sha256_file from common.jsonio, and DEFAULT_BASE_DIR/
    DEFAULT_DIRECTORY_CSV/DEFAULT_IMPORT_DIR from common.paths (kept as module
    globals so the existing mock.patch.object(import_common, ...) test hooks
    still bind). DEFAULT_ACCOUNTS/DEFAULT_PROFILE_CACHE_DIR moved to common.paths
    (their consumers import them from there directly).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import (
    now_iso,
    read_json,
    sha256_file,
    write_json,
)
from packs.ingestion.primitives.common.manifests import (
    ArtifactFingerprints,
    ArtifactStat,
)
from packs.ingestion.primitives.common.paths import (
    DEFAULT_DIRECTORY_CSV,
    DEFAULT_IMPORT_DIR,
)
from packs.shared.csv_io import CsvIO


IMPORT_MANIFEST_CURRENT_REASON = "import_manifest_current"


@dataclass(frozen=True)
class ImportManifest:
    """An import-stage ``manifest.json`` as typed values.

    `from_payload` is the only place the raw document is read; readers use
    attributes — `manifest.status`, `manifest.outputs["people_csv"]`,
    `manifest.fingerprints...` — and `to_payload` is exactly what is on disk, so a
    caller can re-emit it unchanged. `fingerprints` is typed (`ArtifactStat`), so a
    consumer never indexes into the `fingerprints` block by key.
    """

    source: str
    status: str
    updated_at: str
    reason: str
    input: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=dict)
    fingerprints: ArtifactFingerprints = field(default_factory=ArtifactFingerprints)
    noop: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def read(cls, source: str, import_dir: Path | None = None) -> "ImportManifest":
        """Parse `<import_dir>/<source>/manifest.json` (absent file -> a blank record)."""
        manifest = (import_dir or DEFAULT_IMPORT_DIR) / source / "manifest.json"
        payload = read_json(manifest, {}) or {}
        return cls.from_payload(source, payload)

    @classmethod
    def from_payload(cls, source: str, payload: Any) -> "ImportManifest":
        raw = payload if isinstance(payload, dict) else {}
        return cls(
            source=str(raw.get("source") or source),
            status=str(raw.get("status") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            reason=str(raw.get("reason") or ""),
            input=_record(raw.get("input")),
            outputs=_record(raw.get("outputs")),
            artifacts=_record(raw.get("artifacts")),
            stats={key: value if isinstance(value, int) else 0 for key, value in _record(raw.get("stats")).items()},
            fingerprints=ArtifactFingerprints.from_record(raw.get("fingerprints")),
            noop=raw.get("noop") is True,
            raw=dict(raw),
        )

    @property
    def present(self) -> bool:
        """True when a manifest document was actually found on disk."""
        return bool(self.raw)

    def output_path(self, key: str = "people_csv") -> str:
        return str(self.outputs.get(key) or "")

    def matches_input(self, expected: dict[str, Any]) -> bool:
        return all(self.input.get(key) == value for key, value in expected.items())

    def to_payload(self) -> dict[str, Any]:
        return dict(self.raw)


def _record(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _previous(stat: ArtifactStat | None) -> dict[str, Any] | None:
    return stat.to_record() if stat else None


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
    return {
        "path": str(path),
        "exists": True,
        "size": stat.st_size,
        "mtime_ns": mtime_ns,
        "sha256": sha256_file(path),
    }


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
        if text.startswith(".powerpacks/") or Path(text).exists():
            paths.append(text)
    return list(dict.fromkeys(paths))


def manifest_fingerprints(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    document = ImportManifest.from_payload("", payload)
    previous = ArtifactFingerprints.from_record(existing)
    input_paths = collect_artifact_paths(document.input)
    output_paths = collect_artifact_paths({"outputs": document.outputs, "artifacts": document.artifacts})
    return ArtifactFingerprints(
        input_artifacts={
            path: ArtifactStat.from_record(path, artifact_fingerprint(path, _previous(previous.input_artifacts.get(path))))
            for path in input_paths
        },
        output_artifacts={
            path: ArtifactStat.from_record(path, artifact_fingerprint(path, _previous(previous.output_artifacts.get(path))))
            for path in output_paths
        },
    ).to_record()


def stable_manifest_signature(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the whole manifest payload without volatile timestamp fields."""
    signature = dict(payload)
    signature.pop("updated_at", None)
    signature.pop("created_at", None)
    return signature


def write_manifest(source: str, payload: dict[str, Any], import_dir: Path | None = None) -> dict[str, Any]:
    import_dir = (import_dir or DEFAULT_IMPORT_DIR) / source
    manifest = import_dir / "manifest.json"
    existing = read_json(manifest, {}) or {}
    submitted = ImportManifest.from_payload(source, payload)
    document = {
        "source": source,
        "status": submitted.status or "completed",
        **payload,
    }
    document["fingerprints"] = payload.get("fingerprints") or manifest_fingerprints(
        payload, ImportManifest.from_payload(source, existing).fingerprints.to_record(),
    )
    if existing and stable_manifest_signature(existing) == stable_manifest_signature(document):
        return existing
    document["updated_at"] = payload.get("updated_at") or now_iso()
    write_json(manifest, document)
    return document


def fingerprint_matches(path_text: str, fingerprint: ArtifactStat) -> bool:
    """True when the file at `path_text` still matches its recorded stat."""
    return ArtifactStat.from_record(path_text, artifact_fingerprint(path_text, fingerprint.to_record())) == fingerprint


def is_shared_directory_csv(path_text: str) -> bool:
    if str(path_text) == str(DEFAULT_DIRECTORY_CSV):
        return True
    try:
        return Path(path_text).resolve() == DEFAULT_DIRECTORY_CSV.resolve()
    except (OSError, RuntimeError):
        return False


def import_manifest_current(
    source: str,
    expected_input: dict[str, Any] | None = None,
    import_dir: Path | None = None,
) -> ImportManifest | None:
    """The on-disk manifest when this source's import is still current, else None.

    Current means `status: completed`, the expected input keys match, and every
    fingerprinted artifact still matches on disk (the shared `directory.csv` is
    excluded; at least one artifact must exist).
    """
    existing = ImportManifest.read(source, import_dir)
    if existing.status != "completed":
        return None
    if expected_input and not existing.matches_input(expected_input):
        return None
    saw_file = False
    for fingerprint in existing.fingerprints.stats():
        if is_shared_directory_csv(fingerprint.path) or not fingerprint.exists:
            continue
        saw_file = True
        if not fingerprint_matches(fingerprint.path, fingerprint):
            return None
    if not saw_file:
        return None
    return replace(
        existing,
        noop=True,
        reason=IMPORT_MANIFEST_CURRENT_REASON,
        raw={**existing.raw, "noop": True, "reason": IMPORT_MANIFEST_CURRENT_REASON},
    )


def csv_count(path_text: str) -> int:
    """Data rows in the CSV at `path_text` (0 for "" or a missing file)."""
    return CsvIO.count_rows(Path(str(path_text or "")))
