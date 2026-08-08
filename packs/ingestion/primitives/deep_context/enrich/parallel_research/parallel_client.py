"""Thin official Parallel SDK task-group client."""

from __future__ import annotations

import time
from typing import Callable

from parallel import Parallel

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.enrich.parallel_research.models import (
    ParallelExecutionResult,
    ParallelProviderResult,
    ParallelRunInput,
    ProviderGroupStatus,
    ProviderStatusCounts,
    ResearchRunParams,
)


class ParallelClient:
    """Submit, wait for, and fetch one in-memory Parallel task group."""

    def __init__(self, api_key: str, base_url: str, beta_header: str) -> None:
        headers = {"parallel-beta": beta_header} if beta_header else None
        self._client = Parallel(
            api_key=api_key,
            base_url=base_url,
            default_headers=headers,
        )

    def execute(
        self,
        inputs: list[ParallelRunInput],
        params: ResearchRunParams,
        on_status: Callable[[ProviderStatusCounts], None],
    ) -> ParallelExecutionResult:
        # Group creation is free; nothing is billed until add_runs() below.
        group_id = str(
            self._client.task_group.create(
                metadata={"source": "powerpacks", "submitted_at": now_iso()}
            ).task_group_id
        )
        run_ids: list[str] = []
        for start in range(0, len(inputs), params.batch_size):
            # This is the paid call: every item in this batch is billed on
            # submission, independent of whether it ever completes. A raised
            # exception here (network error, bad request) still leaves any
            # run_ids already appended from prior batches billed but
            # unreturned — driver.run_research's except-block reports the
            # whole call failed and has no record of those already-submitted
            # runs to reconcile against on retry.
            response = self._client.task_group.add_runs(
                group_id,
                inputs=[
                    item.to_payload()
                    for item in inputs[start : start + params.batch_size]
                ],
            )
            run_ids.extend(str(value) for value in response.run_ids)
        if not run_ids:
            return ParallelExecutionResult(0, (), (), ProviderGroupStatus.empty())

        # Up to max_wait (2h default), every poll_interval (15s default). Hitting
        # the deadline while runs are still active does not raise — the loop just
        # exits and falls through to get_runs() below with whatever `final` was
        # last observed; a run still running at that point comes back as a
        # non-"completed" status and is reported as a per-handle error, not as a
        # timeout. Nothing here retries a run past this point.
        deadline = time.time() + params.max_wait
        final = ProviderGroupStatus.empty()
        while time.time() < deadline:
            raw_status = self._client.task_group.retrieve(group_id).status.model_dump()
            final = ProviderGroupStatus.from_payload(raw_status)
            on_status(final.task_counts)
            if final.is_active is False:
                break
            time.sleep(params.poll_interval)

        results: list[tuple[str, ParallelProviderResult]] = []
        errors: list[str] = []
        events = self._client.task_group.get_runs(
            group_id,
            include_input=True,
            include_output=True,
            timeout=params.api_timeout + 10,
        )
        with events:
            for event in events:
                run = getattr(event, "run", None)
                if run is None:
                    error = getattr(event, "error", "unknown stream error")
                    errors.append(f"task group: {error}"[:300])
                    continue
                run_id = str(run.run_id)
                metadata = dict(run.metadata or {})
                # Falls back to run_id when metadata is missing the handle we
                # submitted — that result then can't match any queue row in
                # driver.run_research (rows_by_handle lookup misses) and is
                # reported as an error even though the run itself succeeded.
                handle = str(metadata.get("handle") or run_id)
                if run.status != "completed":
                    # Still-running, queued, failed, and cancelled all land here
                    # as the same shape of error string — a timed-out-but-still-
                    # working run is indistinguishable from a provider failure.
                    errors.append(f"{run_id}: {run.status}: {run.error or 'no result'}"[:300])
                    continue
                content = getattr(getattr(event, "output", None), "content", None)
                if content is None:
                    errors.append(f"{run_id}: no payload")
                    continue
                # Parse boundary: a non-dict output is wrapped as {"raw": str(...)}
                # instead of raising, so a shape change on the provider side still
                # produces a ParallelProviderResult (all fields None but "raw") —
                # it surfaces later as low completeness, not a loud parse failure.
                payload = content if isinstance(content, dict) else {"raw": str(content)}
                results.append((handle, ParallelProviderResult.from_payload(payload)))
        return ParallelExecutionResult(len(run_ids), tuple(results), tuple(errors), final)
