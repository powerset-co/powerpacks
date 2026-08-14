"""Thin official Parallel SDK task-group client."""

from __future__ import annotations

import time
from typing import Callable

from parallel import Parallel
from parallel.types import RunInputParam, TaskGroupStatus, TaskRunEvent, TaskRunJsonOutput

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.enrich.parallel_research import config
from packs.ingestion.primitives.deep_context.enrich.parallel_research.models import (
    ParallelExecutionResult,
    ResearchRunParams,
)


class ParallelClient:
    """Submit, wait for, and stream one in-memory Parallel task group."""

    def __init__(self, api_key: str, base_url: str, beta_header: str) -> None:
        headers = {"parallel-beta": beta_header} if beta_header else None
        # Paid POSTs have no provider idempotency key. Retrying an ambiguous
        # timeout could submit the same paid work twice, so SDK retries are off.
        self._client = Parallel(
            api_key=api_key,
            base_url=base_url,
            default_headers=headers,
            max_retries=0,
        )

    def execute(
        self,
        inputs: list[RunInputParam],
        params: ResearchRunParams,
        on_status: Callable[[TaskGroupStatus], None],
        on_result: Callable[[str, TaskRunJsonOutput], None],
    ) -> ParallelExecutionResult:
        group_id = str(
            self._client.task_group.create(
                metadata={"source": "powerpacks", "submitted_at": now_iso()}
            ).task_group_id
        )
        run_ids: list[str] = []
        errors: list[str] = []
        for start in range(0, len(inputs), params.batch_size):
            try:
                response = self._client.task_group.add_runs(
                    group_id,
                    inputs=inputs[start : start + params.batch_size],
                    default_task_spec=config.TASK_SPEC,
                )
            except Exception as exc:
                # The server may have accepted an HTTP request whose response
                # was lost. Without provider idempotency, automatic retry would
                # risk double billing; surface that ambiguity and reconcile any
                # earlier, acknowledged batches below.
                errors.append(f"submission_unknown: {type(exc).__name__}: {exc}"[:300])
                break
            run_ids.extend(str(value) for value in response.run_ids)
        if not run_ids:
            return ParallelExecutionResult(0, 0, tuple(errors), None)

        deadline = time.time() + params.max_wait
        final: TaskGroupStatus | None = None
        while time.time() < deadline:
            try:
                final = self._client.task_group.retrieve(group_id).status
            except Exception as exc:
                errors.append(f"poll: {type(exc).__name__}: {exc}"[:300])
                break
            on_status(final)
            if not final.is_active:
                break
            time.sleep(params.poll_interval)
        if final is not None and final.is_active:
            errors.append("task group: timeout while provider runs remain active")

        result_count = 0
        try:
            events = self._client.task_group.get_runs(
                group_id,
                include_input=True,
                include_output=True,
                timeout=params.api_timeout + 10,
            )
            with events:
                for event in events:
                    if not isinstance(event, TaskRunEvent):
                        errors.append(f"task group: {getattr(event, 'error', 'unknown stream error')}"[:300])
                        continue
                    run = event.run
                    handle = str((run.metadata or {}).get("handle") or run.run_id)
                    if run.status != "completed":
                        errors.append(f"{run.run_id}: {run.status}: {run.error or 'no result'}"[:300])
                        continue
                    if not isinstance(event.output, TaskRunJsonOutput):
                        errors.append(f"{run.run_id}: completed without JSON output")
                        continue
                    on_result(handle, event.output)
                    result_count += 1
        except Exception as exc:
            # Results already handed to on_result are durable; only the
            # unobserved tail remains incomplete.
            errors.append(f"result_stream: {type(exc).__name__}: {exc}"[:300])
        return ParallelExecutionResult(len(run_ids), result_count, tuple(errors), final)
