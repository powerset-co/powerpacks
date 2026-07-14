#!/usr/bin/env python3
"""Shared helpers for the per-vertical Modal sandbox runners.

Each vertical gets its own runner (run_linkedin.py, run_indexing.py) so no
single orchestrator grows unbounded; this module holds only the pieces they
genuinely share: volume status writes and the key-union cache merge.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_status(run_vol: Path, payload: dict) -> None:
    run_vol.mkdir(parents=True, exist_ok=True)
    tmp = run_vol / "status.json.tmp"
    tmp.write_text(json.dumps(payload | {"updated_at": now_iso()}, indent=2))
    tmp.replace(run_vol / "status.json")


def row_key(row: dict, key_fields: tuple[str, ...]) -> str:
    for field in key_fields:
        value = str(row.get(field) or "").strip()
        if value:
            return f"{field}={value}"
    return ""


def _qident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _cache_key_sql(alias: str, key_fields: tuple[str, ...]) -> str:
    fields = [
        f"CASE WHEN NULLIF(trim(CAST({alias}.{_qident(field)} AS VARCHAR)), '') IS NOT NULL "
        f"THEN '{field}=' || trim(CAST({alias}.{_qident(field)} AS VARCHAR)) END"
        for field in key_fields
    ]
    return f"COALESCE({', '.join(fields)})"


def merge_parquet_cache_file(
    new_rows_path: Path,
    cache_path: Path,
    key_fields: tuple[str, ...],
) -> tuple[int, int]:
    """Atomically key-union a native Parquet run artifact into its cache."""
    import duckdb  # type: ignore

    if new_rows_path.suffix.lower() != ".parquet":
        raise ValueError(f"Parquet caches require Parquet run artifacts: {new_rows_path}")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.parent / f".{cache_path.name}.tmp-{new_rows_path.stat().st_ino}"
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(":memory:")
    try:
        con.execute("SET enable_progress_bar=false")
        con.execute(f"SET threads={int(os.getenv('POWERPACKS_CACHE_THREADS', '8'))}")
        con.execute("SET memory_limit='12GB'")
        con.execute("SET preserve_insertion_order=false")
        new_source = f"SELECT * FROM read_parquet({_sql_literal(str(new_rows_path))})"
        new_count = int(con.execute(f"SELECT count(*) FROM ({new_source})").fetchone()[0])
        if not new_count:
            kept_count = 0
            if cache_path.exists() and cache_path.stat().st_size:
                kept_count = int(con.execute(
                    f"SELECT count(*) FROM read_parquet({_sql_literal(str(cache_path))})"
                ).fetchone()[0])
            return 0, kept_count
        kept_count = 0
        if cache_path.exists() and cache_path.stat().st_size:
            old_source = f"read_parquet({_sql_literal(str(cache_path))})"
        else:
            old_source = ""
        if old_source:
            new_key = _cache_key_sql("new", key_fields)
            old_key = _cache_key_sql("old", key_fields)
            kept_count = int(con.execute(
                f"SELECT count(*) FROM {old_source} old WHERE {old_key} IS NULL "
                f"OR NOT EXISTS (SELECT 1 FROM ({new_source}) new WHERE {new_key} = {old_key})"
            ).fetchone()[0])
            merged = (
                f"SELECT * FROM ({new_source}) new UNION ALL BY NAME "
                f"SELECT old.* FROM {old_source} old WHERE {old_key} IS NULL "
                f"OR NOT EXISTS (SELECT 1 FROM ({new_source}) new WHERE {new_key} = {old_key})"
            )
        else:
            merged = new_source
        con.execute(
            f"COPY ({merged}) TO {_sql_literal(str(tmp))} "
            "(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 16384)"
        )
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        con.close()
    tmp.replace(cache_path)
    return new_count, kept_count


def merge_cache_file(
    new_rows_path: Path,
    cache_path: Path,
    key_fields: tuple[str, ...],
) -> tuple[int, int]:
    """Union-merge like-formatted caches, with new rows winning shared keys.

    JSONL merges stream through Python; native Parquet run artifacts merge in
    DuckDB. Atomic replacement prevents corruption from interrupted writes.
    """
    if cache_path.suffix.lower() == ".parquet":
        return merge_parquet_cache_file(new_rows_path, cache_path, key_fields)

    seen: set[str] = set()
    tmp = cache_path.parent / (cache_path.name + f".tmp-{new_rows_path.stat().st_ino}")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    new_count = 0
    kept_count = 0
    with tmp.open("w", encoding="utf-8") as out:
        with new_rows_path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                key = row_key(json.loads(line), key_fields)
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                out.write(line + "\n")
                new_count += 1
        if cache_path.exists():
            with cache_path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    key = row_key(json.loads(line), key_fields)
                    if key and key in seen:
                        continue
                    if key:
                        seen.add(key)
                    out.write(line + "\n")
                    kept_count += 1
    tmp.replace(cache_path)
    return new_count, kept_count


def merge_file_dir(src_dir: Path, cache_dir: Path) -> tuple[int, int]:
    """Union-merge file-per-key caches (e.g. profile_cache_v2 keyed by slug
    filename): copy files absent from the shared cache, leave existing ones.
    Returns (added, existing)."""
    import shutil

    cache_dir.mkdir(parents=True, exist_ok=True)
    added = 0
    existing = 0
    if not src_dir.is_dir():
        return 0, 0
    for src in src_dir.iterdir():
        if not src.is_file():
            continue
        dest = cache_dir / src.name
        if dest.exists():
            existing += 1
            continue
        shutil.copyfile(src, dest)
        added += 1
    return added, existing
