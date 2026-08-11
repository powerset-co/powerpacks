"""One synchronous provider pass from filtered queue through durable outputs."""

from __future__ import annotations

import os
import sys
from dataclasses import asdict
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import write_json
from packs.ingestion.primitives.deep_context.shared.common import load_env
from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.deep_context.db.models import ArtifactProjection
from packs.ingestion.primitives.deep_context.manifests.enrichment_receipt import (
    EnrichmentReceipt,
)
from packs.ingestion.primitives.deep_context.enrich.parallel_research import (
    config,
    normalization,
    parallel_client,
    projection,
    queue,
)
from packs.ingestion.primitives.deep_context.enrich.parallel_research.models import (
    ResearchProgress,
    ResearchProgressCounts,
    ResearchRunCounts,
    ResearchRunResult,
    ParallelRunInput,
    ProviderStatusCounts,
    ResearchRunParams,
)
from packs.ingestion.primitives.deep_context.db.workflow_views import ReviewSelection


def _api_key(explicit: str | None) -> str:
    load_env()
    value = explicit or os.environ.get("PARALLEL_API_KEY")
    if not value:
        raise SystemExit("PARALLEL_API_KEY not set (pass --api-key or add it to the repo .env)")
    return value


def _progress_counts(
    total: int,
    reused: int,
    provider: ProviderStatusCounts,
) -> ResearchProgressCounts:
    """Merge reused-from-cache counts with the provider's cumulative poll counts."""
    completed = reused + provider.completed_total
    failed = provider.failed_total
    return ResearchProgressCounts(
        total,
        completed,
        max(0, total - completed - failed),
        failed,
    )


def report_progress(
    params: ResearchRunParams,
    status: str,
    counts: ResearchProgressCounts,
    *,
    projections: tuple[ArtifactProjection, ...] | None = None,
    selection: ReviewSelection | None = None,
    provider_status: dict[str, object] | None = None,
    error: str | None = None,
    errors: list[str] | None = None,
) -> None:
    """Project new outputs, then publish callback progress and the receipt.

    Two independent progress sinks, both driven off the same counts: the
    on-disk receipt (manifest.json, the FE-visible progress file — no separate
    progress store) and the in-process on_progress callback. `projections`
    reaching the DB here, not the files run_research already wrote, is what
    makes a row resume-visible; see queue.filter_already_done.
    """
    manifest_path = Path(params.manifest) if params.manifest else params.output_dir / "manifest.json"
    if projections is not None:
        params.db.project_rows(projections)
    if params.owns_receipt:
        try:
            receipt = EnrichmentReceipt(manifest_path)
        except ValueError as exc:
            raise SystemExit("--manifest must end in manifest.json") from exc
        payload: dict[str, object] = {
            "stage": "enrich",
            "status": status,
            "counts": asdict(counts),
        }
        if provider_status is not None:
            payload["provider_status"] = provider_status
        if selection is not None:
            payload["selection"] = asdict(selection)
        if error is not None:
            payload["error"] = error
        if errors is not None:
            payload["errors"] = errors
        receipt.write(payload)
    if params.on_progress:
        params.on_progress(ResearchProgress(status, counts))


def run_research(params: ResearchRunParams) -> ResearchRunResult:
    """Run one synchronous paid pass; fixed completed outputs make reruns free.

    Callers (research_reconcile.coordinator) gate --approve-spend and budget
    before ever constructing `params` — nothing below re-checks approval, so
    reaching this function means spend was already authorized.
    """
    processor = config.validate_processor(params.processor)
    rows = list(params.rows)
    existing = queries.artifacts(params.db)
    todo, reused = queue.filter_already_done(rows, existing)
    if params.limit is not None:
        # Slices the already-deduped/resume-filtered queue, so repeated
        # --limit N runs advance through new/undone work rather than
        # re-testing the same head of the raw row list.
        todo = todo[: params.limit]
    total = reused + len(todo)

    def failed(error: str) -> ResearchRunResult:
        report_progress(
            params,
            "failed",
            ResearchProgressCounts(total, reused, 0, len(todo)),
            error=error,
        )
        return ResearchRunResult.failed(error)

    if not todo:
        report_progress(
            params,
            "research_complete",
            ResearchProgressCounts(total, reused, 0, 0),
            provider_status={},
        )
        return ResearchRunResult(
            "no_work",
            queue_rows=len(rows),
            skipped_already_done=reused,
        )

    params.output_dir.mkdir(parents=True, exist_ok=True)
    inputs = [
        ParallelRunInput.from_payload(
            config.TASK_SPEC,
            queue.build_input(row, row.handle),
            row.handle,
            processor,
        )
        for row in todo
    ]
    api_key = _api_key(params.api_key)
    # Reports intent to submit len(todo) runs before execute() has actually
    # called add_runs() — if the client construction or the first batch fails
    # outright, the receipt already claimed a submission that never billed.
    report_progress(
        params,
        "running",
        ResearchProgressCounts(total, reused, len(todo), 0),
        provider_status={"submitted": len(todo)},
    )

    def on_status(provider: ProviderStatusCounts) -> None:
        report_progress(
            params,
            "running",
            _progress_counts(total, reused, provider),
            provider_status=provider.to_payload(),
        )
        print(
            f"[deep-research] poll status {provider.to_payload()}",
            file=sys.stderr,
            flush=True,
        )

    try:
        # The paid call: submits `inputs` to Parallel and polls to completion
        # or params.max_wait. See parallel_client.ParallelClient.execute for the
        # exact add_runs()/poll/get_runs sequence and their failure modes.
        execution = parallel_client.ParallelClient(
            api_key,
            params.base_url,
            params.beta_header,
        ).execute(inputs, params, on_status)
    except Exception as exc:
        # Any run_ids already billed inside a partially-completed execute()
        # call are lost here — this reports the whole pass "failed" with no
        # record of which handles were already submitted, so a retry
        # resubmits (and re-bills) all of `todo` again.
        return failed(f"{type(exc).__name__}: {exc}"[:300])
    if not execution.run_count:
        return failed("Parallel returned no run ids")

    rows_by_handle = {row.handle: row for row in todo}
    completed_rows: list[queue.ResearchQueueRow] = []
    found_name = found_linkedin = 0
    errors = list(execution.errors)
    # Paid results are already fully fetched by this point (execute() only
    # returns after get_runs() completes) — this loop is durable local file
    # I/O, not another round-trip to the provider.
    for handle, result in execution.results:
        row: queue.ResearchQueueRow | None = rows_by_handle.get(handle)
        if row is None:
            errors.append(f"{handle}: result did not match a submitted subject")
            continue
        person_dir = params.output_dir / handle
        person_dir.mkdir(parents=True, exist_ok=True)
        write_json(person_dir / "00_parallel_raw.json", result.to_payload())
        normalized = normalization.parallel_to_research_json(
            result,
            row,
            handle,
            row.display_name or handle,
            row.bio,
            research_method=f"parallel-{processor}",
        )
        write_json(person_dir / "01_research_parallel.json", normalized)
        completed_rows.append(row)
        found_name += int(bool(result.real_name))
        found_linkedin += int(bool(result.linkedin_url))

    status = "completed" if not errors else "completed_with_errors"
    # Every completed_rows file above is already on disk; this is the single
    # atomic DB commit (report_progress -> db.project_rows) that makes the
    # whole batch resume-visible to queue.filter_already_done. A crash between
    # the last write_json above and this call leaves paid, fully-written
    # results on disk that the next run still cannot see as done.
    projections = projection.research_artifact_projections(params, completed_rows)
    report_progress(
        params,
        "research_complete" if not errors else status,
        ResearchProgressCounts(total, reused + len(execution.results), 0, len(errors)),
        projections=projections,
        provider_status=execution.final_status.to_payload(),
        errors=errors,
    )
    return ResearchRunResult(
        status,
        output_dir=str(params.output_dir),
        counts=ResearchRunCounts(
            execution.run_count,
            len(execution.results),
            len(errors),
            found_name,
            found_linkedin,
        ),
        errors=tuple(errors),
    )
