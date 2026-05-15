#!/usr/bin/env python3
"""Checkpointed local role-enrichment stage for Powerpacks indexing.

This is the first crash-safe enrichment-stage port: it consumes flattened people
JSONL, emits Aleph-shaped role enrichment artifacts, and checkpoints every N
input rows/position batches. The default provider is local/no-spend and only
produces deterministic fallback enrichment; model/TLM-backed doc2query and role
taxonomy mapping should be wired behind an explicit paid provider later.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from packs.indexing.lib.io import read_json, read_jsonl, write_json  # noqa: E402
from packs.indexing.lib.text import dense_text  # noqa: E402

WORD_RE = re.compile(r"[a-z0-9]+")
DEFAULT_CHECKPOINT_EVERY = 1000

TRACK_SKILLS = {
    "engineering": ["software engineering", "system design", "technical leadership"],
    "product": ["product strategy", "roadmapping", "user research"],
    "sales": ["sales", "go to market", "customer development"],
    "marketing": ["marketing", "growth", "demand generation"],
    "finance": ["finance", "accounting", "financial planning"],
    "operations": ["operations", "process improvement", "business operations"],
    "investing": ["investing", "venture capital", "portfolio support"],
    "founder": ["company building", "fundraising", "leadership"],
    "data": ["data analysis", "machine learning", "analytics"],
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


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


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def get_positions(person: dict[str, Any]) -> list[dict[str, Any]]:
    positions = person.get("work_experiences")
    if isinstance(positions, list):
        return [item for item in positions if isinstance(item, dict)]
    # Aleph flattened position grain uses a single nested position object.
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


def normalize_hash_text(value: str) -> str:
    return re.sub(r"\s+", " ", clean(value).lower()).strip()


def title_hash(title: str, description: str) -> str:
    # Aleph contract: stable MD5 of normalized title+description; exact upstream
    # separator is not available in the copied seed, so this stage records the
    # hash version in manifest for migration if the upstream helper is ported.
    payload = f"{normalize_hash_text(title)}::{normalize_hash_text(description)}"
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:16]


def role_track(title: str) -> str:
    t = title.lower()
    if any(word in t for word in ["data scientist", "machine learning", " ml ", "ai scientist", "analytics"]):
        return "data"
    if any(word in t for word in ["engineer", "developer", "architect", "cto", "technology", "technical", "scientist", "data", "ai", "ml"]):
        return "engineering"
    if "product" in t or t in {"cpo"}:
        return "product"
    if any(word in t for word in ["sales", "revenue", "account executive", "cro", "business development"]):
        return "sales"
    if any(word in t for word in ["marketing", "growth", "demand gen", "brand", "cmo"]):
        return "marketing"
    if any(word in t for word in ["finance", "cfo", "accounting", "controller"]):
        return "finance"
    if any(word in t for word in ["operations", "operator", "coo", "chief of staff", "general manager"]):
        return "operations"
    if any(word in t for word in ["investor", "partner", "venture", "principal"]):
        return "investing"
    if any(word in t for word in ["founder", "co-founder", "cofounder", "ceo", "chief executive"]):
        return "founder"
    return ""


def seniority_band(title: str) -> str:
    t = title.lower()
    if re.search(r"\b(co-?founder|cofounder|founder|owner)\b", t):
        return "owner"
    if re.search(r"\b(ceo|cto|cfo|coo|cpo|cro|cmo|chief|president)\b", t):
        return "c-suite"
    if re.search(r"\b(svp|evp|vp|vice president)\b", t):
        return "vice-president"
    if "director" in t or "head of" in t:
        return "director"
    if "manager" in t or "lead" in t:
        return "manager"
    if "senior" in t or "staff" in t or "principal" in t:
        return "senior-ic"
    return "ic" if t else ""


def role_ids_for(title: str, track: str) -> list[str]:
    t = title.lower()
    out: list[str] = []
    shortcuts = [
        ("founder", r"\b(co-?founder|cofounder|founder|founding)\b"),
        ("chief_executive_officer", r"\b(ceo|chief executive officer)\b"),
        ("chief_technology_officer", r"\b(cto|chief technology officer)\b"),
        ("chief_financial_officer", r"\b(cfo|chief financial officer)\b"),
        ("chief_operating_officer", r"\b(coo|chief operating officer)\b"),
        ("chief_product_officer", r"\b(cpo|chief product officer)\b"),
        ("chief_revenue_officer", r"\b(cro|chief revenue officer)\b"),
        ("chief_marketing_officer", r"\b(cmo|chief marketing officer)\b"),
        ("software_engineer", r"\b(software engineer|developer|backend|frontend|full stack)\b"),
        ("product_manager", r"\b(product manager|product lead|head of product)\b"),
        ("data_scientist", r"\b(data scientist|machine learning|ml engineer|ai scientist)\b"),
        ("sales_leader", r"\b(vp sales|sales director|account executive|revenue)\b"),
    ]
    for role_id, pattern in shortcuts:
        if re.search(pattern, t):
            out.append(role_id)
    if track and track not in {"founder"}:
        out.append(track)
    return list(dict.fromkeys(out))


def inferred_skills_for(title: str, description: str, track: str) -> list[str]:
    skills = list(TRACK_SKILLS.get(track, []))
    tokens = set(WORD_RE.findall(f"{title} {description}".lower()))
    if {"python", "django", "flask"} & tokens:
        skills.append("python")
    if {"typescript", "javascript", "react"} & tokens:
        skills.append("typescript")
    if {"kubernetes", "aws", "cloud"} & tokens:
        skills.append("cloud infrastructure")
    return list(dict.fromkeys(skills))


def doc2query_for(title: str, company: str, track: str, seniority: str, skills: list[str]) -> list[str]:
    parts = [title, track, seniority, company, *skills]
    text = " ".join(part for part in parts if part)
    queries = [text] if text else []
    if track:
        queries.append(f"{track} leader")
    if seniority in {"owner", "c_suite", "vice_president", "director"}:
        queries.append("executive leadership")
    return list(dict.fromkeys(q for q in queries if q.strip()))


def enrich_role(person: dict[str, Any], position: dict[str, Any]) -> dict[str, Any] | None:
    title = title_from_position(person, position)
    if not title:
        return None
    description = description_from_position(position)
    company = company_from_position(position)
    track = role_track(title)
    seniority = seniority_band(title)
    skills = inferred_skills_for(title, description, track)
    role_ids = role_ids_for(title, track)
    doc2query = doc2query_for(title, company, track, seniority, skills)
    return {
        "title_hash": title_hash(title, description),
        "raw_title": title,
        "description": description,
        "expanded_title": title,
        "role_ids": role_ids,
        "seniority_band": seniority,
        "role_track": track,
        "role_type": track,
        "specialization": "",
        "cluster": track,
        "doc2query": doc2query,
        "inferred_skills": skills,
        "dense_text": dense_text([title, description, company, person.get("headline"), person.get("summary")]),
        "provider": "local_deterministic_no_spend",
        "provider_equivalence": "shape_compatible_not_tlm_equivalent",
    }


def default_state(flattened: Path, output_dir: Path, checkpoint_every: int) -> dict[str, Any]:
    return {
        "status": "running",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "flattened": str(flattened),
        "output_dir": str(output_dir),
        "checkpoint_every": checkpoint_every,
        "input_rows_processed": 0,
        "positions_seen": 0,
        "unique_roles_written": 0,
        "chunks_written": 0,
        "seen_title_hashes": [],
        "provider": "local_deterministic_no_spend",
        "hash_contract": "md5(normalized_title + '::' + normalized_description)",
    }


def state_path(output_dir: Path) -> Path:
    return output_dir / "checkpoint.json"


def load_state(flattened: Path, output_dir: Path, checkpoint_every: int, force: bool) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    sp = state_path(output_dir)
    if force and output_dir.exists():
        shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
    if sp.exists():
        return read_json(sp)
    state = default_state(flattened, output_dir, checkpoint_every)
    write_json(sp, state)
    return state


def save_state(output_dir: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    write_json(state_path(output_dir), state)


def chunk_path(output_dir: Path, chunk_index: int) -> Path:
    return output_dir / "chunks" / f"roles.{chunk_index:06d}.jsonl"


def append_chunk(output_dir: Path, chunk_index: int, rows: list[dict[str, Any]]) -> int:
    return atomic_write_jsonl(chunk_path(output_dir, chunk_index), rows)


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
            if not th or th in seen:
                continue
            seen.add(th)
            roles.append(row)
    roles.sort(key=lambda row: row["title_hash"])
    role_fields = ["title_hash", "raw_title", "description", "cluster", "role_ids", "seniority_band", "role_type", "role_track", "specialization", "doc2query", "inferred_skills", "dense_text"]
    shaped_roles = [{field: row.get(field, [] if field in {"role_ids", "doc2query", "inferred_skills"} else "") for field in role_fields} for row in roles]
    roles_path = output_dir / "roles_with_dense_text_remapped.jsonl"
    raw_titles_path = output_dir / "raw_titles.jsonl"
    mapping_path = output_dir / "role_mapping.csv"
    atomic_write_jsonl(roles_path, shaped_roles)
    atomic_write_jsonl(raw_titles_path, ({"title_hash": row["title_hash"], "raw_title": row["raw_title"], "description": row.get("description", "")} for row in shaped_roles))
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
    manifest = {
        "status": "completed",
        "stage": "enrich_roles_checkpointed",
        "provider": state.get("provider"),
        "provider_equivalence": "shape_compatible_not_tlm_equivalent",
        "input": state.get("flattened"),
        "checkpoint": str(state_path(output_dir)),
        "checkpoint_every": state.get("checkpoint_every"),
        "chunks": [str(path) for path in chunks],
        "artifacts": {
            "roles_with_dense_text_remapped": str(roles_path),
            "raw_titles": str(raw_titles_path),
            "role_mapping": str(mapping_path),
        },
        "counts": {
            "input_rows_processed": state.get("input_rows_processed", 0),
            "positions_seen": state.get("positions_seen", 0),
            "unique_roles": len(shaped_roles),
            "chunks_written": len(chunks),
        },
        "missing_paid_provider_hooks": [
            "TLM/LLM doc2query generation",
            "production role taxonomy classifier for role_ids/specialization/cluster",
            "quality/confidence scoring for inferred_skills",
        ],
    }
    write_json(output_dir / "manifest.json", manifest)
    return manifest


def run(args: argparse.Namespace) -> dict[str, Any]:
    flattened = Path(args.flattened)
    output_dir = Path(args.output_dir)
    if not flattened.exists():
        raise SystemExit(f"missing flattened input: {flattened}")
    if args.provider != "local":
        raise SystemExit("Only --provider local is available without explicit spend approval/API wiring")
    state = load_state(flattened, output_dir, args.checkpoint_every, args.force)
    if state.get("status") == "completed" and not args.force:
        manifest_path = output_dir / "manifest.json"
        return read_json(manifest_path) if manifest_path.exists() else {"status": "completed", "checkpoint": str(state_path(output_dir))}

    seen_hashes = set(state.get("seen_title_hashes") or [])
    batch: list[dict[str, Any]] = []
    chunks_started = int(state.get("chunks_written") or 0)
    chunks_this_run = 0
    started = time.time()

    for idx, person in iter_unprocessed_rows(flattened, int(state.get("input_rows_processed") or 0)):
        for position in get_positions(person):
            state["positions_seen"] = int(state.get("positions_seen") or 0) + 1
            role = enrich_role(person, position)
            if not role:
                continue
            th = role["title_hash"]
            if th in seen_hashes:
                continue
            seen_hashes.add(th)
            batch.append(role)
        state["input_rows_processed"] = idx
        if len(batch) >= args.checkpoint_every:
            chunk_index = int(state.get("chunks_written") or 0) + 1
            written = append_chunk(output_dir, chunk_index, batch)
            state["chunks_written"] = chunk_index
            state["unique_roles_written"] = int(state.get("unique_roles_written") or 0) + written
            state["seen_title_hashes"] = sorted(seen_hashes)
            save_state(output_dir, state)
            batch = []
            chunks_this_run += 1
            if args.stop_after_chunks and chunks_this_run >= args.stop_after_chunks:
                return {
                    "status": "partial",
                    "checkpoint": str(state_path(output_dir)),
                    "chunks_written_total": state["chunks_written"],
                    "input_rows_processed": state["input_rows_processed"],
                    "resume_command": f"uv run --project . python {Path(__file__).relative_to(ROOT)} run --flattened {flattened} --output-dir {output_dir} --checkpoint-every {args.checkpoint_every}",
                }

    if batch:
        chunk_index = int(state.get("chunks_written") or 0) + 1
        written = append_chunk(output_dir, chunk_index, batch)
        state["chunks_written"] = chunk_index
        state["unique_roles_written"] = int(state.get("unique_roles_written") or 0) + written
        state["seen_title_hashes"] = sorted(seen_hashes)
        save_state(output_dir, state)
    state["elapsed_seconds_last_run"] = round(time.time() - started, 3)
    manifest = finalize(output_dir, state)
    manifest["chunks_started_this_run"] = chunks_started
    return manifest


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
    run_p.add_argument("--provider", choices=["local", "tlm"], default="local")
    run_p.add_argument("--force", action="store_true")
    run_p.add_argument("--stop-after-chunks", type=int, help="Testing hook: stop after N chunks and leave checkpoint resumable")
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
