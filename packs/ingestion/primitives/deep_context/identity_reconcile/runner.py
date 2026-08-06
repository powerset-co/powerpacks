"""Paid/free attached-identity stage runner over typed queue and result policy."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, TypeVar

from packs.indexing.lib.openai_responses import estimate_cost_usd, reasoning_effort
from packs.indexing.lib.openai_usage_tiers import env_or_profile_int
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.dossier_evidence import owner_background
from packs.ingestion.primitives.deep_context import identity_evidence
from packs.ingestion.primitives.deep_context.identity_reconcile import judgment_policy
from packs.ingestion.primitives.deep_context.identity_reconcile.queue import (
    CONNECTION_VERDICT,
    fetch_missing_profiles,
    select_tasks,
)
from packs.ingestion.primitives.deep_context.identity_reconcile.results import (
    load_tasks_from_snapshot,
    merge_subset_tasks,
    write_overrides,
    write_verdicts,
)
from packs.ingestion.primitives.pipeline.contract import StageManifest

ManifestT = TypeVar("ManifestT", bound=StageManifest)


def _judge_tasks(
    db: Db,
    tasks: list[dict[str, Any]], *, model: str, requested_effort: str,
    requested_concurrency: int, timeout: int, max_retries: int,
) -> dict[str, int]:
    usage = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
    concurrency = requested_concurrency or env_or_profile_int(
        "POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency", fallback=64,
    )
    effort = reasoning_effort(requested_effort)
    owner_block = owner_background(canonical_snapshot(db))

    results = identity_evidence.judge_batch(
        tasks, use_llm=True, owner_block=owner_block, model=model, effort=effort,
        concurrency=concurrency, timeout=timeout, max_retries=max_retries,
    )
    for task, result in zip(tasks, results):
        task["verdict"] = result.get("verdict") or {}
        task["error"] = result.get("error") or ""
        task["judgment_fingerprint"] = str(
            result.get("fingerprint")
            or identity_evidence.task_fingerprint(task, owner_block)
        )
        for key in usage:
            usage[key] += int((result.get("usage") or {}).get(key) or 0)
    return usage


def run_stage(
    manifest_type: type[ManifestT], *, db: Db, profile_cache_dir: Path,
    verdicts_jsonl: Path, confirm_threshold: float, detach_threshold: float,
    model: str, requested_effort: str, concurrency: int, timeout: int,
    max_retries: int, slugs: list[str], limit: int, no_overrides: bool,
    no_llm: bool, reapply: bool,
) -> ManifestT:
    started = time.monotonic()
    use_llm = not no_llm and not reapply
    owner_block = owner_background(canonical_snapshot(db))
    fetch_counts: dict[str, int] = {}
    usage = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
    if reapply:
        tasks = load_tasks_from_snapshot(db)
    else:
        tasks = select_tasks(db, slugs, limit)
        for task in tasks:
            if task.get("from_connections"):
                task["verdict"], task["error"] = dict(CONNECTION_VERDICT), ""
        if use_llm:
            fetch_counts = fetch_missing_profiles(db, tasks, profile_cache_dir)
        judgeable = [
            task for task in tasks
            if not task.get("from_connections") and task["linkedin"].get("has_profile")
        ]
        if use_llm and judgeable:
            usage = _judge_tasks(
                db,
                judgeable, model=model, requested_effort=requested_effort,
                requested_concurrency=concurrency, timeout=timeout,
                max_retries=max_retries,
            )
        deterministic = [task for task in tasks if "verdict" not in task]
        if deterministic:
            results = identity_evidence.judge_batch(
                deterministic,
                use_llm=False,
                owner_block=owner_block,
                model=model,
                effort=reasoning_effort(requested_effort),
                concurrency=1,
                timeout=timeout,
                max_retries=max_retries,
            )
            for task, result in zip(deterministic, results):
                task["verdict"] = result.get("verdict") or {}
                task["error"] = result.get("error") or ""
                task["judgment_fingerprint"] = str(
                    result.get("fingerprint")
                    or identity_evidence.task_fingerprint(task, owner_block)
                )
        for task in tasks:
            if not task.get("judgment_fingerprint"):
                task["judgment_fingerprint"] = identity_evidence.task_fingerprint(
                    task, owner_block
                )
        if slugs or limit:
            tasks = merge_subset_tasks(db, tasks)

    judgment_policy.decide_actions(tasks, confirm_threshold, detach_threshold)
    write_verdicts(verdicts_jsonl, tasks)
    overrides = write_overrides(
        db, [] if no_overrides else tasks,
        artifact_path=None if no_overrides else verdicts_jsonl,
    )
    counts = {value: 0 for value in judgment_policy.VERDICTS}
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
