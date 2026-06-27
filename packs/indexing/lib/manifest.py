"""Manifest and cache helpers for local search-index builds."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from packs.indexing.lib.fingerprints import build_fingerprints, operator_scope_slug, sha256_file
from packs.indexing.lib.io import write_json

MANIFEST_NAME = "index-manifest.json"
LATEST_MANIFEST_NAME = "latest-manifest.json"
DUCKDB_NAME = "local-search.duckdb"
DUCKDB_SHA_NAME = "local-search.duckdb.sha256"
COMPAT_DIRS = ["unified", "profiles", "roles", "company", "education", "location", "summaries", "records", "stats"]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def manifest_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / MANIFEST_NAME


def read_manifest(path_or_run_dir: str | Path) -> dict[str, Any] | None:
    path = Path(path_or_run_dir)
    if path.is_dir():
        path = manifest_path(path)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_manifest(run_dir: str | Path, manifest: dict[str, Any]) -> Path:
    return write_json(manifest_path(run_dir), manifest)


def duckdb_checksum_file(db_path: str | Path) -> Path:
    db = Path(db_path)
    out = db.with_name(DUCKDB_SHA_NAME)
    out.write_text(sha256_file(db) + "\n", encoding="utf-8")
    return out


def latest_paths(output_dir: str | Path) -> dict[str, Path]:
    root = Path(output_dir)
    return {
        "duckdb": root / DUCKDB_NAME,
        "checksum": root / DUCKDB_SHA_NAME,
        "manifest": root / LATEST_MANIFEST_NAME,
    }


def promote_latest(run_dir: str | Path, output_dir: str | Path) -> dict[str, str]:
    rd = Path(run_dir)
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    paths = latest_paths(root)
    db = rd / DUCKDB_NAME
    if db.exists():
        _copy_file_atomic(db, paths["duckdb"])
        duckdb_checksum_file(paths["duckdb"])
    manifest = manifest_path(rd)
    if manifest.exists():
        _copy_file_atomic(manifest, paths["manifest"])
    return {key: str(value) for key, value in paths.items()}


def build_manifest(
    *,
    run_id: str,
    run_dir: str | Path,
    input_path: str | Path,
    operator_id: str | None = None,
    default_operator_id: str | None = None,
    limit: int | None = None,
    status: str = "partial",
    stages: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | None = None,
    validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fps = build_fingerprints(input_path, operator_id=operator_id, default_operator_id=default_operator_id, limit=limit)
    return {
        "schema_version": 1,
        "run_id": run_id,
        "run_dir": str(Path(run_dir)),
        "status": status,
        "created_at": now_iso(),
        "operator_id": operator_id or default_operator_id or "local:user",
        "input": fps["input"],
        "fingerprints": {key: value for key, value in fps.items() if key != "input"},
        "stages": stages or {},
        "artifacts": artifacts or {},
        "validation": validation or {},
    }


def cache_dir(output_dir: str | Path, manifest: dict[str, Any]) -> Path:
    fps = manifest.get("fingerprints") or {}
    scope = fps.get("operator_scope") or {"operator_id": manifest.get("operator_id") or "local:user"}
    slug = str(fps.get("operator_scope_slug") or operator_scope_slug(scope))
    combined = str(fps.get("combined") or "missing")
    return Path(output_dir) / "cache" / slug / combined


def manifest_ready(manifest: dict[str, Any] | None, base_dir: str | Path | None = None) -> bool:
    if not manifest or manifest.get("status") != "ready":
        return False
    validation = manifest.get("validation") or {}
    if not all(
        bool(validation.get(key))
        for key in ["contracts_ok", "duckdb_opened", "namespace_probes_ok", "hydration_parity_ok"]
    ):
        return False
    if base_dir is None:
        return True
    root = Path(base_dir)
    db = root / DUCKDB_NAME
    checksum = root / DUCKDB_SHA_NAME
    if not db.exists() or db.stat().st_size <= 0 or not checksum.exists():
        return False
    checksum_text = checksum.read_text(encoding="utf-8").strip()
    expected = checksum_text.split()[0] if checksum_text else ""
    return bool(expected) and sha256_file(db) == expected


def store_cache(run_dir: str | Path, output_dir: str | Path, manifest: dict[str, Any]) -> Path:
    dest = cache_dir(output_dir, manifest)
    tmp = dest.with_name(dest.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    _copy_compat_artifacts(Path(run_dir), tmp)
    if dest.exists():
        shutil.rmtree(dest)
    os.replace(tmp, dest)
    return dest


def restore_cache(cache: str | Path, run_dir: str | Path) -> None:
    src = Path(cache)
    dest = Path(run_dir)
    dest.mkdir(parents=True, exist_ok=True)
    _copy_compat_artifacts(src, dest)


def _copy_compat_artifacts(src: Path, dest: Path) -> None:
    for name in ["ledger.json", MANIFEST_NAME, DUCKDB_NAME, DUCKDB_SHA_NAME]:
        if (src / name).exists():
            _copy_file_atomic(src / name, dest / name)
    for dirname in COMPAT_DIRS:
        s = src / dirname
        if not s.exists():
            continue
        d = dest / dirname
        if d.exists():
            shutil.rmtree(d)
        shutil.copytree(s, d)


def _copy_file_atomic(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dest)
