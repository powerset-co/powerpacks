"""Submit prepared rows and checkpoint each typed provider result."""

from __future__ import annotations

import json
import os
import sys

from parallel.types import RunInputParam, TaskGroupStatus, TaskRunJsonOutput

from packs.ingestion.primitives.common.jsonio import write_json
from packs.ingestion.primitives.deep_context.shared.common import load_env
from packs.ingestion.primitives.deep_context.manifests.receipt_counts import ReceiptCounts
from packs.ingestion.primitives.deep_context.enrich.parallel_research import config, parallel_client, projection, queue
from packs.ingestion.primitives.deep_context.enrich.parallel_research.models import (
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


def run_research(params: ResearchRunParams) -> ResearchRunResult:
    """Run one paid pass over rows already selected as net-new."""
    processor = config.validate_processor(params.processor)
    rows = list(params.rows)
    total = len(rows)

    if not rows:
        return ResearchRunResult(0)

    params.output_dir.mkdir(parents=True, exist_ok=True)
    inputs: list[RunInputParam] = [
        {
            "input": queue.build_input(row, row.handle),
            "metadata": {"handle": row.handle},
            "processor": processor,
        }
        for row in rows
    ]
    if params.on_progress:
        params.on_progress(ReceiptCounts(total, 0, total, 0))
    rows_by_handle = {row.handle: row for row in rows}
    completed: set[str] = set()
    local_errors: list[str] = []

    def on_status(status: TaskGroupStatus) -> None:
        if params.on_progress:
            params.on_progress(_progress_counts(total, status))
        print(f"[deep-research] poll status {status.task_run_status_counts}", file=sys.stderr, flush=True)

    def on_result(handle: str, output: TaskRunJsonOutput) -> None:
        row = rows_by_handle.get(handle)
        if row is None:
            local_errors.append(f"{handle}: result did not match a submitted subject")
            return
        try:
            result = ResearchResult.from_output(output)
            person_dir = params.output_dir / handle
            person_dir.mkdir(parents=True, exist_ok=True)
            result_path = person_dir / "00_parallel_result.json"
            payload = output.model_dump(mode="json", exclude_none=True)
            result_data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
            projected = projection.research_artifact_projection(params, row, result, result_path, result_data)
            # SQLite is the record and contains the full provider envelope.
            # Commit it before rendering the fixed-path file so a crash between
            # the two cannot make paid work invisible to exact-input reuse.
            params.db.project_rows((projected,))
            completed.add(handle)
            write_json(result_path, payload)
            if params.on_progress:
                params.on_progress(
                    ReceiptCounts(total, len(completed), max(0, total - len(completed)), 0)
                )
        except Exception as exc:
            local_errors.append(f"{handle}: {type(exc).__name__}: {exc}"[:300])
            return

    try:
        provider_errors = parallel_client.ParallelClient(api_key=_api_key(params.api_key), base_url=params.base_url, beta_header=params.beta_header).execute(
            inputs, params, on_status, on_result
        )
    except Exception as exc:
        return ResearchRunResult.failed(total, f"{type(exc).__name__}: {exc}"[:300])
    errors = [*provider_errors, *local_errors]
    if not completed:
        error = errors[0] if errors else "Parallel returned no completed results"
        return ResearchRunResult(total, errors=tuple(errors or [error]))
    missing = total - len(completed)
    if missing and not errors:
        errors.append(f"Parallel returned no completed result for {missing} submitted subject(s)")
    return ResearchRunResult(
        total,
        completed=len(completed),
        errors=tuple(errors),
    )
