"""Paid/free attached-identity stage runner over typed queue and result policy."""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import TypeVar

from packs.ingestion.primitives.deep_context.db.identity_views import human_settled_identities
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import owner_background
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile import judge
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile import judgment_policy
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.queue import (
    CONNECTION_VERDICT,
    build_tasks,
    fetch_missing_profiles,
    judgeable_tasks,
    split_reuse,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.models import (
    ProfileFetchCounts,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import (
    IdentityTask,
    IdentityUsage,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.results import (
    load_tasks_from_store,
    settle,
    write_verdicts,
)
from packs.ingestion.primitives.deep_context.shared.openai_responses import (
    OpenAIResponsesConfig,
    estimate_cost_usd,
)
from packs.ingestion.primitives.pipeline.contract import StageManifest

ManifestT = TypeVar("ManifestT", bound=StageManifest)


def _absorb(tasks: list[IdentityTask], updated: tuple[IdentityTask, ...] | list[IdentityTask]) -> list[IdentityTask]:
    """Replace tasks the stage just settled, keeping the full list's order."""
    by_key = {task.candidate_key: task for task in updated}
    return [by_key.get(task.candidate_key, task) for task in tasks]


def _judge_tasks(
    tasks: list[IdentityTask],
    *,
    config: OpenAIResponsesConfig,
    owner_block: str,
) -> tuple[list[IdentityTask], IdentityUsage]:
    """Bill one LLM call per task — callers must pre-filter to judgeable tasks."""
    results = judge.judge_batch(
        tasks,
        use_llm=True,
        owner_block=owner_block,
        model=config.model,
        effort=config.effort,
        concurrency=config.concurrency,
        timeout=config.timeout,
        max_retries=config.max_retries,
    )
    usage = IdentityUsage()
    for result in results:
        usage += result.usage
    return [task.with_judgment(result) for task, result in zip(tasks, results)], usage


def run_stage(
    manifest_type: type[ManifestT],
    *,
    db: Db,
    profile_cache_dir: Path,
    verdicts_jsonl: Path,
    confirm_threshold: float,
    detach_threshold: float,
    model: str,
    requested_effort: str,
    concurrency: int | None,
    timeout: int,
    max_retries: int,
    no_overrides: bool,
    reapply: bool,
    force: bool = False,
) -> ManifestT:
    started = time.monotonic()
    # reapply is the only thing that suppresses the judge and RapidAPI: it
    # replays verdicts already bought through the threshold policy. There is no
    # offline switch — a caller that wants to run without a provider stubs the
    # provider, so the stage under test is the same one production runs.
    owner_block = owner_background(db)
    fetch_counts: ProfileFetchCounts | None = None
    usage = IdentityUsage()
    billed = reused_count = 0
    if reapply:
        tasks = load_tasks_from_store(db)
    else:
        tasks = build_tasks(db)
        tasks = [replace(task, verdict=CONNECTION_VERDICT) if task.from_connections else task for task in tasks]
        fetched = fetch_missing_profiles(db, tasks, profile_cache_dir)
        tasks = list(fetched.tasks)
        fetch_counts = fetched.as_counts()
        # The judge answers a question built from evidence + prompt + model +
        # effort. Resolve the config ONCE and fingerprint against the resolved
        # values: OpenAIResponsesConfig.resolve normalizes the effort, so
        # fingerprinting the raw request would never match what the judge stores.
        judge_config = OpenAIResponsesConfig.resolve(
            model=model, effort=requested_effort, concurrency=concurrency,
            timeout=timeout, max_retries=max_retries,
        )
        split = split_reuse(
            db, judgeable_tasks(tasks), config=judge_config, owner_block=owner_block, force=force,
        )
        to_judge = list(split.to_judge)
        # len(to_judge) is exactly the number of LLM calls this run bills.
        billed, reused_count = len(to_judge), len(split.reused)
        if split.reused:
            tasks = _absorb(tasks, split.reused)
        if to_judge:
            judged, usage = _judge_tasks(to_judge, config=judge_config, owner_block=owner_block)
            tasks = _absorb(tasks, judged)
        deterministic = [task for task in tasks if task.verdict is None and not task.error]
        # Free pass: gives every still-unjudged task (no profile, no LLM key) its
        # deterministic verdict so nothing exits run_stage without one. Errored
        # tasks are excluded so a failed judge call isn't silently overwritten
        # with "no usable profile" — it stays unverdicted and gets retried by
        # simply showing up in the queue view again on the next run.
        if deterministic:
            results = judge.judge_batch(
                deterministic,
                use_llm=False,
                owner_block=owner_block,
                model=judge_config.model,
                effort=judge_config.effort,
                concurrency=1,
                timeout=timeout,
                max_retries=max_retries,
            )
            judged = [task.with_judgment(result) for task, result in zip(deterministic, results)]
            tasks = _absorb(tasks, judged)
        # settle_machine_identities requires a fingerprint on every row it
        # projects; this backfills whatever the judge passes above left unset.
        tasks = [
            task
            if task.judgment_fingerprint
            else replace(
                task,
                judgment_fingerprint=judge.task_fingerprint(
                    task, owner_block, model=judge_config.model, effort=judge_config.effort
                ),
            )
            for task in tasks
        ]

    settled = settle(
        db,
        tasks,
        confirm=confirm_threshold,
        detach=detach_threshold,
        artifact_path=verdicts_jsonl,
        project=not no_overrides,
    )
    tasks = list(settled.tasks)
    write_verdicts(verdicts_jsonl, tasks)
    overrides = settled.overrides
    counts = {value: 0 for value in judgment_policy.VERDICTS}
    for task in tasks:
        value = task.verdict.value if task.verdict else ""
        if value in counts:
            counts[value] += 1
    conflicts = [task for task in tasks if task.conflict]
    # Deep-research eligible tasks, gated by the exact detach bar decide_actions
    # just applied (decided.thresholds) — never a second, independently
    # re-resolved detach_threshold; see judgment_policy.deep_research_eligible.
    research = [task for task in tasks if judgment_policy.deep_research_eligible(task, settled.thresholds)]
    billed_output = usage.output_tokens + usage.reasoning_tokens  # reasoning tokens price as output tokens
    return manifest_type(
        status="completed",
        judge="deterministic" if reapply else "llm",
        parents=len({task.parent_id or task.parent_slug for task in tasks}),
        tasks=len(tasks),
        # What this run actually paid for, NOT what was eligible: with reuse the
        # two differ, and the eligible count would overstate the bill every time.
        judged=billed,
        reused=reused_count,
        human_settled=human_settled_identities(db),
        ground_truth_connections=sum(task.from_connections for task in tasks),
        verdicts=counts,
        conflicts=len(conflicts),
        conflicts_auto_resolved=sum(task.via == "conflict_resolved" for task in conflicts),
        conflicts_to_review=sum(task.action == "review" for task in conflicts),
        profile_fetch=fetch_counts,
        errors=sum(bool(task.error) for task in tasks),
        overrides=overrides,
        needs_review=overrides.pending,
        deep_research_eligible=len(research),
        deep_research_est_usd=round(len(research) * 0.05, 2),
        tokens=usage,
        estimated_cost_usd=estimate_cost_usd(usage.input_tokens, billed_output, model),
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
