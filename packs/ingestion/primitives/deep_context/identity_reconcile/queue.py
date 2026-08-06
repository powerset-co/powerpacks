"""SQLite task selection and cached LinkedIn profile hydration."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from packs.indexing.lib.openai_responses import reasoning_effort
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db.models import ResearchHandle, RowKind
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot, identity_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.dossier_evidence import DossierEvidence
from packs.ingestion.primitives.deep_context import profile_projection
from packs.ingestion.schemas.people_schema import extract_public_identifier, parse_jsonish


def _span(entry: dict[str, Any]) -> str:
    def year(value: object) -> str:
        return str(value.get("year") or "") if isinstance(value, dict) else ""

    start, end = year(entry.get("starts_at")), year(entry.get("ends_at"))
    return f"{start}–{end}" if start and end else f"{start}–present" if start else end


def linkedin_view(
    row: dict[str, Any], projected: dict[str, Any] | None = None
) -> dict[str, Any]:
    public_identifier = (
        str(row.get("public_identifier") or "").strip().lower()
        or extract_public_identifier(str(row.get("linkedin_url") or "")).lower()
    )
    profile = (projected or {}).get("normalized_profile")
    if isinstance(profile, dict):
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
        experiences = parse_jsonish(row.get("work_experiences"), []) or []
        education = parse_jsonish(row.get("education"), []) or []
        location = ", ".join(
            str(row.get(key) or "") for key in ("city", "state", "country") if row.get(key)
        )
        full_name = row.get("full_name") or row.get("display_name") or ""
        headline = row.get("headline") or ""
        picture = row.get("profile_picture_url") or ""
        source = "fallback"
    work = []
    for item in experiences:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "")
        company = str(item.get("company_name") or item.get("company") or "")
        text = " @ ".join(value for value in (title, company) if value)
        span = _span(item)
        if text:
            work.append(f"{text}{f' ({span})' if span else ''}")
    schools = []
    for item in education:
        if not isinstance(item, dict):
            continue
        school = str(item.get("school") or item.get("school_name") or "")
        degree = ", ".join(
            str(item.get(key) or "") for key in ("degree", "field") if item.get(key)
        )
        text = f"{degree} — {school}" if degree and school else degree or school
        if text:
            schools.append(text)
    return {
        "public_identifier": public_identifier,
        "linkedin_url": str(row.get("linkedin_url") or ""),
        "full_name": str(full_name),
        "headline": str(headline),
        "profile_pic_url": str(picture),
        "experiences": work,
        "education": schools,
        "location": location,
        "source": source,
        "has_profile": bool(profile or work or schools or headline),
    }


def build_tasks(db: Db) -> list[dict[str, Any]]:
    graph, identity = canonical_snapshot(db), identity_snapshot(db)
    profiles = profile_projection.profile_payloads(graph)
    parents = {row.parent_id: row for row in graph.parents}
    parent_people: dict[str, list[str]] = {}
    for person in graph.people:
        if not person.is_owner and not person.is_ghost:
            parent_people.setdefault(person.parent_id, []).append(person.person_id)
    members: dict[str, list[str]] = {}
    for membership in identity.memberships:
        members.setdefault(membership.row_key, []).append(membership.person_id)
    sources: dict[str, set[str]] = {}
    for source in graph.sources:
        sources.setdefault(source.person_id, set()).add(source.source)

    tasks: list[dict[str, Any]] = []
    for link in identity.links:
        if not link.linkedin_url or link.kind in {RowKind.SYNTHETIC.value, RowKind.RESEARCH.value}:
            continue
        parent = parents.get(link.parent_id)
        if parent is None:
            continue
        all_people = sorted(parent_people.get(link.parent_id, []))
        person_ids = sorted(members.get(link.row_key) or all_people)
        profile_row = {
            "public_identifier": str(link.public_identifier or "").lower(),
            "linkedin_url": link.linkedin_url or "",
            "display_name": link.display_name or "",
            "candidate_key": link.row_key,
            "parent_id": link.parent_id,
        }
        evidence = DossierEvidence.from_parent(link.parent_id, graph)
        tasks.append({
            "parent_slug": ResearchHandle.for_parent(parent.parent_id, parent.display_slug),
            "parent_id": parent.parent_id,
            "name": parent.display_name or link.display_name or parent.public_identifier,
            "candidate_key": link.row_key,
            "person_ids": person_ids,
            "conflict": False,
            "no_link": False,
            "evidence": evidence,
            "dossier": evidence.as_judge_dict(),
            "linkedin": linkedin_view(profile_row, profiles.get(link.row_key)),
            "from_connections": any(
                "linkedin_csv" in sources.get(person_id, set()) for person_id in person_ids
            ),
        })
    counts: dict[str, int] = {}
    for task in tasks:
        counts[task["parent_id"]] = counts.get(task["parent_id"], 0) + 1
    for task in tasks:
        task["conflict"] = counts[task["parent_id"]] > 1
    return tasks


def select_tasks(
    db: Db, slugs: list[str] | None, limit: int,
) -> list[dict[str, Any]]:
    tasks = build_tasks(db)
    if slugs:
        wanted = {value.lower() for value in slugs}
        tasks = [
            task for task in tasks
            if str(task.get("parent_slug") or "").lower() in wanted
        ]
    return tasks[:limit] if limit else tasks


CONNECTION_VERDICT = {
    "verdict": "confirmed", "confidence": 1.0,
    "supporting_evidence": ["LinkedIn Connections import"],
    "contradicting_evidence": [], "linkedin_plausibly_absent": False,
    "recommend_deep_research": False,
    "reason": "Ground truth: this profile came from your LinkedIn Connections import.",
}


def profile_fetch_candidates(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        task for task in tasks
        if not task.get("from_connections")
        and (task.get("linkedin") or {}).get("linkedin_url")
        and not (task.get("linkedin") or {}).get("has_profile")
    ]


def fetch_missing_profiles(
    db: Db,
    tasks: list[dict[str, Any]],
    cache_dir: Path,
    *,
    max_workers: int = 8,
) -> dict[str, int]:
    wanted = profile_fetch_candidates(tasks)
    counts = {
        "fetch_wanted": len(wanted), "fetch_ok": 0,
        "fetch_failed": 0, "fetch_skipped_no_key": 0,
    }
    if not wanted:
        return counts
    if not profile_projection.provider_key_available():
        counts["fetch_skipped_no_key"] = len(wanted)
        return counts
    targets = [{
        "public_identifier": str(task["linkedin"].get("public_identifier") or ""),
        "linkedin_url": str(task["linkedin"].get("linkedin_url") or ""),
        "candidate_key": str(task["candidate_key"]),
        "parent_id": str(task["parent_id"]),
    } for task in wanted if task.get("candidate_key") and task.get("parent_id")]

    hydrated, _ = profile_projection.hydrate_profiles(
        targets, cache_dir, db=db, max_workers=max_workers
    )
    counts["fetch_ok"], counts["fetch_failed"] = hydrated["ok"], hydrated["failed"]
    profiles = profile_projection.profile_payloads(canonical_snapshot(db))
    for task in wanted:
        row = {**task["linkedin"], "candidate_key": task.get("candidate_key") or ""}
        task["linkedin"] = linkedin_view(
            row, profiles.get(str(row.get("candidate_key") or ""))
        )
    return counts


def dry_run_estimate(
    *, db: Db, model: str, effort: str,
    slug: list[str] | None = None, limit: int = 0,
) -> dict[str, Any]:
    started = time.monotonic()
    tasks = select_tasks(db, slug, limit)
    judgeable = [
        task for task in tasks
        if not task.get("from_connections") and task["linkedin"].get("has_profile")
    ]
    misses = len(profile_fetch_candidates(tasks))
    return {
        "source": "reconcile_linkedin", "status": "dry_run",
        "profile_fetch_misses": misses, "estimated_rapidapi_credits": misses,
        "parents": len({task["parent_id"] for task in tasks}), "tasks": len(tasks),
        "judgeable": len(judgeable), "no_link": 0,
        "identity_judgeable": len(judgeable),
        "ground_truth_connections": sum(bool(task.get("from_connections")) for task in tasks),
        "conflicts": sum(bool(task.get("conflict")) for task in tasks),
        "estimated_cost_usd_low": round(len(judgeable) * 0.004, 2),
        "estimated_cost_usd_high": round(len(judgeable) * 0.02, 2),
        "model": model, "reasoning_effort": reasoning_effort(effort),
        "elapsed_ms": int((time.monotonic() - started) * 1000), "updated_at": now_iso(),
    }
