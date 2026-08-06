"""Thin official Parallel SDK task-group client."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from parallel import NotFoundError, Parallel

from packs.ingestion.primitives.common.jsonio import now_iso


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
        inputs: list[dict[str, Any]],
        params: Any,
        on_status: Callable[[dict[str, Any]], None],
    ) -> tuple[int, dict[str, dict[str, Any]], list[str], dict[str, Any]]:
        group_id = str(
            self._client.task_group.create(
                metadata={"source": "powerpacks", "submitted_at": now_iso()}
            ).task_group_id
        )
        run_ids: list[str] = []
        for start in range(0, len(inputs), params.batch_size):
            response = self._client.task_group.add_runs(
                group_id,
                inputs=inputs[start : start + params.batch_size],
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
                    run_id,
                    api_timeout=params.api_timeout,
                    timeout=params.api_timeout + 10,
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
