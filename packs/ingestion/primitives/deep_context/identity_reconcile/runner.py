"""Paid/free attached-identity stage runner over typed queue and result policy."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, TypeVar

from packs.indexing.lib.openai_responses import estimate_cost_usd, make_async_client, reasoning_effort
from packs.indexing.lib.openai_stream import drain_pool
from packs.indexing.lib.openai_usage_tiers import env_or_profile_int
from packs.ingestion.primitives.deep_context.common import (
    load_env,
    load_owner,
    owner_background_block,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.identity_evidence import (
    VERDICTS,
    decide_actions,
    deterministic_verdict,
    judge_task,
)
from packs.ingestion.primitives.deep_context.identity_reconcile.queue import (
    CONNECTION_VERDICT,
    fetch_missing_profiles,
    select_tasks,
)
from packs.ingestion.primitives.deep_context.identity_reconcile.results import (
    empty_overrides,
    load_tasks_from_verdicts,
    merge_subset_tasks,
    write_overrides,
    write_verdicts,
)
from packs.ingestion.primitives.pipeline.contract import StageManifest

ManifestT = TypeVar("ManifestT", bound=StageManifest)


def _judge_tasks(
    tasks: list[dict[str, Any]], *, model: str, requested_effort: str,
    requested_concurrency: int, timeout: int, max_retries: int,
) -> dict[str, int]:
    usage = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
    load_env()
    concurrency = requested_concurrency or env_or_profile_int(
        "POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency", fallback=64,
    )
    effort = reasoning_effort(requested_effort)
    owner = load_owner()
    owner_block = owner_background_block(owner) if owner else ""

    async def run() -> None:
        client = make_async_client(timeout=timeout)
        results: dict[int, dict[str, Any]] = {}
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def one(index: int, task: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            return index, await judge_task(
                client, task, owner_block, model=model, effort=effort,
                semaphore=semaphore, max_retries=max_retries,
            )

        try:
            await drain_pool(
                [one(index, task) for index, task in enumerate(tasks)],
                lambda result: results.__setitem__(result[0], result[1]),
            )
        finally:
            await client.close()
        for index, task in enumerate(tasks):
            result = results.get(index, {"verdict": {}, "usage": {}, "error": "no result"})
            task["verdict"] = result.get("verdict") or {}
            task["error"] = result.get("error") or ""
            for key in usage:
                usage[key] += int((result.get("usage") or {}).get(key) or 0)

    asyncio.run(run())
    return usage


def run_stage(
    manifest_type: type[ManifestT], *, db: Db, facts_dir: Path, raw_dir: Path,
    profile_cache_dir: Path,
    verdicts_jsonl: Path, confirm_threshold: float, detach_threshold: float,
    model: str, requested_effort: str, concurrency: int, timeout: int,
    max_retries: int, slugs: list[str], limit: int, no_overrides: bool,
    no_llm: bool, reapply: bool,
) -> ManifestT:
    started = time.monotonic()
    use_llm = not no_llm and not reapply
    fetch_counts: dict[str, int] = {}
    usage = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
    if reapply:
        tasks = load_tasks_from_verdicts(verdicts_jsonl)
    else:
        tasks = select_tasks(db, facts_dir, raw_dir, profile_cache_dir, slugs, limit)
        for task in tasks:
            if task.get("from_connections"):
                task["verdict"], task["error"] = dict(CONNECTION_VERDICT), ""
        if use_llm:
            fetch_counts = fetch_missing_profiles(tasks, profile_cache_dir)
        judgeable = [
            task for task in tasks
            if not task.get("from_connections") and task["linkedin"].get("has_profile")
        ]
        if use_llm and judgeable:
            usage = _judge_tasks(
                judgeable, model=model, requested_effort=requested_effort,
                requested_concurrency=concurrency, timeout=timeout,
                max_retries=max_retries,
            )
        for task in tasks:
            if "verdict" not in task:
                task["verdict"], task["error"] = deterministic_verdict(task), ""
        if slugs or limit:
            tasks = merge_subset_tasks(verdicts_jsonl, tasks)

    decide_actions(tasks, confirm_threshold, detach_threshold)
    write_verdicts(verdicts_jsonl, tasks)
    overrides = empty_overrides(db) if no_overrides else write_overrides(
        db, tasks, artifact_path=verdicts_jsonl,
    )
    counts = {value: 0 for value in VERDICTS}
    for task in tasks:
        value = str((task.get("verdict") or {}).get("verdict") or "")
        if value in counts:
            counts[value] += 1
    conflicts = [task for task in tasks if task.get("conflict")]
    research = [
        task for task in tasks
        if (task.get("verdict") or {}).get("verdict") == "wrong_person"
        and float((task.get("verdict") or {}).get("confidence") or 0) >= detach_threshold
        and (task.get("verdict") or {}).get("recommend_deep_research")
        and not (task.get("verdict") or {}).get("linkedin_plausibly_absent")
    ]
    billed_output = usage["output_tokens"] + usage["reasoning_tokens"]
    return manifest_type(
        status="completed",
        judge="llm" if use_llm else "deterministic",
        parents=len({task.get("parent_id") or task.get("parent_slug") for task in tasks}),
        tasks=len(tasks),
        judged=sum(
            not task.get("from_connections") and task["linkedin"].get("has_profile")
            for task in tasks
        ),
        ground_truth_connections=sum(bool(task.get("from_connections")) for task in tasks),
        verdicts=counts,
        conflicts=len(conflicts),
        conflicts_auto_resolved=sum(task.get("via") == "conflict_resolved" for task in conflicts),
        conflicts_to_review=sum(task.get("action") == "review" for task in conflicts),
        profile_fetch=fetch_counts or None,
        errors=sum(bool(task.get("error")) for task in tasks),
        overrides=overrides,
        consolidation={"consolidated_parents": 0},
        needs_review=int(overrides.get("pending", 0)),
        deep_research_eligible=len(research),
        deep_research_est_usd=round(len(research) * 0.05, 2),
        tokens=usage,
        estimated_cost_usd=estimate_cost_usd(usage["input_tokens"], billed_output, model),
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
