"""Selection, hydration, judging, and settlement policy for identity healing.

Heals exactly one broken state: an attached LinkedIn link whose most recent
judge run returned "needs_review" at confidence 0 because the fetched profile
had no usable content — a judge-skip, not a wrong verdict. Everything else
(a confirmed/wrong_person verdict, a pending human decision, an in-flight
retarget) is out of scope and untouched. Selection re-fetches fresh, then
splits on what came back: content re-enters the paid judge (``rejudge``), a
clean empty confirms the link is dead and settles locally (``terminate``).
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Callable

from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.ingestion.primitives.deep_context.enrich import identity_evidence, profile_projection
from packs.ingestion.primitives.deep_context.shared.common import load_env
from packs.ingestion.primitives.deep_context.db.identity_views import heal_identity_queue
from packs.ingestion.primitives.deep_context.db.models import (
    JUDGE_CONFIRM_THRESHOLD,
    JUDGE_DETACH_THRESHOLD,
    IdentityOrigin,
    LinkSnapshotRow,
    RowKind,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.db.identity_queries import links
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import (
    DossierEvidence,
    owner_background,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.queue import build_tasks
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile import judgment_policy
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.models import (
    HealCandidate,
    HealFetchResult,
    HealFetchState,
    HealRejudgeResult,
    HealSelection,
    HealTerminationResult,
)
from packs.ingestion.primitives.deep_context.enrich.judge_models import (
    IdentityTask,
    IdentityVerdict,
    JudgeProfile,
)
from packs.ingestion.primitives.deep_context.enrich.profile_models import ProfileTarget
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.results import write_overrides


def select_candidates(
    db: Db,
    cap: int | None,
    say: Callable[[str], None],
) -> HealSelection:
    """Select judge-skipped links eligible for healing.

    ``cap=None`` (the caller default) heals every eligible row every run. A
    cap only limits this run's batch size — every row it excludes stays
    eligible and reappears next run, so a small default silently leaves
    judge-skips unhealed indefinitely rather than erroring.
    """
    # heal_identity_queue applies the actual state filter: needs_review at
    # confidence 0 with this exact machine_reason is the signature of a judge
    # run that fetched no usable profile — the only state this module
    # repairs. A link that failed judging for any other reason, or that a
    # human/auto decision already resolved, never reaches `rows`.
    rows = heal_identity_queue(db, judgment_policy.NO_PROFILE_REASON)
    # A "pending_retarget" row already has a proposed replacement identity
    # queued (the guided-retarget path's territory) — heal skips it rather
    # than racing a fetch/rejudge under an in-flight retarget.
    skipped_retarget = sum(row.selection == "pending_retarget" for row in rows)
    selected = []
    for row in rows:
        if row.selection != "candidate":
            continue
        selected.append(
            HealCandidate(
                parent_id=row.parent_id,
                parent_slug=row.parent_slug,
                name=row.name,
                candidate_key=row.candidate_key,
                public_identifier=row.public_identifier,
                linkedin_url=row.linkedin_url,
            )
        )
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
    targets = [
        ProfileTarget(
            row.public_identifier,
            row.linkedin_url,
            row.candidate_key,
            row.parent_id,
        )
        for row in candidates
    ]
    # fresh=True bypasses the profile cache: the whole reason a link is here
    # is that its last fetch was empty, so serving the cached (empty) result
    # would heal nothing. Every selected candidate re-bills the LinkedIn
    # provider, uncapped by anything but the selection cap above.
    hydrated = profile_projection.hydrate_profiles(
        targets,
        cache_dir,
        db=db,
        max_workers=max_workers,
        fresh=True,
    )
    return HealFetchResult(
        tuple(
            HealFetchState.from_result(
                row.candidate_key,
                hydrated.profiles.get(row.public_identifier.strip().lower()),
            )
            for row in candidates
        )
    )


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
        # Degrades instead of raising: the profile fetch above already spent
        # money and is cached on disk, so a missing key skips only the judge
        # call — these candidates stay judge-skipped and heal again next run
        # rather than losing the fetch.
        return replace(base, skipped_no_openai_key=True)
    # Reuses the queue's own task-building/parsing path rather than
    # constructing IdentityTask directly from HealCandidate rows, so judge
    # input parsing stays defined in exactly one place.
    by_key = {task.candidate_key: task for task in build_tasks(db)}
    tasks = [by_key[row.candidate_key] for row in candidates]
    owner_block = owner_background(db)
    # use_llm=True and effort="high" are pinned, not caller-configurable: heal
    # only reaches identities the first pass already flagged unusable, so it
    # spends the most careful — and most expensive — judge call, not the
    # cheapest one available.
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
            # result.fingerprint (produced by the judge call itself) wins;
            # this recomputes the same paid-cache key only as a fallback if
            # that came back empty — see judgment_fingerprint's docstring for
            # why the key's serialization must stay pinned.
            result,
            fallback_fingerprint=identity_evidence.task_fingerprint(task, owner_block),
        )
        for task, result in zip(tasks, verdicts)
    ]
    actions = judgment_policy.decide_actions(tasks, JUDGE_CONFIRM_THRESHOLD, JUDGE_DETACH_THRESHOLD)
    tasks = [replace(task, action=action.action, via=action.via) for task, action in zip(tasks, actions)]
    # Local write from here on — no further billing. settle_machine_identities
    # still re-checks for a human decision even though selection already
    # filtered those rows out: a user can approve/reject the same row through
    # the review UI while this batch is mid-flight, and that decision must
    # still win (see preserved_user_rows below).
    projected = write_overrides(db, tasks, source=WriterSource.HEAL)
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
    """Detach links whose fresh fetch came back with no profile content at all.

    This is heal's other branch: ``rejudge`` handles candidates whose fresh
    fetch returned content (paid judge decides); ``terminate`` handles
    candidates that fetched clean and empty — a confirmed-dead LinkedIn, not
    a judge failure. No LLM call here: an empty fetch is itself conclusive
    evidence, so the "wrong_person" verdict built below is a fixed local
    judgment, not a rejudge.
    """
    if not candidates:
        return HealTerminationResult(candidates=0)
    owner_block = owner_background(db)
    parent_ids = tuple(sorted({candidate.parent_id for candidate in candidates}))
    synthetic_by_parent = {
        link.parent_id: link
        for link in links(
            db,
            parent_ids=parent_ids,
            kind=RowKind.SYNTHETIC.value,
        )
    }
    tasks: list[IdentityTask] = []
    stood_synthetic = 0
    pending_reresearch = 0
    for candidate in candidates:
        task = IdentityTask(
            candidate_key=candidate.candidate_key,
            action="detach",
            verdict=IdentityVerdict.from_payload(
                {
                    "verdict": "wrong_person",
                    "confidence": 1.0,
                    "reason": "fresh LinkedIn fetch returned no profile content",
                }
            ),
            evidence=DossierEvidence.from_parent_db(db, candidate.parent_id),
            linkedin=JudgeProfile.from_payload(
                {
                    "linkedin_url": candidate.linkedin_url,
                    "full_name": candidate.name,
                    "has_profile": False,
                }
            ),
        )
        tasks.append(
            replace(
                task,
                judgment_fingerprint=identity_evidence.task_fingerprint(task, owner_block),
            )
        )
        synthetic: LinkSnapshotRow | None = synthetic_by_parent.get(candidate.parent_id)
        approved = (synthetic.decision_approved or synthetic.machine_approved or "") if synthetic else ""
        if synthetic and approved == "yes":
            # Human already approved this synthetic as the standing identity —
            # nothing to write, just count it as this dead link's outcome.
            stood_synthetic += 1
        elif synthetic and approved not in {"no", "auto"}:
            # A synthetic exists but nobody has ruled on it: propose it as the
            # replacement now, at fixed confidence — still no LLM call, since
            # a synthetic identity is itself already-summarized fact, not new
            # evidence to weigh.
            synthetic_task = IdentityTask(
                candidate_key=synthetic.row_key,
                action="confirm",
                verdict=IdentityVerdict.from_payload(
                    {
                        "verdict": "confirmed",
                        "confidence": 1.0,
                        "reason": "standing synthetic identity for dead attached link",
                    }
                ),
                evidence=DossierEvidence.from_parent_db(db, candidate.parent_id),
                linkedin=JudgeProfile.from_payload(
                    {
                        "linkedin_url": synthetic.linkedin_url or "",
                        "full_name": synthetic.display_name or candidate.name,
                        "has_profile": True,
                    }
                ),
            )
            synthetic_task = replace(
                synthetic_task,
                judgment_fingerprint=(
                    # ATTACHED, not a synthetic-specific origin: IdentityOrigin
                    # only distinguishes attached vs. research-sourced evidence
                    # for threshold/fingerprint policy, and a synthetic being
                    # confirmed here is standing in for the (now-detached)
                    # attached link, so it takes that policy.
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
            # No usable synthetic to fall back on (none exists, or one was
            # already rejected/auto-settled) — the person stays LinkedIn-less
            # until guided re-research finds a replacement.
            pending_reresearch += 1
    projected = write_overrides(db, tasks, source=WriterSource.HEAL)
    return HealTerminationResult(
        candidates=len(candidates),
        detached=projected.detached,
        # Two sources merge into one count: `stood_synthetic` counts
        # already-human-approved synthetics (nothing written above);
        # `projected.verified` counts the ones this run just confirmed. Both
        # mean "this person now has a standing synthetic identity."
        stood_synthetic=stood_synthetic + projected.verified,
        pending_reresearch=pending_reresearch,
        skipped_human_decided=projected.preserved_user_rows,
    )
