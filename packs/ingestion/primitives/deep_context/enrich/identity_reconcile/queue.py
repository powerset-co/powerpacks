"""SQLite task selection and cached LinkedIn profile hydration.

Building a task here never spends. ``profile_fetch_candidates`` marks the
RapidAPI misses and ``fetch_missing_profiles`` is the one call that bills.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db.identity_views import attached_identity_queue, human_settled_identities
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence, owner_background
from packs.ingestion.primitives.deep_context.enrich.profiles import projection
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile import judge
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile import judgment_policy
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judgment_policy import stored_judgments
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.models import (
    IdentityProfileSource,
    ProfileFetchResult,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import (
    IdentityTask,
    IdentityVerdict,
    JudgeProfile,
)
from packs.ingestion.primitives.deep_context.enrich.profiles.models import (
    NormalizedProfile,
    ProfileExperience,
    ProfileResult,
    ProfileTarget,
)
from packs.ingestion.primitives.deep_context.shared.openai_responses import (
    OpenAIResponsesConfig,
    normalize_reasoning_effort,
)


def _span(entry: ProfileExperience) -> str:
    start = str(entry.starts_at or "")
    end = str(entry.ends_at or "")
    return f"{start}–{end}" if start and end else f"{start}–present" if start else end


def linkedin_view(
    row: IdentityProfileSource,
    projected: ProfileResult | None = None,
) -> JudgeProfile:
    """Parse one SQLite/provider profile into the sole judge-facing shape."""
    # "cache" means a profile fetch was attempted, successful or not; "fallback"
    # means no fetch happened and only the raw import/attached row is available.
    profile: NormalizedProfile | None = projected.normalized_profile if projected is not None else None
    if profile is not None and profile.present:
        if not profile.success:
            # Fetch attempted and failed: keep the identifier we searched for
            # rather than whatever (if anything) the failed response carried.
            public_identifier = row.public_identifier.strip().lower()
        else:
            public_identifier = (profile.public_identifier or "").strip().lower()
        experiences = profile.experiences
        education = profile.education
        location = profile.location or ""
        full_name = profile.full_name or ""
        headline = profile.headline or ""
        picture = profile.profile_pic_url or ""
        source = "cache"
    else:
        public_identifier = row.public_identifier.strip().lower()
        # IdentityProfileSource is identity-only — it has no work/education/
        # location fields to show until fetch_missing_profiles hydrates a real
        # ProfileResult and this branch stops firing.
        experiences = ()
        education = ()
        location = ""
        full_name = row.full_name or row.display_name
        headline = row.headline
        picture = row.profile_picture_url
        source = "fallback"
    work = []
    for item in experiences:
        title = item.title or ""
        company = item.company_name or ""
        text = " @ ".join(value for value in (title, company) if value)
        span = _span(item)
        if text:
            work.append(f"{text}{f' ({span})' if span else ''}")
    schools = []
    for item in education:
        school = item.school_name or ""
        degree = ", ".join(value for value in (item.degree, item.field) if value)
        text = f"{degree} — {school}" if degree and school else degree or school
        if text:
            schools.append(text)
    return JudgeProfile.from_payload(
        {
            "public_identifier": public_identifier,
            "linkedin_url": row.linkedin_url,
            "full_name": str(full_name),
            "headline": str(headline),
            "profile_pic_url": str(picture),
            "experiences": work,
            "education": schools,
            "location": location,
            "source": source,
            # Gates profile_fetch_candidates (skip re-fetching) and runner's
            # judgeable filter (skip judging) — the one "enough LinkedIn signal
            # to act on" bit downstream.
            "has_profile": bool((profile and profile.present) or work or schools or headline),
        }
    )


def build_tasks(db: Db) -> list[IdentityTask]:
    """Assemble tasks from the current queue view; no judging happens here.

    Also the read path ``results.load_tasks_from_store`` replays against for
    ``reapply``, so a row dropping out of the queue view is invisible there too.
    """
    tasks: list[IdentityTask] = []
    rows = attached_identity_queue(db)
    profiles = projection.profile_payloads(db, (row.candidate_key for row in rows))
    for row in rows:
        evidence = DossierEvidence.from_parent_db(db, row.parent_id)
        tasks.append(
            IdentityTask(
                parent_slug=row.parent_slug,
                parent_id=row.parent_id,
                name=row.name,
                candidate_key=row.candidate_key,
                person_ids=row.person_ids,
                conflict=row.conflict,
                evidence=evidence,
                linkedin=linkedin_view(
                    IdentityProfileSource(
                        public_identifier=row.public_identifier,
                        linkedin_url=row.linkedin_url,
                        display_name=row.name,
                    ),
                    profiles.get(row.candidate_key),
                ),
                from_connections=row.from_connections,
            )
        )
    return tasks


def judgeable_tasks(tasks: list[IdentityTask]) -> list[IdentityTask]:
    """The tasks the paid judge can actually answer.

    An imported LinkedIn connection is ground truth (run_stage stamps it with
    CONNECTION_VERDICT and never pays), and a row whose profile fetch found
    nothing has no candidate to judge the evidence against.
    """
    return [task for task in tasks if not task.from_connections and task.linkedin.has_profile]


@dataclass(frozen=True)
class ReuseSplit:
    """Judgeable tasks already paid for, and the ones this run will bill."""

    reused: tuple[IdentityTask, ...]
    to_judge: tuple[IdentityTask, ...]


def split_reuse(
    db: Db,
    tasks: list[IdentityTask],
    *,
    config: OpenAIResponsesConfig,
    owner_block: str,
    force: bool = False,
) -> ReuseSplit:
    """THE billing decision, made once for both the estimate and the run.

    The dry run and run_stage call this same function, so the predicted bill
    cannot drift from the real one (they used to be hand-copied loops).
    Reused tasks come back carrying the stored verdict and the fingerprint
    that matched, so a caller can merge them straight back into its list.
    """
    stored = stored_judgments(db)
    reused: list[IdentityTask] = []
    to_judge: list[IdentityTask] = []
    for task in tasks:
        fingerprint = judge.task_fingerprint(
            task, owner_block, model=config.model, effort=config.effort,
        )
        prior = stored.get(task.candidate_key)
        if judgment_policy.reuses_stored_verdict(prior, fingerprint, force=force):
            reused.append(replace(task, verdict=prior.verdict, judgment_fingerprint=fingerprint, error=""))
        else:
            to_judge.append(task)
    return ReuseSplit(tuple(reused), tuple(to_judge))


# run_stage stamps this on every from_connections task before judging — an
# imported LinkedIn connection is ground truth and never reaches the paid judge.
CONNECTION_VERDICT = IdentityVerdict.from_payload(
    {
        "verdict": "confirmed",
        "confidence": 1.0,
        "supporting_evidence": ["LinkedIn Connections import"],
        "contradicting_evidence": [],
        "linkedin_plausibly_absent": False,
        "recommend_deep_research": False,
        "reason": "Ground truth: this profile came from your LinkedIn Connections import.",
    }
)


def profile_fetch_candidates(tasks: list[IdentityTask]) -> list[IdentityTask]:
    """Tasks still missing a cached profile — this list's length is the RapidAPI bill."""
    return [
        task
        for task in tasks
        if not task.from_connections and task.linkedin.linkedin_url and not task.linkedin.has_profile
    ]


def fetch_missing_profiles(
    db: Db,
    tasks: list[IdentityTask],
    cache_dir: Path,
    *,
    max_workers: int = 8,
) -> ProfileFetchResult:
    wanted = profile_fetch_candidates(tasks)
    if not wanted:
        return ProfileFetchResult(tuple(tasks))
    if not projection.provider_key_available():
        # No RapidAPI key: every candidate stays profile-less rather than failing
        # the stage — each falls through to the deterministic "no usable profile"
        # verdict in run_stage instead of getting judged.
        return ProfileFetchResult(
            tuple(tasks),
            fetch_wanted=len(wanted),
            fetch_skipped_no_key=len(wanted),
        )
    targets = [
        ProfileTarget(
            task.linkedin.public_identifier,
            task.linkedin.linkedin_url,
            task.candidate_key,
            task.parent_id,
        )
        for task in wanted
        if task.candidate_key and task.parent_id
    ]

    hydrated = projection.hydrate_profiles(targets, cache_dir, db=db, max_workers=max_workers)
    wanted_keys = {task.candidate_key for task in wanted}
    profiles = projection.profile_payloads(db, wanted_keys)
    refreshed = tuple(
        replace(
            task,
            linkedin=linkedin_view(
                IdentityProfileSource(
                    public_identifier=task.linkedin.public_identifier,
                    linkedin_url=task.linkedin.linkedin_url,
                    full_name=task.linkedin.full_name,
                    headline=task.linkedin.headline,
                    profile_picture_url=task.linkedin.profile_pic_url,
                ),
                profiles.get(task.candidate_key),
            ),
        )
        if task.candidate_key in wanted_keys and profiles.get(task.candidate_key)
        else task
        for task in tasks
    )
    return ProfileFetchResult(
        refreshed,
        fetch_wanted=len(wanted),
        fetch_ok=hydrated.ok,
        fetch_failed=hydrated.failed,
    )


def dry_run_estimate(*, db: Db, model: str, effort: str) -> dict[str, Any]:
    """Pre-flight cost estimate — never fetches or judges, only counts what would.

    ``estimated_rapidapi_credits`` assumes 1 credit per profile-fetch miss.
    ``estimated_cost_usd_*`` is a flat per-task band, not the priced-per-model
    figure ``estimate_cost_usd`` computes from actual token usage after a run.
    """
    started = time.monotonic()
    tasks = build_tasks(db)
    judgeable = judgeable_tasks(tasks)
    # The estimate runs run_stage's own split, against the same resolved config
    # (resolve() normalizes effort, and the fingerprint hashes the resolved
    # value) — so what this predicts is what that bills.
    judge_config = OpenAIResponsesConfig.resolve(
        model=model, effort=effort, concurrency=None, timeout=120, max_retries=6,
    )
    split = split_reuse(db, judgeable, config=judge_config, owner_block=owner_background(db))
    reused, billed = split.reused, len(split.to_judge)
    misses = len(profile_fetch_candidates(tasks))
    return {
        "source": "reconcile_linkedin",
        "status": "dry_run",
        "profile_fetch_misses": misses,
        "estimated_rapidapi_credits": misses,
        "parents": len({task.parent_id for task in tasks}),
        "tasks": len(tasks),
        "judgeable": len(judgeable),
        "reused": len(reused),
        "human_settled": human_settled_identities(db),
        "billed": billed,
        "ground_truth_connections": sum(task.from_connections for task in tasks),
        "conflicts": sum(task.conflict for task in tasks),
        "estimated_cost_usd_low": round(billed * 0.004, 2),
        "estimated_cost_usd_high": round(billed * 0.02, 2),
        "model": model,
        "reasoning_effort": normalize_reasoning_effort(effort),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "updated_at": now_iso(),
    }
