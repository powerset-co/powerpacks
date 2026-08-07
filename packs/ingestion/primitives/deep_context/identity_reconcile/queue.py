"""SQLite task selection and cached LinkedIn profile hydration."""
from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from packs.indexing.lib.openai_responses import reasoning_effort
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db.identity_views import (
    AttachedIdentityQueueRow,
    linkedin_review,
)
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.dossier_evidence import DossierEvidence
from packs.ingestion.primitives.deep_context import profile_projection
from packs.ingestion.primitives.deep_context.identity_reconcile.models import (
    IdentityProfileSource,
    ProfileFetchResult,
)
from packs.ingestion.primitives.deep_context.judge_models import (
    IdentityTask,
    IdentityVerdict,
    JudgeProfile,
)
from packs.ingestion.schemas.people_schema import parse_jsonish


def identity_profile_source(*, linkedin_url: str) -> IdentityProfileSource:
    """Build the attached-stage profile row used by shared research judging."""
    return IdentityProfileSource(linkedin_url=linkedin_url)


def _span(entry: dict[str, Any]) -> str:
    def year(value: object) -> str:
        return str(value.get("year") or "") if isinstance(value, dict) else ""

    start, end = year(entry.get("starts_at")), year(entry.get("ends_at"))
    return f"{start}–{end}" if start and end else f"{start}–present" if start else end


def linkedin_view(
    row: IdentityProfileSource,
    projected: dict[str, Any] | None = None,
) -> JudgeProfile:
    """Parse one SQLite/provider profile into the sole judge-facing shape."""
    profile = (projected or {}).get("normalized_profile")
    if isinstance(profile, dict):
        if profile.get("success") is not True:
            public_identifier = row.public_identifier.strip().lower()
        else:
            public_identifier = str(profile.get("public_identifier") or "").strip().lower()
        experiences = profile.get("experiences") or []
        education = profile.get("education") or []
        location = profile.get("location_str") or ", ".join(
            str(profile.get(key) or "") for key in ("city", "state", "country")
            if profile.get(key)
        )
        full_name = profile.get("full_name") or ""
        headline = profile.get("headline") or ""
        picture = profile.get("profile_pic_url") or ""
        source = "cache"
    else:
        public_identifier = row.public_identifier.strip().lower()
        experiences = parse_jsonish(row.work_experiences, []) or []
        education = parse_jsonish(row.education, []) or []
        location = ", ".join(
            value for value in (row.city, row.state, row.country) if value
        )
        full_name = row.full_name or row.display_name
        headline = row.headline
        picture = row.profile_picture_url
        source = "fallback"
    work = []
    for item in experiences:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "")
        company = str(item.get("company_name") or "")
        text = " @ ".join(value for value in (title, company) if value)
        span = _span(item)
        if text:
            work.append(f"{text}{f' ({span})' if span else ''}")
    schools = []
    for item in education:
        if not isinstance(item, dict):
            continue
        school = str(item.get("school_name") or "")
        degree = ", ".join(
            str(item.get(key) or "") for key in ("degree", "field") if item.get(key)
        )
        text = f"{degree} — {school}" if degree and school else degree or school
        if text:
            schools.append(text)
    return JudgeProfile.from_payload({
        "public_identifier": public_identifier,
        "linkedin_url": row.linkedin_url,
        "full_name": str(full_name),
        "headline": str(headline),
        "profile_pic_url": str(picture),
        "experiences": work,
        "education": schools,
        "location": location,
        "source": source,
        "has_profile": bool(profile or work or schools or headline),
    })


def build_tasks(db: Db) -> list[IdentityTask]:
    graph = canonical_snapshot(db)
    profiles = profile_projection.profile_payloads(graph)
    tasks: list[IdentityTask] = []
    rows = cast(list[AttachedIdentityQueueRow], linkedin_review(db, "attached"))
    for row in rows:
        evidence = DossierEvidence.from_parent(row.parent_id, graph)
        tasks.append(IdentityTask(
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
        ))
    return tasks


def select_tasks(
    db: Db, slugs: list[str] | None, limit: int,
) -> list[IdentityTask]:
    tasks = build_tasks(db)
    if slugs:
        wanted = {value.lower() for value in slugs}
        tasks = [
            task for task in tasks
            if task.parent_slug.lower() in wanted
        ]
    return tasks[:limit] if limit else tasks


CONNECTION_VERDICT = IdentityVerdict.from_payload({
    "verdict": "confirmed", "confidence": 1.0,
    "supporting_evidence": ["LinkedIn Connections import"],
    "contradicting_evidence": [], "linkedin_plausibly_absent": False,
    "recommend_deep_research": False,
    "reason": "Ground truth: this profile came from your LinkedIn Connections import.",
})


def profile_fetch_candidates(tasks: list[IdentityTask]) -> list[IdentityTask]:
    return [
        task for task in tasks
        if not task.from_connections
        and task.linkedin.linkedin_url
        and not task.linkedin.has_profile
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
    if not profile_projection.provider_key_available():
        return ProfileFetchResult(
            tuple(tasks),
            fetch_wanted=len(wanted),
            fetch_skipped_no_key=len(wanted),
        )
    targets = [{
        "public_identifier": task.linkedin.public_identifier,
        "linkedin_url": task.linkedin.linkedin_url,
        "candidate_key": task.candidate_key,
        "parent_id": task.parent_id,
    } for task in wanted if task.candidate_key and task.parent_id]

    hydrated, _ = profile_projection.hydrate_profiles(
        targets, cache_dir, db=db, max_workers=max_workers
    )
    profiles = profile_projection.profile_payloads(canonical_snapshot(db))
    wanted_keys = {task.candidate_key for task in wanted}
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
        fetch_ok=hydrated["ok"],
        fetch_failed=hydrated["failed"],
    )


def dry_run_estimate(
    *, db: Db, model: str, effort: str,
    slug: list[str] | None = None, limit: int = 0,
) -> dict[str, Any]:
    started = time.monotonic()
    tasks = select_tasks(db, slug, limit)
    judgeable = [
        task for task in tasks
        if not task.from_connections and task.linkedin.has_profile
    ]
    misses = len(profile_fetch_candidates(tasks))
    return {
        "source": "reconcile_linkedin", "status": "dry_run",
        "profile_fetch_misses": misses, "estimated_rapidapi_credits": misses,
        "parents": len({task.parent_id for task in tasks}), "tasks": len(tasks),
        "judgeable": len(judgeable), "no_link": 0,
        "identity_judgeable": len(judgeable),
        "ground_truth_connections": sum(task.from_connections for task in tasks),
        "conflicts": sum(task.conflict for task in tasks),
        "estimated_cost_usd_low": round(len(judgeable) * 0.004, 2),
        "estimated_cost_usd_high": round(len(judgeable) * 0.02, 2),
        "model": model, "reasoning_effort": reasoning_effort(effort),
        "elapsed_ms": int((time.monotonic() - started) * 1000), "updated_at": now_iso(),
    }
