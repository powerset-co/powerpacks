"""Conservative content/code fingerprints for local search-index builds."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[3]
LOCAL_DUCKDB_SCHEMA_VERSION = "local-duckdb-v1"


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def operator_scope(operator_id: str | None = None, default_operator_id: str | None = None) -> dict[str, str]:
    return {
        "operator_id": operator_id or default_operator_id or "local:user",
        "default_operator_id": default_operator_id or operator_id or "local:user",
    }


def operator_scope_slug(scope: dict[str, Any]) -> str:
    raw = str(scope.get("operator_id") or scope.get("default_operator_id") or "local:user")
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw).strip(".-_").lower()
    digest = sha256_json(scope)[:12]
    if not slug or len(slug) > 40 or "@" in raw:
        slug = "operator"
    return f"{slug}-{digest}"


def _hash_paths(paths: Iterable[Path]) -> str:
    h = hashlib.sha256()
    for path in sorted({p.resolve() for p in paths if p.exists()}):
        rel = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(sha256_file(path).encode("ascii"))
        h.update(b"\0")
    return h.hexdigest()


def pipeline_code_fingerprint() -> str:
    candidates = [
        *ROOT.glob("packs/indexing/lib/*.py"),
        *ROOT.glob("packs/indexing/primitives/build_processing_pipeline/*.py"),
        *ROOT.glob("packs/indexing/primitives/build_local_duckdb/*.py"),
        *ROOT.glob("packs/indexing/primitives/validate_local_search_index/*.py"),
        *ROOT.glob("packs/indexing/primitives/validate_index_parity/*.py"),
        ROOT / "packs/search/primitives/hydrate_people/hydrate_people.py",
        ROOT / "packs/search/primitives/lib/local_hydration_store.py",
    ]
    return _hash_paths(candidates)


def contract_fingerprint() -> str:
    return _hash_paths(ROOT.glob("packs/search/contracts/turbopuffer/*.namespace.json"))


def local_duckdb_store_fingerprint() -> str:
    return _hash_paths([ROOT / "packs/search/primitives/lib/local_duckdb_store.py"])


def input_metadata(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    row_count = 0
    if p.exists():
        import csv
        import sys
        try:
            csv.field_size_limit(sys.maxsize)
        except OverflowError:  # pragma: no cover
            csv.field_size_limit(2**31 - 1)
        with p.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
            reader = csv.reader(handle)
            header_seen = False
            for _row in reader:
                if not header_seen:
                    header_seen = True
                    continue
                row_count += 1
    return {
        "path": str(p),
        "sha256": sha256_file(p) if p.exists() else None,
        "size_bytes": p.stat().st_size if p.exists() else 0,
        "row_count": row_count,
    }


def build_fingerprints(
    input_path: str | Path,
    *,
    operator_id: str | None = None,
    default_operator_id: str | None = None,
    limit: int | None = None,
    extra_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scope = operator_scope(operator_id, default_operator_id)
    params = {"limit": limit, **(extra_params or {})}
    parts = {
        "input": input_metadata(input_path),
        "operator_scope": scope,
        "operator_scope_slug": operator_scope_slug(scope),
        "params": params,
        "pipeline_code": pipeline_code_fingerprint(),
        "contracts": contract_fingerprint(),
        "local_duckdb_store_schema": local_duckdb_store_fingerprint(),
        "local_duckdb_schema": LOCAL_DUCKDB_SCHEMA_VERSION,
    }
    parts["combined"] = sha256_json(parts)
    return parts
