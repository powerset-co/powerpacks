"""One synchronous provider pass from filtered queue through durable outputs."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso, write_json
from packs.ingestion.primitives.deep_context.common import load_env
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.enrichment_receipt import EnrichmentReceipt
from packs.ingestion.primitives.deep_context.parallel_research import (
    config,
    normalization,
    queue,
    sdk_client,
)


def _api_key(explicit: str | None) -> str:
    load_env()
    value = explicit or os.environ.get("PARALLEL_API_KEY")
    if not value:
        raise SystemExit(
            "PARALLEL_API_KEY not set (pass --api-key or add it to the repo .env)"
        )
    return value


def _progress_counts(
    total: int,
    reused: int,
    provider: dict[str, Any],
) -> dict[str, int]:
    completed = reused + sum(
        int(provider.get(key) or 0)
        for key in ("completed", "succeeded", "success")
    )
    failed = sum(
        int(provider.get(key) or 0)
        for key in ("failed", "error", "errored", "cancelled", "canceled")
    )
    completed = min(total, completed)
    failed = min(max(0, total - completed), failed)
    return {
        "total": total,
        "completed": completed,
        "pending": max(0, total - completed - failed),
        "failed": failed,
    }


def _manifest_relative(manifest_path: Path, artifact_path: Path) -> str:
    try:
        return artifact_path.resolve().relative_to(manifest_path.parent.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(
            f"research artifact must be inside the manifest directory: {artifact_path}"
        ) from exc


def research_artifact_inventory(params: Any) -> list[dict[str, Any]]:
    """Name and hash the exact fixed paid outputs for explicit projection."""
    if params.db is None:
        return []
    manifest_path = Path(params.manifest) if params.manifest else params.output_dir / "manifest.json"
    owners = {row.person_id: row.parent_id for row in canonical_snapshot(params.db).people}
    inventory: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in queue.load_queue(params.input_csv):
        handle = queue.candidate_handle(row)
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
            "kind": "research", "artifact_key": f"research:{handle}".lower(),
            "parent_id": next(iter(parent_ids)), "candidate_key": candidate_key,
            "public_identifier": candidate_key, "handle": handle,
            "person_ids": person_ids, "display_name": (row.get("display_name") or "").strip(),
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


def report_progress(
    params: Any, status: str, counts: dict[str, int], **extra: Any,
) -> None:
    """Publish callback progress and the standalone fixed receipt when owned."""
    if params.owns_receipt:
        manifest_path = Path(params.manifest) if params.manifest else params.output_dir / "manifest.json"
        try:
            receipt = EnrichmentReceipt(manifest_path, params.db)
        except ValueError as exc:
            raise SystemExit("--manifest must end in manifest.json") from exc
        receipt.update({
            **extra, "stage": "enrich", "status": status, "counts": counts,
            "artifacts": research_artifact_inventory(params),
        })
    if params.on_progress:
        params.on_progress({"status": status, "counts": counts})


def run_research(params: Any) -> dict[str, Any]:
    """Run one synchronous paid pass; fixed completed outputs make reruns free."""
    processor = config.validate_processor(params.processor)
    rows = queue.load_queue(params.input_csv)
    todo, reused = queue.filter_already_done(rows, params.output_dir)
    if params.limit is not None:
        todo = todo[: params.limit]
    total = reused + len(todo)
    if not todo:
        report_progress(
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
        "task_spec": config.TASK_SPEC,
        "input": queue.build_input(row, row["handle"]),
        "metadata": {"handle": row["handle"]},
        "processor": processor,
    } for row in todo]
    api_key = _api_key(params.api_key)
    report_progress(
        params,
        "running",
        {"total": total, "completed": reused, "pending": len(todo), "failed": 0},
        provider_status={"submitted": len(todo)},
    )

    def on_status(provider: dict[str, Any]) -> None:
        report_progress(
            params,
            "running",
            _progress_counts(total, reused, provider),
            provider_status=provider,
        )
        print(
            f"[deep_research_contacts] poll status {provider}",
            file=sys.stderr,
            flush=True,
        )

    try:
        run_count, results, errors, final_group = sdk_client.ParallelClient(
            api_key,
            params.base_url,
            params.beta_header,
        ).execute(inputs, params, on_status)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:300]
        report_progress(
            params,
            "failed",
            {"total": total, "completed": reused, "pending": 0, "failed": len(todo)},
            error=error,
        )
        return {
            "primitive": "deep_research_contacts",
            "command": "run",
            "status": "failed",
            "error": error,
        }
    if not run_count:
        error = "Parallel returned no run ids"
        report_progress(
            params,
            "failed",
            {"total": total, "completed": reused, "pending": 0, "failed": len(todo)},
            error=error,
        )
        return {
            "primitive": "deep_research_contacts",
            "command": "run",
            "status": "failed",
            "error": error,
        }

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
        normalized = normalization.parallel_to_research_json(
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
    report_progress(
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
