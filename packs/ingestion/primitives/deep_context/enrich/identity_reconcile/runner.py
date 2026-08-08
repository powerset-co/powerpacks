"""Paid/free attached-identity stage runner over typed queue and result policy."""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import TypeVar

from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import owner_background
from packs.ingestion.primitives.deep_context.enrich import identity_evidence
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile import judgment_policy
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.queue import (
    CONNECTION_VERDICT,
    fetch_missing_profiles,
    select_tasks,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.models import (
    ProfileFetchCounts,
)
from packs.ingestion.primitives.deep_context.enrich.judge_models import (
    IdentityTask,
    IdentityUsage,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.results import (
    load_tasks_from_store,
    merge_subset_tasks,
    write_overrides,
    write_verdicts,
)
from packs.ingestion.primitives.deep_context.shared.openai_responses import (
    estimate_cost_usd,
)
from packs.ingestion.primitives.pipeline.contract import StageManifest

ManifestT = TypeVar("ManifestT", bound=StageManifest)


def _judge_tasks(
    db: Db,
    tasks: list[IdentityTask],
    *,
    model: str,
    requested_effort: str,
    requested_concurrency: int | None,
    timeout: int,
    max_retries: int,
) -> tuple[list[IdentityTask], IdentityUsage]:
    """Bill one LLM call per task — callers must pre-filter to judgeable tasks."""
    usage = IdentityUsage()
    owner_block = owner_background(db)

    results = identity_evidence.judge_batch(
        tasks,
        use_llm=True,
        owner_block=owner_block,
        model=model,
        effort=requested_effort,
        concurrency=requested_concurrency,
        timeout=timeout,
        max_retries=max_retries,
    )
    judged = []
    for task, result in zip(tasks, results):
        judged.append(
            task.with_judgment(
                result,
                fallback_fingerprint=identity_evidence.task_fingerprint(task, owner_block),
            )
        )
        usage += result.usage
    return judged, usage


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
    slugs: list[str],
    limit: int | None,
    no_overrides: bool,
    no_llm: bool,
    reapply: bool,
) -> ManifestT:
    started = time.monotonic()
    # reapply never calls the judge or RapidAPI: it only reruns the threshold
    # policy over verdicts already paid for and persisted in judgment_payload_json.
    use_llm = not no_llm and not reapply
    owner_block = owner_background(db)
    fetch_counts: ProfileFetchCounts | None = None
    usage = IdentityUsage()
    if reapply:
        tasks = load_tasks_from_store(db)
    else:
        tasks = select_tasks(db, slugs, limit)
        tasks = [replace(task, verdict=CONNECTION_VERDICT) if task.from_connections else task for task in tasks]
        if use_llm:
            fetched = fetch_missing_profiles(db, tasks, profile_cache_dir)
            tasks = list(fetched.tasks)
            fetch_counts = fetched.as_counts()
        judgeable = [task for task in tasks if not task.from_connections and task.linkedin.has_profile]
        # len(judgeable) is exactly the number of LLM calls this run bills.
        if use_llm and judgeable:
            judged, usage = _judge_tasks(
                db,
                judgeable,
                model=model,
                requested_effort=requested_effort,
                requested_concurrency=concurrency,
                timeout=timeout,
                max_retries=max_retries,
            )
            by_key = {task.candidate_key: task for task in judged}
            tasks = [by_key.get(task.candidate_key, task) for task in tasks]
        deterministic = [task for task in tasks if task.verdict is None and not task.error]
        # Free pass: gives every still-unjudged task (no profile, no LLM key) its
        # deterministic verdict so nothing exits run_stage without one. Errored
        # tasks are excluded so a failed judge call isn't silently overwritten
        # with "no usable profile" — it stays unverdicted and gets retried by
        # simply showing up in the queue view again on the next run.
        if deterministic:
            results = identity_evidence.judge_batch(
                deterministic,
                use_llm=False,
                owner_block=owner_block,
                model=model,
                effort=requested_effort,
                concurrency=1,
                timeout=timeout,
                max_retries=max_retries,
            )
            judged = [
                task.with_judgment(
                    result,
                    fallback_fingerprint=identity_evidence.task_fingerprint(task, owner_block),
                )
                for task, result in zip(deterministic, results)
            ]
            by_key = {task.candidate_key: task for task in judged}
            tasks = [by_key.get(task.candidate_key, task) for task in tasks]
        # settle_machine_identities requires a fingerprint on every row it
        # projects; this backfills whatever the judge passes above left unset.
        tasks = [
            task
            if task.judgment_fingerprint
            else replace(
                task,
                judgment_fingerprint=identity_evidence.task_fingerprint(task, owner_block),
            )
            for task in tasks
        ]
        if slugs or limit:
            # A scoped run still settles the whole graph, not just the subset:
            # merge in stored verdicts for every parent this run didn't touch so
            # write_overrides and the manifest below reflect all parents.
            tasks = merge_subset_tasks(db, tasks)

    actions = judgment_policy.decide_actions(tasks, confirm_threshold, detach_threshold)
    tasks = [replace(task, action=action.action, via=action.via) for task, action in zip(tasks, actions)]
    write_verdicts(verdicts_jsonl, tasks)
    overrides = write_overrides(
        db,
        [] if no_overrides else tasks,
        artifact_path=None if no_overrides else verdicts_jsonl,
    )
    counts = {value: 0 for value in judgment_policy.VERDICTS}
    for task in tasks:
        value = task.verdict.value if task.verdict else ""
        if value in counts:
            counts[value] += 1
    conflicts = [task for task in tasks if task.conflict]
    # Deep-research eligible: a confident detach the judge itself flagged as worth
    # chasing, unless it already concluded no LinkedIn plausibly exists for them.
    research = [
        task
        for task in tasks
        if task.verdict
        and task.verdict.value == "wrong_person"
        and task.verdict.confidence >= detach_threshold
        and task.verdict.recommend_deep_research
        and not task.verdict.linkedin_plausibly_absent
    ]
    billed_output = usage.output_tokens + usage.reasoning_tokens  # reasoning tokens price as output tokens
    return manifest_type(
        status="completed",
        judge="llm" if use_llm else "deterministic",
        parents=len({task.parent_id or task.parent_slug for task in tasks}),
        tasks=len(tasks),
        judged=sum(not task.from_connections and task.linkedin.has_profile for task in tasks),
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
