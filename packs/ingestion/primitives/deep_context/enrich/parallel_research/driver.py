"""One synchronous provider pass from prepared queue through durable outputs."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict

from parallel.types import RunInputParam, TaskGroupStatus, TaskRunJsonOutput

from packs.ingestion.primitives.common.jsonio import write_json
from packs.ingestion.primitives.deep_context.shared.common import load_env
from packs.ingestion.primitives.deep_context.db.models import ArtifactProjection
from packs.ingestion.primitives.deep_context.manifests.enrichment_receipt import EnrichmentReceipt
from packs.ingestion.primitives.deep_context.manifests.receipt_counts import ReceiptCounts
from packs.ingestion.primitives.deep_context.manifests.receipt_status import ReceiptStatus
from packs.ingestion.primitives.deep_context.enrich.parallel_research import config, parallel_client, projection, queue
from packs.ingestion.primitives.deep_context.enrich.parallel_research.models import (
    ResearchProgress,
    ResearchRunCounts,
    ResearchRunParams,
    ResearchRunResult,
)
from packs.ingestion.primitives.deep_context.enrich.parallel_research.result import ResearchResult


def _api_key(explicit: str | None) -> str:
    load_env()
    value = explicit or os.environ.get("PARALLEL_API_KEY")
    if not value:
        raise SystemExit("PARALLEL_API_KEY not set (pass --api-key or add it to the repo .env)")
    return value


def _progress_counts(total: int, status: TaskGroupStatus) -> ReceiptCounts:
    counts = status.task_run_status_counts
    completed = int(counts.get("completed", 0))
    failed = int(counts.get("failed", 0)) + int(counts.get("cancelled", 0))
    return ReceiptCounts(total, completed, max(0, total - completed - failed), failed)


def report_progress(
    params: ResearchRunParams,
    status: str,
    counts: ReceiptCounts,
    *,
    projections: tuple[ArtifactProjection, ...] | None = None,
    provider_status: dict[str, object] | None = None,
    error: str | None = None,
    errors: list[str] | None = None,
) -> None:
    manifest_path = params.manifest if params.manifest is not None else params.output_dir / "manifest.json"
    if projections:
        params.db.project_rows(projections)
    if params.owns_receipt:
        payload: dict[str, object] = {
            "stage": "enrich",
            "status": status,
            "counts": asdict(counts),
        }
        if provider_status is not None:
            payload["provider_status"] = provider_status
        if error is not None:
            payload["error"] = error
        if errors is not None:
            payload["errors"] = errors
        EnrichmentReceipt(manifest_path).write(payload)
    if params.on_progress:
        params.on_progress(ResearchProgress(status, counts))


def run_research(params: ResearchRunParams) -> ResearchRunResult:
    """Run one paid pass over rows already selected as net-new."""
    processor = config.validate_processor(params.processor)
    rows = list(params.rows)
    total = len(rows)

    def failed(error: str) -> ResearchRunResult:
        report_progress(params, ReceiptStatus.FAILED, ReceiptCounts(total, 0, 0, total), error=error)
        return ResearchRunResult.failed(error)

    if not rows:
        report_progress(params, ReceiptStatus.RESEARCH_COMPLETE, ReceiptCounts(0, 0, 0, 0), provider_status={})
        return ResearchRunResult("no_work")

    params.output_dir.mkdir(parents=True, exist_ok=True)
    inputs: list[RunInputParam] = [
        {
            "input": queue.build_input(row, row.handle),
            "metadata": {"handle": row.handle},
            "processor": processor,
        }
        for row in rows
    ]
    report_progress(
        params,
        ReceiptStatus.RUNNING,
        ReceiptCounts(total, 0, total, 0),
        provider_status={"submitting": total},
    )
    rows_by_handle = {row.handle: row for row in rows}
    completed: set[str] = set()
    local_errors: list[str] = []
    found_name = found_linkedin = 0

    def on_status(status: TaskGroupStatus) -> None:
        payload = status.model_dump(mode="json", exclude_none=True)
        report_progress(params, ReceiptStatus.RUNNING, _progress_counts(total, status), provider_status=payload)
        print(f"[deep-research] poll status {status.task_run_status_counts}", file=sys.stderr, flush=True)

    def on_result(handle: str, output: TaskRunJsonOutput) -> None:
        nonlocal found_name, found_linkedin
        row = rows_by_handle.get(handle)
        if row is None:
            local_errors.append(f"{handle}: result did not match a submitted subject")
            return
        try:
            result = ResearchResult.from_output(output)
            person_dir = params.output_dir / handle
            person_dir.mkdir(parents=True, exist_ok=True)
            result_path = person_dir / "00_parallel_result.json"
            payload = result.to_payload()
            result_data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
            write_json(result_path, payload)
            projected = projection.research_artifact_projection(params, row, result, result_path, result_data)
            # One accepted provider output and its canonical SQLite row become
            # durable together before the stream advances to the next result.
            report_progress(
                params,
                ReceiptStatus.RUNNING,
                ReceiptCounts(total, len(completed) + 1, max(0, total - len(completed) - 1), 0),
                projections=(projected,),
            )
        except Exception as exc:
            local_errors.append(f"{handle}: {type(exc).__name__}: {exc}"[:300])
            return
        completed.add(handle)
        found_name += int(bool(result.person.full_name))
        found_linkedin += int(bool(result.linkedin_url))

    try:
        execution = parallel_client.ParallelClient(api_key=_api_key(params.api_key), base_url=params.base_url, beta_header=params.beta_header).execute(
            inputs, params, on_status, on_result
        )
    except Exception as exc:
        return failed(f"{type(exc).__name__}: {exc}"[:300])
    errors = [*execution.errors, *local_errors]
    if not execution.run_count:
        return failed(errors[0] if errors else "Parallel returned no run ids")
    status = "completed" if not errors and len(completed) == total else "completed_with_errors"
    provider_payload = execution.final_status.model_dump(mode="json", exclude_none=True) if execution.final_status else {}
    final_counts = ReceiptCounts.create(
        total=total,
        completed=len(completed),
        failed=len(errors),
    )
    report_progress(
        params,
        ReceiptStatus.RESEARCH_COMPLETE if status == "completed" else status,
        final_counts,
        provider_status=provider_payload,
        errors=errors,
    )
    return ResearchRunResult(
        status,
        output_dir=str(params.output_dir),
        counts=ResearchRunCounts(execution.run_count, len(completed), len(errors), found_name, found_linkedin),
        errors=tuple(errors),
    )
