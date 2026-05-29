#!/usr/bin/env python3
"""Checkpointed OpenAI embedding stage for local indexing.

This primitive has no fake/mock provider. It either:
- performs a dry-run estimate without writing embeddings,
- replays explicitly supplied real embeddings via --input-embeddings, or
- calls OpenAI when --provider openai and --allow-paid are both set.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from packs.indexing.lib.io import read_json, read_jsonl, write_json  # noqa: E402

DEFAULT_DIMENSION = 1536
DEFAULT_MODEL = "text-embedding-3-small"
DEFAULT_COST_PER_1K_TOKENS = 0.00002


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def load_input_embeddings(path: str | None, id_field: str, embedding_field: str) -> dict[str, list[float]]:
    if not path:
        return {}
    input_path = Path(path)
    if not input_path.exists():
        raise SystemExit(f"missing input embeddings: {input_path}")
    out: dict[str, list[float]] = {}
    for row in read_jsonl(input_path):
        rid = clean(row.get(id_field))
        embedding = row.get(embedding_field)
        if rid and isinstance(embedding, list):
            out[rid] = [float(v) for v in embedding]
    return out


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


def openai_embeddings(
    texts: list[str],
    *,
    api_key: str,
    base_url: str,
    model: str,
    dimension: int = DEFAULT_DIMENSION,
    timeout: int = 60,
    max_retries: int = 3,
) -> list[list[float]]:
    if not texts:
        return []
    url = base_url.rstrip("/") + "/embeddings"
    payload: dict[str, Any] = {"model": model, "input": texts}
    if dimension:
        payload["dimensions"] = dimension
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    last_error = ""
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - explicit paid provider path
                response_payload = json.loads(response.read().decode("utf-8"))
            data = response_payload.get("data") if isinstance(response_payload, dict) else None
            if not isinstance(data, list) or len(data) != len(texts):
                raise RuntimeError("OpenAI embeddings response row count mismatch")
            ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
            embeddings: list[list[float]] = []
            for item in ordered:
                embedding = item.get("embedding") if isinstance(item, dict) else None
                if not isinstance(embedding, list) or (dimension and len(embedding) != dimension):
                    raise RuntimeError(f"OpenAI embeddings dimension mismatch: {len(embedding) if isinstance(embedding, list) else 'invalid'} != {dimension}")
                embeddings.append([float(value) for value in embedding])
            return embeddings
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            last_error = f"HTTP {exc.code}: {detail}"
            if exc.code not in {408, 409, 429, 500, 502, 503, 504} or attempt >= max_retries:
                break
        except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            last_error = str(exc)
            if attempt >= max_retries:
                break
        time.sleep(min(2**attempt, 30))
    raise RuntimeError(f"OpenAI embeddings request failed after retries: {last_error}")


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


def load_state(output_dir: Path, input_path: Path, checkpoint_every: int, provider: str, force: bool, dimension: int) -> dict[str, Any]:
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
        "dimension": dimension,
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


def copy_fields(record: dict[str, Any], fields: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field in str(fields or "").split(","):
        if field and field in record:
            out[field] = record[field]
    return out


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
        "dimension": state.get("dimension", DEFAULT_DIMENSION),
        "checkpoint": str(checkpoint_path(output_dir)),
        "chunks": [str(path) for path in chunks],
        "output": str(output_path),
        "counts": {"input_rows_processed": state.get("input_rows_processed", 0), "embeddings": len(rows), "chunks_written": len(chunks)},
    }
    write_json(output_dir / "manifest.json", manifest)
    return manifest


def dry_run(args: argparse.Namespace) -> dict[str, Any]:
    input_path = Path(args.input)
    fields = [field for field in str(args.text_fields).split(",") if field]
    rows = 0
    tokens = 0
    for record in read_jsonl(input_path):
        if clean(record.get(args.id_field)):
            rows += 1
            tokens += estimate_tokens(text_for_record(record, fields) or clean(record.get(args.id_field)))
    cost_per_1k = float(getattr(args, "cost_per_1k_tokens", DEFAULT_COST_PER_1K_TOKENS))
    batch_size = int(getattr(args, "api_batch_size", 128) or 128)
    return {
        "status": "dry-run",
        "stage": "embed_records_checkpointed",
        "provider": "openai" if not getattr(args, "input_embeddings", None) else "input-embeddings",
        "rows": rows,
        "estimated_tokens": tokens,
        "estimated_batches": (rows + batch_size - 1) // batch_size,
        "estimated_cost_usd": round(tokens / 1000.0 * cost_per_1k, 6),
        "would_write": [str(Path(args.output_dir) / "checkpoint.json"), str(args.output)],
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    input_path = Path(args.input)
    output_dir = Path(args.output_dir)
    output_path = Path(args.output)
    if not input_path.exists():
        raise SystemExit(f"missing input JSONL: {input_path}")
    if getattr(args, "dry_run", False):
        return dry_run(args)
    if args.provider != "openai":
        raise SystemExit("embedding provider must be 'openai'; no fake/mock/local provider is available")
    fields = [field for field in str(args.text_fields).split(",") if field]
    dimension = int(getattr(args, "dimension", DEFAULT_DIMENSION) or DEFAULT_DIMENSION)
    input_embeddings = load_input_embeddings(getattr(args, "input_embeddings", None), getattr(args, "input_id_field", None) or args.id_field, getattr(args, "input_embedding_field", "embedding"))
    if input_embeddings:
        provider = "input-embeddings"
        api_key = ""
    else:
        provider = "openai"
        if not getattr(args, "allow_paid", False):
            raise SystemExit("embedding provider 'openai' requires --allow-paid; no paid API was called")
        api_key = getattr(args, "api_key", None) or os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise SystemExit("embedding provider 'openai' requires OPENAI_API_KEY or --api-key; no paid API was called")
    state = load_state(output_dir, input_path, int(args.checkpoint_every), provider, bool(args.force), dimension)
    if state.get("status") == "completed" and output_path.exists() and not args.force:
        manifest = output_dir / "manifest.json"
        return read_json(manifest) if manifest.exists() else {"status": "completed", "output": str(output_path)}

    api_batch_size = int(getattr(args, "api_batch_size", 128) or 128)
    base_url = getattr(args, "base_url", None) or os.getenv("POWERPACKS_OPENAI_BASE", "https://api.openai.com/v1")
    model = getattr(args, "model", None) or os.getenv("POWERPACKS_OPENAI_EMBEDDING_MODEL", DEFAULT_MODEL)
    pending: list[tuple[str, str, dict[str, Any]]] = []
    chunks_this_run = 0

    def flush() -> None:
        nonlocal pending, chunks_this_run
        if not pending:
            return
        outputs: list[dict[str, Any]] = []
        if input_embeddings:
            for rid, text, copied in pending:
                embedding = input_embeddings.get(rid)
                if embedding is None:
                    raise SystemExit(f"missing input embedding for id={rid}")
                outputs.append({"id": rid, "embedding": embedding, "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(), **copied})
        else:
            for start in range(0, len(pending), api_batch_size):
                group = pending[start : start + api_batch_size]
                embeddings = openai_embeddings([item[1] for item in group], api_key=api_key, base_url=base_url, model=model, dimension=dimension)
                for (rid, text, copied), embedding in zip(group, embeddings):
                    outputs.append({"id": rid, "embedding": embedding, "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(), **copied})
        chunk_index = int(state.get("chunks_written") or 0) + 1
        written = atomic_write_jsonl(chunk_path(output_dir, chunk_index), outputs)
        state["chunks_written"] = chunk_index
        state["embeddings_written"] = int(state.get("embeddings_written") or 0) + written
        save_state(output_dir, state)
        pending = []
        chunks_this_run += 1

    for idx, record in iter_unprocessed(input_path, int(state.get("input_rows_processed") or 0)):
        rid = clean(record.get(args.id_field))
        if rid:
            text = text_for_record(record, fields) or rid
            pending.append((rid, text, copy_fields(record, str(args.copy_fields or ""))))
        state["input_rows_processed"] = idx
        if len(pending) >= int(args.checkpoint_every):
            flush()
            if args.stop_after_chunks and chunks_this_run >= args.stop_after_chunks:
                return {"status": "partial", "checkpoint": str(checkpoint_path(output_dir)), "chunks_written_total": state["chunks_written"], "input_rows_processed": state["input_rows_processed"], "embeddings_written": state["embeddings_written"]}
    flush()
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
    run_p.add_argument("--text-fields", required=True)
    run_p.add_argument("--copy-fields", default="")
    run_p.add_argument("--checkpoint-every", type=int, default=1000)
    run_p.add_argument("--provider", choices=["openai"], default="openai")
    run_p.add_argument("--api-key")
    run_p.add_argument("--base-url")
    run_p.add_argument("--model", default=None)
    run_p.add_argument("--dimension", type=int, default=DEFAULT_DIMENSION)
    run_p.add_argument("--api-batch-size", type=int, default=128)
    run_p.add_argument("--cost-per-1k-tokens", type=float, default=DEFAULT_COST_PER_1K_TOKENS)
    run_p.add_argument("--input-embeddings", help="Precomputed real embedding JSONL; not a provider")
    run_p.add_argument("--input-id-field", default=None)
    run_p.add_argument("--input-embedding-field", default="embedding")
    run_p.add_argument("--allow-paid", action="store_true")
    run_p.add_argument("--dry-run", action="store_true")
    run_p.add_argument("--force", action="store_true")
    run_p.add_argument("--stop-after-chunks", type=int)
    run_p.set_defaults(func=run)
    status_p = sub.add_parser("status")
    status_p.add_argument("--output-dir", required=True)
    status_p.set_defaults(func=status)
    return parser


def main() -> None:
    load_dotenv(ROOT / ".env", override=False)
    args = build_parser().parse_args()
    emit(args.func(args))


if __name__ == "__main__":
    main()
