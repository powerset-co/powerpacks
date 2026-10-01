"""Thin official Parallel SDK task-group event-stream client."""

from __future__ import annotations

import json
import time
from typing import Callable

import httpx

from parallel import APIConnectionError, Parallel
from parallel.types import (
    ErrorEvent,
    RunInputParam,
    TaskGroupStatus,
    TaskGroupStatusEvent,
    TaskRunEvent,
    TaskRunJsonOutput,
)

from packs.ingestion.primitives.common.jsonio import now_iso, write_json
from packs.ingestion.primitives.deep_context.enrich.parallel_research import config
from packs.ingestion.primitives.deep_context.enrich.parallel_research.models import ResearchRunParams


class ParallelClient:
    """Submit and consume one typed Parallel task-group event stream."""

    def __init__(self, api_key: str, base_url: str, beta_header: str) -> None:
        self._beta_header = beta_header
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
        manifest_path = params.output_dir / "manifest.json"
        requested = {str(item["metadata"]["handle"]): item for item in inputs}
        receipt = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        contract = {"task_spec": config.TASK_SPEC, "beta_header": self._beta_header}
        previous = receipt.get("inputs", {})
        resume = (
            receipt.get("provider_contract") == contract
            and bool(previous)
            and all(previous.get(key) == value for key, value in requested.items())
        )
        group_id = receipt["task_group_id"] if resume else str(
            self._client.task_group.create(
                metadata={"source": "powerpacks", "submitted_at": now_iso()}
            ).task_group_id
        )
        if not resume:
            write_json(manifest_path, {
                "source": "powerpacks", "submitted_at": now_iso(),
                "task_group_id": group_id, "inputs": requested, "provider_contract": contract,
            })
        errors: list[str] = []
        finished_runs: set[str] = set()

        def accept_run(event: object) -> None:
            if isinstance(event, ErrorEvent):
                errors.append(f"task group: {event.error.message}"[:300])
                return
            if not isinstance(event, TaskRunEvent):
                return
            run = event.run
            if run.is_active or run.run_id in finished_runs:
                return
            handle = str((run.metadata or {}).get("handle") or run.run_id)
            if handle not in requested:
                return
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
                    except (httpx.TransportError, APIConnectionError):
                        raise
                    except Exception as exc:
                        errors.append(
                            f"{run.run_id}: result: {type(exc).__name__}: {exc}"[:300]
                        )
                        return
                if isinstance(output, TaskRunJsonOutput):
                    on_result(handle, output)
                else:
                    errors.append(f"{run.run_id}: completed without JSON output")
            finished_runs.add(run.run_id)

        # Retry reads only. Recover an ambiguous add_runs POST by listing the
        # same group's handles on the next invocation.
        def read_runs() -> set[str]:
            handles: set[str] = set()
            cursor = None
            while True:
                try:
                    options = {"last_event_id": cursor} if cursor else {}
                    with self._client.task_group.get_runs(
                        group_id, include_output=True,
                        timeout=params.stream_timeout + 30, **options,
                    ) as runs:
                        for event in runs:
                            accept_run(event)
                            if isinstance(event, TaskRunEvent):
                                handles.add(str((event.run.metadata or {}).get("handle") or event.run.run_id))
                                cursor = event.event_id
                    return handles
                except (httpx.TransportError, APIConnectionError):
                    time.sleep(1)

        existing = read_runs() if resume else set()
        missing = [item for handle, item in requested.items() if handle not in existing]
        for start in range(0, len(missing), params.batch_size):
            self._client.task_group.add_runs(
                group_id, inputs=missing[start : start + params.batch_size],
                default_task_spec=config.TASK_SPEC,
            )

        cursor = None
        while True:
            terminal = False
            try:
                options = {"last_event_id": cursor} if cursor else {}
                with self._client.task_group.events(
                    group_id, api_timeout=params.stream_timeout,
                    timeout=params.stream_timeout + 30, **options,
                ) as events:
                    for event in events:
                        if isinstance(event, TaskGroupStatusEvent):
                            on_status(event.status)
                            terminal = not event.status.is_active
                        else:
                            accept_run(event)
                        if isinstance(event, (TaskRunEvent, TaskGroupStatusEvent)):
                            cursor = event.event_id
                        if terminal:
                            break
            except (httpx.TransportError, APIConnectionError):
                pass
            if terminal and not (resume and missing):
                break
            try:
                status = self._client.task_group.retrieve(group_id).status
            except (httpx.TransportError, APIConnectionError):
                time.sleep(1)
                continue
            on_status(status)
            if not status.is_active:
                break
            time.sleep(1)
        read_runs()
        return tuple(errors)
