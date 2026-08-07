"""Selection, hydration, judging, and settlement policy for identity healing."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Callable, cast

from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.ingestion.primitives.deep_context import identity_evidence, profile_projection
from packs.ingestion.primitives.deep_context.common import load_env
from packs.ingestion.primitives.deep_context.db.identity_views import (
    HealIdentityQueueRow,
    linkedin_review,
)
from packs.ingestion.primitives.deep_context.db.models import (
    JUDGE_CONFIRM_THRESHOLD,
    JUDGE_DETACH_THRESHOLD,
    IdentityOrigin,
    ReviewSource,
    RowKind,
)
from packs.ingestion.primitives.deep_context.db.snapshots import (
    canonical_snapshot,
    identity_snapshot,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.dossier_evidence import (
    DossierEvidence,
    owner_background,
)
from packs.ingestion.primitives.deep_context.identity_reconcile.queue import build_tasks
from packs.ingestion.primitives.deep_context.identity_reconcile import judgment_policy
from packs.ingestion.primitives.deep_context.identity_reconcile.models import (
    HealCandidate,
    HealFetchResult,
    HealFetchState,
    HealRejudgeResult,
    HealSelection,
    HealTerminationResult,
)
from packs.ingestion.primitives.deep_context.judge_models import (
    IdentityTask,
    IdentityVerdict,
    JudgeProfile,
)
from packs.ingestion.primitives.deep_context.identity_reconcile.results import write_overrides


def select_candidates(
    db: Db,
    cap: int | None,
    say: Callable[[str], None],
) -> HealSelection:
    rows = cast(
        list[HealIdentityQueueRow],
        linkedin_review(
            db,
            "heal",
            no_profile_reason=judgment_policy.NO_PROFILE_REASON,
        ),
    )
    skipped_retarget = sum(row.selection == "pending_retarget" for row in rows)
    selected = []
    for row in rows:
        if row.selection != "candidate":
            continue
        selected.append(HealCandidate(
            parent_id=row.parent_id,
            parent_slug=row.parent_slug,
            name=row.name,
            candidate_key=row.candidate_key,
            public_identifier=row.public_identifier,
            linkedin_url=row.linkedin_url,
        ))
    uncapped = len(selected)
    if cap is not None:
        selected = selected[:cap]
    if len(selected) < uncapped:
        say(f"cap {cap}: healing {len(selected)} of {uncapped}")
    return HealSelection(tuple(selected), skipped_retarget, uncapped)


def fetch_states(
    db: Db,
    candidates: tuple[HealCandidate, ...] | list[HealCandidate],
    cache_dir: Path,
    *,
    max_workers: int,
    say: Callable[[str], None],
) -> HealFetchResult:
    if not candidates:
        return HealFetchResult(())
    say(f"requesting {len(candidates)} fresh LinkedIn profiles")
    targets = [{
        "public_identifier": row.public_identifier,
        "linkedin_url": row.linkedin_url,
        "candidate_key": row.candidate_key,
        "parent_id": row.parent_id,
    } for row in candidates]
    _, profiles = profile_projection.hydrate_profiles(
        targets,
        cache_dir,
        db=db,
        max_workers=max_workers,
        fresh=True,
    )
    return HealFetchResult(tuple(
        HealFetchState.from_payload(
            row.candidate_key,
            profiles.get(row.public_identifier.strip().lower()) or {
                "state": profile_projection.PROFILE_ERROR,
            },
        )
        for row in candidates
    ))


def rejudge(
    db: Db,
    candidates: tuple[HealCandidate, ...] | list[HealCandidate],
    *,
    concurrency: int,
) -> HealRejudgeResult:
    base = HealRejudgeResult(
        candidates=len(candidates),
        parents=len({row.parent_id for row in candidates}),
    )
    if not candidates:
        return base
    load_env()
    if not (os.environ.get("OPENAI_API_KEY") or "").strip():
        return replace(base, skipped_no_openai_key=True)
    by_key = {task.candidate_key: task for task in build_tasks(db)}
    tasks = [by_key[row.candidate_key] for row in candidates]
    owner_block = owner_background(canonical_snapshot(db))
    verdicts = identity_evidence.judge_batch(
        tasks,
        use_llm=True,
        owner_block=owner_block,
        model=DEFAULT_MODEL,
        effort="high",
        concurrency=concurrency,
        timeout=120,
        max_retries=6,
    )
    tasks = [
        task.with_judgment(
            result,
            fallback_fingerprint=identity_evidence.task_fingerprint(
                task, owner_block
            ),
        )
        for task, result in zip(tasks, verdicts)
    ]
    actions = judgment_policy.decide_actions(
        tasks, JUDGE_CONFIRM_THRESHOLD, JUDGE_DETACH_THRESHOLD
    )
    tasks = [
        replace(task, action=action.action, via=action.via)
        for task, action in zip(tasks, actions)
    ]
    projected = write_overrides(db, tasks, source=ReviewSource.HEAL)
    return replace(
        base,
        verified=projected.verified,
        detached=projected.detached,
        pending=projected.pending,
    )


def terminate(
    db: Db,
    candidates: tuple[HealCandidate, ...] | list[HealCandidate],
) -> HealTerminationResult:
    if not candidates:
        return HealTerminationResult(candidates=0)
    snapshot = canonical_snapshot(db)
    owner_block = owner_background(snapshot)
    synthetic_by_parent = {
        link.parent_id: link
        for link in identity_snapshot(db).links
        if link.kind == RowKind.SYNTHETIC.value
    }
    tasks: list[IdentityTask] = []
    stood_synthetic = 0
    pending_reresearch = 0
    for candidate in candidates:
        task = IdentityTask(
            candidate_key=candidate.candidate_key,
            action="detach",
            verdict=IdentityVerdict.from_payload({
                "verdict": "wrong_person",
                "confidence": 1.0,
                "reason": "fresh LinkedIn fetch returned no profile content",
            }),
            evidence=DossierEvidence.from_parent(candidate.parent_id, snapshot),
            linkedin=JudgeProfile.from_payload({
                "linkedin_url": candidate.linkedin_url,
                "full_name": candidate.name,
                "has_profile": False,
            }),
        )
        tasks.append(replace(
            task,
            judgment_fingerprint=identity_evidence.task_fingerprint(
                task, owner_block
            ),
        ))
        synthetic = synthetic_by_parent.get(candidate.parent_id)
        approved = (
            synthetic.decision_approved or synthetic.machine_approved or ""
        ) if synthetic else ""
        if synthetic and approved == "yes":
            stood_synthetic += 1
        elif synthetic and approved not in {"no", "auto"}:
            synthetic_task = IdentityTask(
                candidate_key=synthetic.row_key,
                action="confirm",
                verdict=IdentityVerdict.from_payload({
                    "verdict": "confirmed",
                    "confidence": 1.0,
                    "reason": "standing synthetic identity for dead attached link",
                }),
                evidence=DossierEvidence.from_parent(
                    candidate.parent_id, snapshot
                ),
                linkedin=JudgeProfile.from_payload({
                    "linkedin_url": synthetic.linkedin_url or "",
                    "full_name": synthetic.display_name or candidate.name,
                    "has_profile": True,
                }),
            )
            synthetic_task = replace(
                synthetic_task,
                judgment_fingerprint=(
                identity_evidence.judgment_fingerprint(
                    synthetic_task.evidence,
                    synthetic_task.linkedin,
                    IdentityOrigin.ATTACHED,
                    owner_block,
                )
                ),
            )
            tasks.append(synthetic_task)
        else:
            pending_reresearch += 1
    projected = write_overrides(db, tasks, source=ReviewSource.HEAL)
    return HealTerminationResult(
        candidates=len(candidates),
        detached=projected.detached,
        stood_synthetic=stood_synthetic + projected.verified,
        pending_reresearch=pending_reresearch,
        skipped_human_decided=projected.preserved_user_rows,
    )
