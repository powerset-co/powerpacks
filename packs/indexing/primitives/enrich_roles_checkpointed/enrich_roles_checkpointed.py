#!/usr/bin/env python3
"""Checkpointed OpenAI role-enrichment stage for Powerpacks indexing.

No fake/mock/local role provider is exposed. The stage either dry-runs, replays
explicit --input-classifications, or calls OpenAI with --allow-paid.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
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

from packs.indexing.lib.io import read_json, read_jsonl, write_json  # noqa: E402
from packs.indexing.lib.text import dense_text  # noqa: E402

DEFAULT_CHECKPOINT_EVERY = 1000
DEFAULT_MODEL = "gpt-4o-mini"
ROLE_FIELDS = ["title_hash", "raw_title", "description", "cluster", "role_ids", "seniority_band", "role_type", "role_track", "specialization", "doc2query", "inferred_skills", "dense_text"]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def get_positions(person: dict[str, Any]) -> list[dict[str, Any]]:
    positions = person.get("work_experiences")
    if isinstance(positions, list):
        return [item for item in positions if isinstance(item, dict)]
    position = person.get("position")
    if isinstance(position, dict):
        return [position]
    return []


def title_from_position(person: dict[str, Any], position: dict[str, Any]) -> str:
    for key in ("title", "position_title", "position", "role", "raw_title"):
        value = clean(position.get(key) or person.get(key))
        if value:
            return value
    return ""


def description_from_position(position: dict[str, Any]) -> str:
    return clean(position.get("description") or position.get("summary"))


def company_from_position(position: dict[str, Any]) -> str:
    for key in ("company_name", "company", "organization", "employer"):
        value = position.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("company_name")
        value = clean(value)
        if value and not value.startswith("{"):
            return value
    return ""


def title_hash(title: str, description: str) -> str:
    raise RuntimeError("title_hash must come from upstream Aleph/DVC checkpoint data; no local fallback hash is allowed")


def shape_role(row: dict[str, Any]) -> dict[str, Any]:
    shaped = {field: row.get(field, [] if field in {"role_ids", "doc2query", "inferred_skills"} else "") for field in ROLE_FIELDS}
    for field in ["role_ids", "doc2query", "inferred_skills"]:
        if not isinstance(shaped[field], list):
            shaped[field] = [shaped[field]] if shaped[field] else []
    return shaped


def role_input(person: dict[str, Any], position: dict[str, Any]) -> dict[str, Any] | None:
    title = title_from_position(person, position)
    if not title:
        return None
    description = description_from_position(position)
    company = company_from_position(position)
    upstream_title_hash = clean(position.get("title_hash") or person.get("title_hash"))
    if not upstream_title_hash:
        raise RuntimeError(f"missing upstream title_hash for role {title!r}; run one-time Aleph bootstrap or copy the exact DVC hash stage first")
    return {
        "title_hash": upstream_title_hash,
        "raw_title": title,
        "description": description,
        "company_name": company,
        "headline": clean(person.get("headline")),
        "summary": clean(person.get("summary")),
        "dense_text": dense_text([title, description, company, person.get("headline"), person.get("summary")]),
    }


def load_input_classifications(path: str | None) -> dict[str, dict[str, Any]]:
    if not path:
        return {}
    input_path = Path(path)
    if not input_path.exists():
        raise SystemExit(f"missing input role classifications: {input_path}")
    out: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(input_path):
        th = clean(row.get("title_hash"))
        if th:
            out[th] = shape_role(row)
    return out


def role_prompt(role: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "Enrich a professional role for Aleph people search. Return only JSON with keys: "
                "role_ids (array of stable snake_case taxonomy IDs), seniority_band, role_track, role_type, "
                "specialization, cluster, doc2query (array of search expansions), inferred_skills (array)."
            ),
        },
        {"role": "user", "content": json.dumps(role, ensure_ascii=False, sort_keys=True)},
    ]


def call_openai_role_enrichment(role: dict[str, Any], *, api_key: str, base_url: str, model: str, timeout: int = 60, max_retries: int = 3) -> dict[str, Any]:
    payload = {"model": model, "response_format": {"type": "json_object"}, "messages": role_prompt(role), "temperature": 0}
    body = json.dumps(payload).encode("utf-8")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url = base_url.rstrip("/") + "/chat/completions"
    last_error = ""
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - explicit paid provider path
                result = json.loads(response.read().decode("utf-8"))
            content = (((result.get("choices") or [{}])[0].get("message") or {}).get("content") or "{}").strip()
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise RuntimeError("OpenAI role enrichment returned non-object JSON")
            return parsed
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            last_error = f"HTTP {exc.code}: {detail}"
            if exc.code not in {408, 409, 429, 500, 502, 503, 504} or attempt >= max_retries:
                break
        except (urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            if attempt >= max_retries:
                break
        time.sleep(min(2**attempt, 30))
    raise RuntimeError(f"OpenAI role enrichment failed after retries: {last_error}")


def merge_role(base: dict[str, Any], enrichment: dict[str, Any]) -> dict[str, Any]:
    row = {
        "title_hash": base["title_hash"],
        "raw_title": base["raw_title"],
        "description": base.get("description", ""),
        "dense_text": base.get("dense_text", ""),
        "cluster": enrichment.get("cluster", ""),
        "role_ids": enrichment.get("role_ids") or [],
        "seniority_band": enrichment.get("seniority_band", ""),
        "role_type": enrichment.get("role_type", ""),
        "role_track": enrichment.get("role_track", ""),
        "specialization": enrichment.get("specialization", ""),
        "doc2query": enrichment.get("doc2query") or [],
        "inferred_skills": enrichment.get("inferred_skills") or [],
    }
    return shape_role(row)


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


def state_path(output_dir: Path) -> Path:
    return output_dir / "checkpoint.json"


def chunk_path(output_dir: Path, chunk_index: int) -> Path:
    return output_dir / "chunks" / f"roles.{chunk_index:06d}.jsonl"


def default_state(flattened: Path, output_dir: Path, checkpoint_every: int, provider: str, input_classifications: str | None) -> dict[str, Any]:
    return {"status": "running", "created_at": now_iso(), "updated_at": now_iso(), "flattened": str(flattened), "output_dir": str(output_dir), "checkpoint_every": checkpoint_every, "input_rows_processed": 0, "positions_seen": 0, "unique_roles_written": 0, "chunks_written": 0, "seen_title_hashes": [], "provider": provider, "input_classifications": input_classifications, "hash_contract": "md5(normalized_title + '::' + normalized_description)"}


def load_state(flattened: Path, output_dir: Path, checkpoint_every: int, force: bool, provider: str, input_classifications: str | None) -> dict[str, Any]:
    if force and output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    sp = state_path(output_dir)
    if sp.exists():
        return read_json(sp)
    state = default_state(flattened, output_dir, checkpoint_every, provider, input_classifications)
    write_json(sp, state)
    return state


def save_state(output_dir: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    write_json(state_path(output_dir), state)


def iter_unprocessed_rows(flattened: Path, start_index: int) -> Iterable[tuple[int, dict[str, Any]]]:
    for idx, row in enumerate(read_jsonl(flattened), start=1):
        if idx <= start_index:
            continue
        yield idx, row


def finalize(output_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    chunks = sorted((output_dir / "chunks").glob("roles.*.jsonl")) if (output_dir / "chunks").exists() else []
    roles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for chunk in chunks:
        for row in read_jsonl(chunk):
            th = clean(row.get("title_hash"))
            if th and th not in seen:
                seen.add(th)
                roles.append(shape_role(row))
    roles.sort(key=lambda row: row["title_hash"])
    roles_path = output_dir / "roles_with_dense_text_remapped.jsonl"
    raw_titles_path = output_dir / "raw_titles.jsonl"
    mapping_path = output_dir / "role_mapping.csv"
    atomic_write_jsonl(roles_path, roles)
    atomic_write_jsonl(raw_titles_path, ({"title_hash": row["title_hash"], "raw_title": row["raw_title"], "description": row.get("description", "")} for row in roles))
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = mapping_path.with_name(f".{mapping_path.name}.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        import csv
        writer = csv.DictWriter(handle, fieldnames=["title_hash", "raw_title", "expanded_title", "seniority_band", "role_track"])
        writer.writeheader()
        for row in roles:
            writer.writerow({key: row.get(key, "") for key in writer.fieldnames or []})
    tmp.replace(mapping_path)
    state["status"] = "completed"
    state["completed_at"] = now_iso()
    state["unique_roles_written"] = len(roles)
    save_state(output_dir, state)
    manifest = {"status": "completed", "stage": "enrich_roles_checkpointed", "provider": state.get("provider"), "input": state.get("flattened"), "checkpoint": str(state_path(output_dir)), "checkpoint_every": state.get("checkpoint_every"), "chunks": [str(path) for path in chunks], "artifacts": {"roles_with_dense_text_remapped": str(roles_path), "raw_titles": str(raw_titles_path), "role_mapping": str(mapping_path)}, "counts": {"input_rows_processed": state.get("input_rows_processed", 0), "positions_seen": state.get("positions_seen", 0), "unique_roles": len(roles), "chunks_written": len(chunks)}}
    write_json(output_dir / "manifest.json", manifest)
    return manifest


def collect_role_inputs(flattened: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for person in read_jsonl(flattened):
        for position in get_positions(person):
            role = role_input(person, position)
            if role and role["title_hash"] not in seen:
                seen.add(role["title_hash"])
                out.append(role)
    return out


def dry_run(args: argparse.Namespace) -> dict[str, Any]:
    roles = collect_role_inputs(Path(args.flattened))
    tokens = sum(estimate_tokens(json.dumps(role, sort_keys=True)) for role in roles)
    return {"status": "dry-run", "stage": "enrich_roles_checkpointed", "provider": "input-classifications" if getattr(args, "input_classifications", None) else args.provider, "unique_roles": len(roles), "estimated_tokens": tokens, "estimated_calls": 0 if getattr(args, "input_classifications", None) else len(roles), "would_write": [str(Path(args.output_dir) / "checkpoint.json"), str(Path(args.output_dir) / "roles_with_dense_text_remapped.jsonl")]}


def run(args: argparse.Namespace) -> dict[str, Any]:
    flattened = Path(args.flattened)
    output_dir = Path(args.output_dir)
    if not flattened.exists():
        raise SystemExit(f"missing flattened input: {flattened}")
    if getattr(args, "dry_run", False):
        return dry_run(args)
    provider = "input-classifications" if getattr(args, "input_classifications", None) else str(args.provider)
    if args.provider not in {"openai", "tlm"}:
        raise SystemExit("role provider must be openai/tlm; no fake/mock/local provider is available")
    input_classifications = load_input_classifications(getattr(args, "input_classifications", None))
    if not input_classifications:
        if not getattr(args, "allow_paid", False):
            raise SystemExit(f"role provider '{args.provider}' requires --allow-paid; no paid API was called")
        api_key = getattr(args, "api_key", None) or os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise SystemExit("role provider requires OPENAI_API_KEY or --api-key; no paid API was called")
    else:
        api_key = ""
    base_url = getattr(args, "base_url", None) or os.getenv("POWERPACKS_OPENAI_BASE", "https://api.openai.com/v1")
    model = getattr(args, "model", None) or os.getenv("POWERPACKS_ROLE_OPENAI_MODEL", DEFAULT_MODEL)
    state = load_state(flattened, output_dir, args.checkpoint_every, args.force, provider, getattr(args, "input_classifications", None))
    if state.get("status") == "completed" and not args.force:
        manifest_path = output_dir / "manifest.json"
        return read_json(manifest_path) if manifest_path.exists() else {"status": "completed", "checkpoint": str(state_path(output_dir))}
    seen_hashes = set(state.get("seen_title_hashes") or [])
    batch: list[dict[str, Any]] = []
    chunks_this_run = 0
    started = time.time()
    for idx, person in iter_unprocessed_rows(flattened, int(state.get("input_rows_processed") or 0)):
        for position in get_positions(person):
            state["positions_seen"] = int(state.get("positions_seen") or 0) + 1
            base = role_input(person, position)
            if not base or base["title_hash"] in seen_hashes:
                continue
            if input_classifications:
                cached = input_classifications.get(base["title_hash"])
                if cached is None:
                    raise SystemExit(f"missing input role classification for title_hash={base['title_hash']}")
                role = merge_role(base, cached)
            else:
                try:
                    role = merge_role(base, call_openai_role_enrichment(base, api_key=api_key, base_url=base_url, model=model))
                except RuntimeError as exc:
                    raise SystemExit(str(exc)) from exc
            seen_hashes.add(base["title_hash"])
            batch.append(role)
        state["input_rows_processed"] = idx
        if len(batch) >= args.checkpoint_every:
            chunk_index = int(state.get("chunks_written") or 0) + 1
            written = atomic_write_jsonl(chunk_path(output_dir, chunk_index), batch)
            state["chunks_written"] = chunk_index
            state["unique_roles_written"] = int(state.get("unique_roles_written") or 0) + written
            state["seen_title_hashes"] = sorted(seen_hashes)
            save_state(output_dir, state)
            batch = []
            chunks_this_run += 1
            if args.stop_after_chunks and chunks_this_run >= args.stop_after_chunks:
                return {"status": "partial", "checkpoint": str(state_path(output_dir)), "chunks_written_total": state["chunks_written"], "input_rows_processed": state["input_rows_processed"]}
    if batch:
        chunk_index = int(state.get("chunks_written") or 0) + 1
        written = atomic_write_jsonl(chunk_path(output_dir, chunk_index), batch)
        state["chunks_written"] = chunk_index
        state["unique_roles_written"] = int(state.get("unique_roles_written") or 0) + written
        state["seen_title_hashes"] = sorted(seen_hashes)
        save_state(output_dir, state)
    state["elapsed_seconds_last_run"] = round(time.time() - started, 3)
    return finalize(output_dir, state)


def status(args: argparse.Namespace) -> dict[str, Any]:
    sp = state_path(Path(args.output_dir))
    if not sp.exists():
        return {"status": "missing", "checkpoint": str(sp)}
    state = read_json(sp)
    manifest = Path(args.output_dir) / "manifest.json"
    return {"status": state.get("status"), "checkpoint": str(sp), "state": state, "manifest_exists": manifest.exists()}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run")
    run_p.add_argument("--flattened", required=True)
    run_p.add_argument("--output-dir", required=True)
    run_p.add_argument("--checkpoint-every", type=int, default=DEFAULT_CHECKPOINT_EVERY)
    run_p.add_argument("--provider", choices=["openai", "tlm"], default="openai")
    run_p.add_argument("--input-classifications", help="Precomputed Aleph roles_with_dense_text_remapped.jsonl; not a provider")
    run_p.add_argument("--api-key")
    run_p.add_argument("--base-url")
    run_p.add_argument("--model", default=DEFAULT_MODEL)
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
    args = build_parser().parse_args()
    emit(args.func(args))


if __name__ == "__main__":
    main()
