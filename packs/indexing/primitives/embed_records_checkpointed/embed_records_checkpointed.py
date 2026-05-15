#!/usr/bin/env python3
"""Checkpointed no-spend embedding stage for local indexing.

Default provider is ``local-fake``: deterministic 1536-dim unit-ish vectors
from record text. It is intentionally not semantically equivalent to OpenAI, but
it preserves vector-backed artifact shape and exercises local DuckDB vector
search without spend. ``openai`` is gated and not implemented here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from packs.indexing.lib.io import read_json, read_jsonl, write_json  # noqa: E402

DIMENSION = 1536


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def text_for_record(record: dict[str, Any], fields: list[str]) -> str:
    parts: list[str] = []
    for field in fields:
        value = record.get(field)
        if isinstance(value, list):
            parts.extend(clean(v) for v in value if clean(v))
        elif isinstance(value, dict):
            parts.append(json.dumps(value, sort_keys=True))
        elif clean(value):
            parts.append(clean(value))
    return "\n".join(parts)


def fake_vector(text: str, dimension: int = DIMENSION) -> list[float]:
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    values: list[float] = []
    counter = 0
    while len(values) < dimension:
        digest = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        for idx in range(0, len(digest), 2):
            raw = int.from_bytes(digest[idx : idx + 2], "big")
            values.append((raw / 65535.0) * 2.0 - 1.0)
            if len(values) == dimension:
                break
        counter += 1
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [round(v / norm, 8) for v in values]


def atomic_write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    count = 0
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                count += 1
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return count


def checkpoint_path(output_dir: Path) -> Path:
    return output_dir / "checkpoint.json"


def chunk_path(output_dir: Path, chunk_index: int) -> Path:
    return output_dir / "chunks" / f"embeddings.{chunk_index:06d}.jsonl"


def load_state(output_dir: Path, input_path: Path, checkpoint_every: int, provider: str, force: bool) -> dict[str, Any]:
    if force and output_dir.exists():
        import shutil

        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cp = checkpoint_path(output_dir)
    if cp.exists():
        return read_json(cp)
    state = {
        "status": "running",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "input": str(input_path),
        "output_dir": str(output_dir),
        "checkpoint_every": checkpoint_every,
        "provider": provider,
        "dimension": DIMENSION,
        "input_rows_processed": 0,
        "embeddings_written": 0,
        "chunks_written": 0,
    }
    write_json(cp, state)
    return state


def save_state(output_dir: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    write_json(checkpoint_path(output_dir), state)


def iter_unprocessed(input_path: Path, start_index: int) -> Iterable[tuple[int, dict[str, Any]]]:
    for idx, row in enumerate(read_jsonl(input_path), start=1):
        if idx <= start_index:
            continue
        yield idx, row


def finalize(output_dir: Path, output_path: Path, state: dict[str, Any]) -> dict[str, Any]:
    chunks = sorted((output_dir / "chunks").glob("embeddings.*.jsonl")) if (output_dir / "chunks").exists() else []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for chunk in chunks:
        for row in read_jsonl(chunk):
            rid = clean(row.get("id"))
            if rid and rid not in seen:
                seen.add(rid)
                rows.append(row)
    rows.sort(key=lambda row: clean(row.get("id")))
    atomic_write_jsonl(output_path, rows)
    state["status"] = "completed"
    state["completed_at"] = now_iso()
    state["embeddings_written"] = len(rows)
    save_state(output_dir, state)
    manifest = {
        "status": "completed",
        "stage": "embed_records_checkpointed",
        "provider": state.get("provider"),
        "provider_equivalence": "shape_compatible_not_semantic_openai_equivalent" if state.get("provider") == "local-fake" else state.get("provider"),
        "dimension": DIMENSION,
        "checkpoint": str(checkpoint_path(output_dir)),
        "chunks": [str(path) for path in chunks],
        "output": str(output_path),
        "counts": {
            "input_rows_processed": state.get("input_rows_processed", 0),
            "embeddings": len(rows),
            "chunks_written": len(chunks),
        },
    }
    write_json(output_dir / "manifest.json", manifest)
    return manifest


def run(args: argparse.Namespace) -> dict[str, Any]:
    input_path = Path(args.input)
    output_dir = Path(args.output_dir)
    output_path = Path(args.output)
    if args.provider != "local-fake":
        if args.provider == "openai" and not getattr(args, "allow_paid", False):
            raise SystemExit("embedding provider 'openai' requires --allow-paid; no paid API was called")
        raise SystemExit(f"embedding provider '{args.provider}' is not implemented; no paid API was called")
    if not input_path.exists():
        raise SystemExit(f"missing input JSONL: {input_path}")
    fields = [field for field in str(args.text_fields).split(",") if field]
    state = load_state(output_dir, input_path, int(args.checkpoint_every), args.provider, bool(args.force))
    if state.get("status") == "completed" and output_path.exists() and not args.force:
        manifest = output_dir / "manifest.json"
        return read_json(manifest) if manifest.exists() else {"status": "completed", "output": str(output_path)}

    batch: list[dict[str, Any]] = []
    chunks_this_run = 0
    for idx, record in iter_unprocessed(input_path, int(state.get("input_rows_processed") or 0)):
        rid = clean(record.get(args.id_field))
        if rid:
            text = text_for_record(record, fields) or rid
            out = {
                "id": rid,
                "embedding": fake_vector(text),
                "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
            for field in str(args.copy_fields or "").split(","):
                if field and field in record:
                    out[field] = record[field]
            batch.append(out)
        state["input_rows_processed"] = idx
        if len(batch) >= int(args.checkpoint_every):
            chunk_index = int(state.get("chunks_written") or 0) + 1
            written = atomic_write_jsonl(chunk_path(output_dir, chunk_index), batch)
            state["chunks_written"] = chunk_index
            state["embeddings_written"] = int(state.get("embeddings_written") or 0) + written
            save_state(output_dir, state)
            batch = []
            chunks_this_run += 1
            if args.stop_after_chunks and chunks_this_run >= args.stop_after_chunks:
                return {
                    "status": "partial",
                    "checkpoint": str(checkpoint_path(output_dir)),
                    "chunks_written_total": state["chunks_written"],
                    "input_rows_processed": state["input_rows_processed"],
                    "embeddings_written": state["embeddings_written"],
                }
    if batch:
        chunk_index = int(state.get("chunks_written") or 0) + 1
        written = atomic_write_jsonl(chunk_path(output_dir, chunk_index), batch)
        state["chunks_written"] = chunk_index
        state["embeddings_written"] = int(state.get("embeddings_written") or 0) + written
        save_state(output_dir, state)
    return finalize(output_dir, output_path, state)


def status(args: argparse.Namespace) -> dict[str, Any]:
    cp = checkpoint_path(Path(args.output_dir))
    return read_json(cp) if cp.exists() else {"status": "missing", "checkpoint": str(cp)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run")
    run_p.add_argument("--input", required=True)
    run_p.add_argument("--output", required=True)
    run_p.add_argument("--output-dir", required=True)
    run_p.add_argument("--id-field", default="id")
    run_p.add_argument("--text-fields", required=True, help="Comma-separated text fields")
    run_p.add_argument("--copy-fields", default="", help="Comma-separated fields to copy into output rows")
    run_p.add_argument("--checkpoint-every", type=int, default=1000)
    run_p.add_argument("--provider", choices=["local-fake", "openai"], default="local-fake")
    run_p.add_argument("--allow-paid", action="store_true")
    run_p.add_argument("--force", action="store_true")
    run_p.add_argument("--stop-after-chunks", type=int)
    run_p.set_defaults(func=run)
    status_p = sub.add_parser("status")
    status_p.add_argument("--output-dir", required=True)
    status_p.set_defaults(func=status)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    emit(args.func(args))


if __name__ == "__main__":
    main()
