"""Thin official Parallel SDK task-group event-stream client."""

from __future__ import annotations

from typing import Callable

from parallel import Parallel
from parallel.types import (
    ErrorEvent,
    RunInputParam,
    TaskGroupStatus,
    TaskGroupStatusEvent,
    TaskRunEvent,
    TaskRunJsonOutput,
)

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.enrich.parallel_research import config
from packs.ingestion.primitives.deep_context.enrich.parallel_research.models import ResearchRunParams


class ParallelClient:
    """Submit and consume one typed Parallel task-group event stream."""

    def __init__(self, api_key: str, base_url: str, beta_header: str) -> None:
        headers = {"parallel-beta": beta_header} if beta_header else None
        # A failed stage is rerun from its projected checkpoints. Do not hide a
        # second paid submission inside the SDK client.
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
    ) -> tuple[str, ...]:
        group_id = str(
            self._client.task_group.create(
                metadata={"source": "powerpacks", "submitted_at": now_iso()}
            ).task_group_id
        )
        errors: list[str] = []
        finished_runs: set[str] = set()
        for start in range(0, len(inputs), params.batch_size):
            self._client.task_group.add_runs(
                group_id,
                inputs=inputs[start : start + params.batch_size],
                default_task_spec=config.TASK_SPEC,
            )

        def accept_run(event: object) -> None:
            if isinstance(event, ErrorEvent):
                errors.append(f"task group: {event.error.message}"[:300])
                return
            if not isinstance(event, TaskRunEvent):
                errors.append("task group: unknown run event")
                return
            run = event.run
            if run.is_active or run.run_id in finished_runs:
                return
            finished_runs.add(run.run_id)
            handle = str((run.metadata or {}).get("handle") or run.run_id)
            if run.status != "completed":
                errors.append(f"{run.run_id}: {run.status}: {run.error or 'no result'}"[:300])
            else:
                output = event.output
                if output is None:
                    try:
                        output = self._client.task_run.result(
                            run.run_id,
                            timeout=params.stream_timeout + 30,
                        ).output
                    except Exception as exc:
                        errors.append(
                            f"{run.run_id}: result: {type(exc).__name__}: {exc}"[:300]
                        )
                        return
                if isinstance(output, TaskRunJsonOutput):
                    on_result(handle, output)
                else:
                    errors.append(f"{run.run_id}: completed without JSON output")

        try:
            events = self._client.task_group.events(
                group_id,
                api_timeout=params.stream_timeout,
                timeout=params.stream_timeout + 30,
            )
            with events:
                for event in events:
                    if isinstance(event, TaskGroupStatusEvent):
                        on_status(event.status)
                        if not event.status.is_active:
                            break
                    else:
                        accept_run(event)
        except Exception as exc:
            errors.append(f"status_stream: {type(exc).__name__}: {exc}"[:300])

        # In real task-group streams Parallel emits progress/status events but
        # may omit the completed run envelopes. Fetch the final SDK run stream
        # once after status becomes terminal; already-seen run IDs dedupe it.
        try:
            runs = self._client.task_group.get_runs(
                group_id,
                include_output=True,
                timeout=params.stream_timeout + 30,
            )
            with runs:
                for event in runs:
                    accept_run(event)
        except Exception as exc:
            errors.append(f"result_stream: {type(exc).__name__}: {exc}"[:300])
        return tuple(errors)
