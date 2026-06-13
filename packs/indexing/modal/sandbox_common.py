#!/usr/bin/env python3
"""Shared helpers for the per-vertical Modal sandbox runners.

Each vertical gets its own runner (run_linkedin.py, run_indexing.py) so no
single orchestrator grows unbounded; this module holds only the pieces they
genuinely share: volume status writes and the key-union cache merge.
"""
from __future__ import annotations

import json
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


def merge_cache_file(new_rows_path: Path, cache_path: Path, key_fields: tuple[str, ...]) -> tuple[int, int]:
    """Union-merge JSONL caches: new rows win for shared keys, existing cache
    rows for keys the run did not touch are preserved. Streaming with a
    seen-key set; atomic tmp+rename so concurrent runs cannot corrupt the file
    (a lost race only delays a row until the next run re-adds it)."""
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
