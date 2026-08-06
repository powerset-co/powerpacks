#!/usr/bin/env python3
"""Research fixed contact dossiers through Parallel.ai.

Flow:
    research_queue.csv -> Parallel task group -> per-person raw/normalized JSON
    -> one fixed manifest.json -> optional SQLite projection

The official Parallel SDK owns transport. This primitive owns only input
shaping, synchronous task-group execution, paid-output reuse, and durable file
outputs. It creates no run ledger, task-group state file, or second manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from parallel import NotFoundError, Parallel

# Skills invoke this file directly as well as through package imports.
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import now_iso, write_json
from packs.ingestion.primitives.deep_context.common import load_env
from packs.ingestion.primitives.deep_context.db.projectors import project_manifest
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.prompts.loader import load_prompt
from packs.ingestion.primitives.imports.common import write_manifest
from packs.shared.csv_io import CsvIO


DEFAULT_BASE_URL = os.environ.get("POWERPACKS_PARALLEL_BASE_URL", "https://api.parallel.ai")
DEFAULT_BETA_HEADER = os.environ.get(
    "POWERPACKS_PARALLEL_BETA", "search-extract-2025-10-10"
)
DEFAULT_PROCESSOR = os.environ.get("POWERPACKS_PARALLEL_PROCESSOR", "core2x")
ALLOWED_PROCESSORS = frozenset({"core", "core2x", "pro"})
PROCESSOR_PRICING_USD = {"core": 0.025, "core2x": 0.05, "pro": 0.10}
PROCESSOR_LATENCY = {
    "core": ("60s-5min", "about 1-5 min once submitted"),
    "core2x": ("60s-10min", "about 10-15 min once submitted"),
    "pro": ("2-10min", "about 2-10 min once submitted"),
}
DEFAULT_OUTPUT_DIR = Path(".powerpacks/messages/research")
DEFAULT_BATCH_SIZE = 500
DEFAULT_POLL_INTERVAL = 15
DEFAULT_MAX_WAIT = 7200
DEFAULT_RESULT_WORKERS = 4
RESEARCH_INSTRUCTIONS = load_prompt("contact_research_instructions")
_SCHEMAS = json.loads(load_prompt("contact_research_schema"))
PERSON_RESEARCH_INPUT_SCHEMA: dict[str, Any] = _SCHEMAS["input"]
PERSON_RESEARCH_OUTPUT_SCHEMA: dict[str, Any] = _SCHEMAS["output"]
TASK_SPEC = {
    "instructions": RESEARCH_INSTRUCTIONS,
    "input_schema": {"json_schema": PERSON_RESEARCH_INPUT_SCHEMA},
    "output_schema": {"json_schema": PERSON_RESEARCH_OUTPUT_SCHEMA},
}


@dataclass(frozen=True)
class ResearchRunParams:
    """One explicit configuration door for an in-process research pass."""

    input_csv: Path
    output_dir: Path
    processor: str = DEFAULT_PROCESSOR
    manifest: str = ""
    api_key: str | None = None
    base_url: str = DEFAULT_BASE_URL
    beta_header: str = DEFAULT_BETA_HEADER
    batch_size: int = DEFAULT_BATCH_SIZE
    limit: int | None = None
    poll_interval: int = DEFAULT_POLL_INTERVAL
    max_wait: int = DEFAULT_MAX_WAIT
    workers: int = DEFAULT_RESULT_WORKERS
    api_timeout: int = 60
    on_progress: Callable[[dict[str, Any]], None] | None = None
    db: Db | None = None


class ParallelClient:
    """Official-SDK channel: submit, wait, and fetch one in-memory task group."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        beta_header: str = DEFAULT_BETA_HEADER,
    ) -> None:
        headers = {"parallel-beta": beta_header} if beta_header else None
        self._client = Parallel(api_key=api_key, base_url=base_url, default_headers=headers)

    def execute(
        self,
        inputs: list[dict[str, Any]],
        params: ResearchRunParams,
        on_status: Callable[[dict[str, Any]], None],
    ) -> tuple[int, dict[str, dict[str, Any]], list[str], dict[str, Any]]:
        group_id = str(self._client.task_group.create(
            metadata={"source": "powerpacks", "submitted_at": now_iso()}
        ).task_group_id)
        run_ids: list[str] = []
        for start in range(0, len(inputs), params.batch_size):
            response = self._client.task_group.add_runs(
                group_id, inputs=inputs[start : start + params.batch_size]
            )
            run_ids.extend(str(value) for value in response.run_ids)
        if not run_ids:
            return 0, {}, [], {}

        deadline = time.time() + params.max_wait
        final: dict[str, Any] = {}
        while time.time() < deadline:
            final = self._client.task_group.retrieve(group_id).status.model_dump()
            on_status(final.get("task_run_status_counts") or {})
            if final.get("is_active") is False:
                break
            time.sleep(params.poll_interval)

        def fetch(run_id: str) -> tuple[str, dict[str, Any]] | None:
            try:
                response = self._client.task_run.result(
                    run_id, api_timeout=params.api_timeout, timeout=params.api_timeout + 10
                )
            except NotFoundError:
                return None
            content = getattr(response.output, "content", None)
            result = content if isinstance(content, dict) else {"raw": str(content)}
            metadata = dict(response.run.metadata or {})
            return str(metadata.get("handle") or run_id), result

        results: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=params.workers) as pool:
            futures = {pool.submit(fetch, run_id): run_id for run_id in run_ids}
            for future in as_completed(futures):
                run_id = futures[future]
                try:
                    item = future.result()
                except Exception as exc:
                    errors.append(f"{run_id}: {type(exc).__name__}: {exc}"[:300])
                    continue
                if item is None:
                    errors.append(f"{run_id}: no payload")
                    continue
                handle, result = item
                results[handle] = result
        return len(run_ids), results, errors, final


def candidate_handle(row: dict[str, str]) -> str:
    """Return the stable fixed-directory key for one queue row."""
    handle = (row.get("handle") or "").strip()
    if handle:
        return handle
    email = (row.get("primary_email") or "").strip()
    if email:
        return email.split("@", 1)[0].lower().replace(".", "_")
    digits = re.sub(r"\D", "", row.get("phone_e164") or "")
    if digits:
        return f"phone-{digits[-10:]}"
    name = " ".join(
        value.strip()
        for value in (row.get("display_name") or row.get("first_name") or "", row.get("last_name") or "")
        if value.strip()
    ).lower()
    return re.sub(r"[^a-z0-9]+", "_", name).strip("_") or "unknown"


def build_input(row: dict[str, str], handle: str) -> dict[str, Any]:
    """Collapse a queue row into one dossier plus optional human guidance."""
    name = (row.get("display_name") or "").strip()
    if not name:
        name = " ".join(
            value.strip()
            for value in (row.get("first_name") or "", row.get("last_name") or "")
            if value.strip()
        )
    guidance = (row.get("retarget_hint") or "").strip()
    known = (row.get("known_info") or "").strip()
    if guidance and known.startswith(guidance):
        known = known[len(guidance) :].strip()
    lines = [f"Name: {name or handle}"]
    for label, value in (
        ("Relationship dossier", row.get("bio") or ""),
        ("Email", row.get("primary_email") or ""),
        ("Phone", row.get("phone_e164") or ""),
        ("Area code", row.get("area_code") or ""),
        ("Company domain", row.get("domain") or ""),
        ("Website", row.get("website_url") or ""),
        ("Additional context", known),
    ):
        text = str(value).strip()
        if text:
            lines.append(f"{label}: {text}")
    payload: dict[str, Any] = {"handle": handle, "dossier": "\n".join(lines)}
    if guidance:
        payload["guidance"] = guidance
    return payload


def _input_fingerprint(row: dict[str, str], handle: str) -> str:
    """Paid-cache key; stable canonical JSON is intentionally pinned."""
    data = json.dumps(
        build_input(row, handle), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def load_queue(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise SystemExit(f"input CSV not found: {path}")
    return CsvIO.read_dict_rows_normalized(path)


def filter_already_done(
    rows: list[dict[str, str]], output_dir: Path
) -> tuple[list[dict[str, str]], int]:
    """Reuse exact paid outputs; changed dossier/guidance overwrites the fixed path.

    Pre-rewrite outputs have no fingerprint and remain reusable so migration does
    not unexpectedly rebill existing paid work.
    """
    todo: list[dict[str, str]] = []
    skipped = 0
    seen: set[str] = set()
    for source in rows:
        row = dict(source)
        handle = candidate_handle(row)
        if handle in seen:
            continue
        seen.add(handle)
        row["handle"] = handle
        path = output_dir / handle / "01_research_parallel.json"
        if path.is_file():
            try:
                prior = json.loads(path.read_text(encoding="utf-8"))
                stored = str((prior.get("metadata") or {}).get("input_fingerprint") or "")
            except (AttributeError, json.JSONDecodeError, OSError):
                stored = "invalid"
            if not stored or stored == _input_fingerprint(row, handle):
                skipped += 1
                continue
        todo.append(row)
    return todo, skipped


def _json_array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _positions(result: dict[str, Any]) -> list[dict[str, Any]]:
    positions: list[dict[str, Any]] = []
    for value in _json_array(result.get("work_experience")):
        if isinstance(value, str):
            positions.append({
                "title": "", "company_name": value, "company_domain": None,
                "company_linkedin_url": None, "description": None, "start_date": None,
                "end_date": None, "is_current": False, "confidence": 0.5, "sources": [],
            })
            continue
        if not isinstance(value, dict):
            continue
        positions.append({
            "title": value.get("title") or value.get("position") or "",
            "company_name": next((str(value.get(key)) for key in (
                "company", "organization", "employer", "company_name", "name"
            ) if value.get(key)), ""),
            "company_domain": value.get("domain") or value.get("company_domain"),
            "company_linkedin_url": None,
            "description": value.get("description"),
            "start_date": value.get("start_date"),
            "end_date": value.get("end_date"),
            "is_current": value.get("current") or value.get("is_current", False),
            "confidence": value.get("confidence", 0.7),
            "sources": value.get("evidence") if isinstance(value.get("evidence"), list)
            else ([value["source"]] if value.get("source") else []),
        })
    return positions


def _education(result: dict[str, Any]) -> list[dict[str, Any]]:
    education: list[dict[str, Any]] = []
    for value in _json_array(result.get("education")):
        if isinstance(value, str):
            education.append({
                "school_name": value, "degree": None, "field_of_study": None,
                "start_year": None, "end_year": None, "confidence": 0.5, "source": "",
            })
            continue
        if not isinstance(value, dict):
            continue
        education.append({
            "school_name": next((str(value.get(key)) for key in (
                "school", "school_name", "institution", "university", "name"
            ) if value.get(key)), ""),
            "degree": value.get("degree"),
            "field_of_study": value.get("field") or value.get("field_of_study"),
            "start_year": value.get("start_year"),
            "end_year": value.get("end_year"),
            "confidence": value.get("confidence", 0.7),
            "source": str(value.get("evidence") or ""),
        })
    return education


def _quality(result: dict[str, Any]) -> tuple[float, list[str]]:
    positions = _json_array(result.get("work_experience"))
    education = _json_array(result.get("education"))
    score = 0.3 if result.get("real_name") else 0.0
    score += min(0.3, len(positions) * 0.1)
    score += min(0.2, len(education) * 0.1)
    score += 0.1 if result.get("location_city") else 0.0
    score += 0.1 if result.get("linkedin_url") else 0.0
    gaps = []
    for missing, label in (
        (not result.get("real_name"), "Real name not identified"),
        (not positions, "No work experience found"),
        (not education, "No education found"),
        (not result.get("location_city") and not result.get("location_country"), "Location unknown"),
        (not result.get("linkedin_url"), "No LinkedIn profile found"),
    ):
        if missing:
            gaps.append(label)
    return round(min(1.0, score), 2), gaps


def parallel_to_research_json(
    result: dict[str, Any],
    row: dict[str, str],
    handle: str,
    name: str,
    bio: str,
    *,
    research_method: str = "parallel-core2x",
) -> dict[str, Any]:
    """Normalize one provider result into the standing research artifact shape."""
    real_name = str(result.get("real_name") or name or handle)
    first, _, last = real_name.partition(" ")
    source_channel = (row.get("source_channel") or "phone").strip().lower()
    completeness, gaps = _quality(result)
    return {
        "research_id": f"{handle}-{date.today().isoformat()}",
        "query": f"@{handle} ({name}): {bio[:100]}",
        "status": "draft",
        "research_method": research_method,
        "person": {
            "full_name": real_name,
            "first_name": first,
            "last_name": last,
            "also_known_as": [handle, name] if real_name != name else [handle],
            "confidence": result.get("name_confidence", 0.3),
            "sources": [],
            "notes": result.get("name_evidence", ""),
        },
        "location": {
            "city": result.get("location_city") or "",
            "state": "",
            "country": result.get("location_country") or "",
            "raw": "",
            "confidence": 0.5 if result.get("location_city") or result.get("location_country") else 0.0,
            "source": "",
        },
        "headline": {
            "text": bio[:200] if bio else "",
            "confidence": 0.95 if bio else 0.0,
            "source": f"https://x.com/{handle}",
        },
        "summary": {
            "text": result.get("summary") or "",
            "confidence": 0.7,
            "source": "Parallel Deep Research",
        },
        "positions": _positions(result),
        "education": _education(result),
        "social": {
            "twitter_handle": handle if source_channel == "twitter" else None,
            "linkedin_url": result.get("linkedin_url"),
            "linkedin_status": "found" if result.get("linkedin_url") else "not_found",
            "github_url": result.get("github_url"),
            "personal_website": result.get("personal_website"),
            "primary_email": row.get("primary_email") if source_channel == "email" else None,
            "primary_phone": row.get("phone_e164") if source_channel == "phone" else None,
        },
        "metadata": {
            "total_sources_consulted": 0,
            "estimated_completeness": completeness,
            "gaps": gaps,
            "research_date": date.today().isoformat(),
            "research_method": research_method,
            "research_notes": result.get("research_notes") or "",
            "source_channel": source_channel or "unknown",
            "source_identifier": row.get("primary_email") or row.get("phone_e164") or handle,
            "input_fingerprint": _input_fingerprint(row, handle),
        },
    }


def _manifest_relative(manifest_path: Path, artifact_path: Path) -> str:
    try:
        return artifact_path.resolve().relative_to(manifest_path.parent.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"research artifact must be inside the manifest directory: {artifact_path}") from exc


def research_artifact_inventory(params: ResearchRunParams) -> list[dict[str, Any]]:
    """Name and hash the exact fixed paid outputs for explicit projection."""
    if params.db is None:
        return []
    manifest_path = Path(params.manifest) if params.manifest else params.output_dir / "manifest.json"
    owners = {row.person_id: row.parent_id for row in canonical_snapshot(params.db).people}
    inventory: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in load_queue(params.input_csv):
        handle = candidate_handle(row)
        if handle in seen:
            continue
        seen.add(handle)
        result_path = params.output_dir / handle / "01_research_parallel.json"
        if not result_path.is_file():
            continue
        try:
            person_ids = [
                str(value).strip().lower()
                for value in json.loads(row.get("source_person_ids") or "[]")
                if str(value).strip()
            ]
        except (json.JSONDecodeError, TypeError):
            person_ids = []
        parent_ids = {owners.get(person_id) for person_id in person_ids}
        if not person_ids:
            raise ValueError(f"research queue row has no person ids: {handle}")
        if None in parent_ids or len(parent_ids) != 1:
            raise ValueError(f"research queue ownership is unresolved: {handle}")
        candidate_key = str(
            row.get("source_candidate_public_identifier") or person_ids[0]
        ).strip().lower()
        entry: dict[str, Any] = {
            "kind": "research",
            "artifact_key": f"research:{handle}".lower(),
            "parent_id": next(iter(parent_ids)),
            "candidate_key": candidate_key,
            "public_identifier": candidate_key,
            "handle": handle,
            "person_ids": person_ids,
            "display_name": (row.get("display_name") or "").strip(),
            "candidate_origin": any(value.startswith("candidate:") for value in person_ids),
            "path": _manifest_relative(manifest_path, result_path),
            "sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
        }
        raw_path = params.output_dir / handle / "00_parallel_raw.json"
        if raw_path.is_file():
            entry.update({
                "raw_path": _manifest_relative(manifest_path, raw_path),
                "raw_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            })
        inventory.append(entry)
    return inventory


def _report_progress(
    params: ResearchRunParams, status: str, counts: dict[str, int], **extra: Any
) -> None:
    """Write the one canonical manifest, project it, then notify memory observers."""
    manifest_path = Path(params.manifest) if params.manifest else params.output_dir / "manifest.json"
    if manifest_path.name != "manifest.json":
        raise SystemExit("--manifest must end in manifest.json")
    try:
        current = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        current = {}
    payload = {
        **current,
        **extra,
        "stage": "enrich",
        "status": status,
        "counts": counts,
        "artifacts": research_artifact_inventory(params),
    }
    payload.pop("updated_at", None)
    payload.pop("created_at", None)
    write_manifest(manifest_path.parent.name, payload, import_dir=manifest_path.parent.parent)
    if params.db is not None:
        project_manifest(params.db, manifest_path)
    if params.on_progress:
        params.on_progress({"status": status, "counts": counts})


def _validate_processor(processor: str) -> str:
    if processor not in ALLOWED_PROCESSORS:
        raise SystemExit(
            f"processor '{processor}' is blocked for Powerpacks contact research; "
            f"allowed processors: {', '.join(sorted(ALLOWED_PROCESSORS))}"
        )
    return processor


def _api_key(explicit: str | None) -> str:
    load_env()
    value = explicit or os.environ.get("PARALLEL_API_KEY")
    if not value:
        raise SystemExit("PARALLEL_API_KEY not set (pass --api-key or add it to the repo .env)")
    return value


def _progress_counts(total: int, reused: int, provider: dict[str, Any]) -> dict[str, int]:
    completed = reused + sum(
        int(provider.get(key) or 0) for key in ("completed", "succeeded", "success")
    )
    failed = sum(
        int(provider.get(key) or 0)
        for key in ("failed", "error", "errored", "cancelled", "canceled")
    )
    completed = min(total, completed)
    failed = min(max(0, total - completed), failed)
    return {"total": total, "completed": completed,
            "pending": max(0, total - completed - failed), "failed": failed}


def run_research(params: ResearchRunParams) -> dict[str, Any]:
    """Run one synchronous paid pass; fixed completed outputs make reruns free."""
    processor = _validate_processor(params.processor)
    rows = load_queue(params.input_csv)
    todo, reused = filter_already_done(rows, params.output_dir)
    if params.limit is not None:
        todo = todo[: params.limit]
    total = reused + len(todo)
    if not todo:
        _report_progress(
            params,
            "research_complete",
            {"total": total, "completed": reused, "pending": 0, "failed": 0},
            provider_status={},
        )
        return {
            "primitive": "deep_research_contacts",
            "command": "run",
            "status": "no_work",
            "queue_rows": len(rows),
            "skipped_already_done": reused,
        }

    params.output_dir.mkdir(parents=True, exist_ok=True)
    inputs = [{
        "task_spec": TASK_SPEC,
        "input": build_input(row, row["handle"]),
        "metadata": {"handle": row["handle"]},
        "processor": processor,
    } for row in todo]
    api_key = _api_key(params.api_key)
    _report_progress(
        params, "running",
        {"total": total, "completed": reused, "pending": len(todo), "failed": 0},
        provider_status={"submitted": len(todo)},
    )

    def on_status(provider: dict[str, Any]) -> None:
        _report_progress(
            params, "running", _progress_counts(total, reused, provider),
            provider_status=provider,
        )
        print(f"[deep_research_contacts] poll status {provider}", file=sys.stderr, flush=True)

    try:
        run_count, results, errors, final_group = ParallelClient(
            api_key, params.base_url, params.beta_header
        ).execute(inputs, params, on_status)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:300]
        _report_progress(
            params, "failed",
            {"total": total, "completed": reused, "pending": 0, "failed": len(todo)},
            error=error,
        )
        return {"primitive": "deep_research_contacts", "command": "run",
                "status": "failed", "error": error}
    if not run_count:
        error = "Parallel returned no run ids"
        _report_progress(
            params,
            "failed",
            {"total": total, "completed": reused, "pending": 0, "failed": len(todo)},
            error=error,
        )
        return {"primitive": "deep_research_contacts", "command": "run", "status": "failed", "error": error}

    rows_by_handle = {row["handle"]: row for row in todo}

    found_name = found_linkedin = 0
    for handle, result in results.items():
        row = rows_by_handle.get(handle)
        if row is None:
            errors.append(f"{handle}: result did not match a submitted subject")
            continue
        person_dir = params.output_dir / handle
        person_dir.mkdir(parents=True, exist_ok=True)
        write_json(person_dir / "00_parallel_raw.json", result)
        normalized = parallel_to_research_json(
            result,
            row,
            handle,
            row.get("display_name") or handle,
            row.get("bio") or "",
            research_method=f"parallel-{processor}",
        )
        write_json(person_dir / "01_research_parallel.json", normalized)
        found_name += int(bool(result.get("real_name")))
        found_linkedin += int(bool(result.get("linkedin_url")))

    status = "completed" if not errors else "completed_with_errors"
    _report_progress(
        params,
        "research_complete" if not errors else status,
        {
            "total": total,
            "completed": reused + len(results),
            "pending": 0,
            "failed": len(errors),
        },
        provider_status=final_group,
        errors=errors,
    )
    return {
        "primitive": "deep_research_contacts",
        "command": "run",
        "status": status,
        "completed_at": now_iso(),
        "output_dir": str(params.output_dir),
        "counts": {
            "run_ids": run_count,
            "results_fetched": len(results),
            "errors": len(errors),
            "real_name_found": found_name,
            "linkedin_found": found_linkedin,
        },
        "group_status": final_group,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Deep-research contact dossiers via Parallel.ai")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("estimate", "run"):
        child = subparsers.add_parser(command)
        child.add_argument("--input", required=True)
        child.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, type=Path)
        child.add_argument("--processor", default=DEFAULT_PROCESSOR, choices=sorted(ALLOWED_PROCESSORS))
        child.add_argument("--limit", type=int)
        if command == "run":
            child.add_argument("--api-key")
            child.add_argument("--base-url", default=DEFAULT_BASE_URL)
            child.add_argument("--beta-header", default=DEFAULT_BETA_HEADER)
            child.add_argument("--manifest")
            child.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
            child.add_argument("--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL)
            child.add_argument("--max-wait", type=int, default=DEFAULT_MAX_WAIT)
            child.add_argument("--workers", type=int, default=DEFAULT_RESULT_WORKERS)
            child.add_argument("--api-timeout", type=int, default=60)
    args = parser.parse_args(argv)
    if args.command == "estimate":
        processor = _validate_processor(args.processor)
        rows = load_queue(Path(args.input))
        todo, reused = filter_already_done(rows, Path(args.output_dir))
        todo = todo[: args.limit] if args.limit is not None else todo
        per_task, wall_clock = PROCESSOR_LATENCY[processor]
        payload = {
            "primitive": "deep_research_contacts", "command": "estimate",
            "input": str(args.input), "output_dir": str(args.output_dir),
            "queue_rows": len(rows), "skipped_already_done": reused,
            "would_submit": len(todo), "processor": processor,
            "estimated_usd": round(len(todo) * PROCESSOR_PRICING_USD[processor], 4),
            "estimated_latency": {
                "processor": processor, "per_task": per_task,
                "rough_wall_clock": "no paid Parallel work" if not todo else wall_clock,
                "basis": "Parallel Task API processor docs; task-group runs are submitted together.",
            },
        }
    else:
        payload = run_research(ResearchRunParams(
            input_csv=Path(args.input), output_dir=Path(args.output_dir),
            processor=args.processor, manifest=str(args.manifest or ""),
            api_key=args.api_key, base_url=args.base_url, beta_header=args.beta_header,
            batch_size=args.batch_size, limit=args.limit, poll_interval=args.poll_interval,
            max_wait=args.max_wait, workers=args.workers, api_timeout=args.api_timeout,
        ))
    print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
    code = {"completed_with_errors": 2, "failed": 1}.get(str(payload.get("status")), 0)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
