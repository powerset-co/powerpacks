"""Judge and project proposed LinkedIn retargets from completed research."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from packs.indexing.lib.openai_usage_tiers import env_or_profile_int
from packs.ingestion.primitives.common.paths import DEFAULT_PROFILE_CACHE_DIR
from packs.ingestion.primitives.deep_context.db.models import (
    IdentityOrigin,
    RESEARCH_CONFIRM_THRESHOLD,
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
from packs.ingestion.primitives.deep_context import identity_evidence
from packs.ingestion.primitives.deep_context.identity_reconcile.queue import (
    linkedin_view,
)
from packs.ingestion.primitives.deep_context.identity_reconcile import judgment_policy
from packs.ingestion.primitives.deep_context import profile_projection
from packs.ingestion.primitives.deep_context.identity_reconcile.results import (
    upsert_retargets,
)
from packs.ingestion.primitives.deep_context.research_result import ResearchResult
from packs.ingestion.schemas.people_schema import (
    extract_public_identifier,
    normalize_linkedin_url,
)


@dataclass(frozen=True)
class PreparedResearchProposal:
    """One main-path proposal after fingerprint/cache classification."""

    proposal: dict[str, Any]
    task: dict[str, Any] | None
    disposition: str


def proposal_fingerprint(
    old_pub: str,
    new_url: str,
    evidence: DossierEvidence,
    profile_view: dict[str, Any],
    owner_block: str = "",
) -> str:
    del old_pub, new_url
    return identity_evidence.judgment_fingerprint(
        evidence, profile_view, IdentityOrigin.RESEARCH, owner_block
    )


def prepare_research_proposal(
    *,
    old_pub: str,
    new_url: str,
    old_url: str,
    dossier: DossierEvidence | dict[str, Any],
    profile: dict[str, Any],
    name: str,
    match_emails: list[str],
    match_phones: list[str],
    person_id: str,
    confidence: float,
    unverified: bool,
    reason: str,
    source: str,
    prior: dict[str, Any],
    owner_block: str = "",
) -> PreparedResearchProposal:
    """Apply the existing main-path cache and grandfather rules once."""
    evidence = (
        dossier
        if isinstance(dossier, DossierEvidence)
        else DossierEvidence.from_judge_dict(dossier, name=name)
    )
    fingerprint = proposal_fingerprint(
        old_pub, new_url, evidence, profile, owner_block
    )
    proposal = {
        "old_public_identifier": old_pub,
        "new_linkedin_url": new_url,
        "linkedin_url": old_url,
        "match_emails": match_emails,
        "match_phones": match_phones,
        "person_id": person_id,
        "confidence": confidence,
        "reason": reason,
        "source": source,
        "judge_fingerprint": fingerprint,
    }
    prior_retarget = (prior.get("action") or "").strip().lower() == "retarget"
    prior_fingerprint = (prior.get("llm_judge_fingerprint") or "").strip()
    if prior_retarget and prior_fingerprint == fingerprint:
        return PreparedResearchProposal(proposal, None, "cached")
    if (
        prior_retarget
        and not prior_fingerprint
        and (prior.get("new_linkedin_url") or "").strip()
        == normalize_linkedin_url(new_url)
    ):
        return PreparedResearchProposal(proposal, None, "grandfathered")
    task = identity_evidence.research_proposal_task(
        evidence,
        profile,
        name=name,
        match_emails=match_emails,
        match_phones=match_phones,
        confidence=confidence,
        unverified=unverified,
    )
    return PreparedResearchProposal(proposal, task, "pending")


def propose_retargets(
    subset: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    db: Db,
    use_llm: bool = False,
    owner_block: str = "",
    model: str = "",
    effort: str = "medium",
    confirm_threshold: float = RESEARCH_CONFIRM_THRESHOLD,
    timeout: int = 120,
    max_retries: int = 6,
    heartbeat: Callable[[int, int], None] | None = None,
    profile_cache_dir: Path | None = None,
    source: str = "deep-research",
    provided_results: dict[str, ResearchResult] | None = None,
) -> dict[str, Any]:
    """Judge projected research and store sticky retarget proposals."""
    cache_dir = (
        Path(profile_cache_dir)
        if profile_cache_dir is not None
        else DEFAULT_PROFILE_CACHE_DIR
    )
    identity = identity_snapshot(db)
    results = {
        handle: (provided_results or {}).get(handle) or ResearchResult.from_snapshot(
            identity,
            handle=handle,
            candidate_key=str(row.get("candidate_key") or ""),
        )
        for row in subset
        if (handle := str(row.get("parent_slug") or ""))
    }
    targets = [
        {
            "public_identifier": extract_public_identifier(result.linkedin_url).lower(),
            "linkedin_url": result.linkedin_url,
            "candidate_key": str(row.get("candidate_key") or "").lower(),
            "parent_id": str(row.get("parent_id") or "").lower(),
        }
        for row in subset
        if (result := results.get(str(row.get("parent_slug") or "")))
        and result.linkedin_url
        and row.get("candidate_key")
        and row.get("parent_id")
    ]
    if targets:
        profile_projection.hydrate_profiles(targets, cache_dir, db=db)
    graph = canonical_snapshot(db)
    owner_block = owner_block or owner_background(graph)
    profiles = profile_projection.profile_payloads(graph)
    existing = identity_snapshot(db).link_decisions
    proposals: list[dict[str, Any]] = []
    pending: list[PreparedResearchProposal] = []
    cached = grandfathered = 0
    for row in subset:
        handle = row.get("parent_slug", "")
        result = results.get(handle)
        if result is None:
            continue
        new_url = result.linkedin_url
        old_pub = (
            row.get("candidate_key")
            or extract_public_identifier(
                (row.get("linkedin") or {}).get("linkedin_url", "")
            )
        ).lower()
        if not new_url or not old_pub:
            continue
        person_ids = row.get("person_ids") or []
        evidence = DossierEvidence.from_parent(str(row.get("parent_id") or ""), graph)
        profile = identity_evidence.prefer_cached_profile(
            result.identity_profile(),
            linkedin_view(
                {"linkedin_url": new_url},
                profiles.get(old_pub),
            ),
        )
        prior = existing.get(old_pub) or {}
        prepared = prepare_research_proposal(
            old_pub=old_pub,
            new_url=new_url,
            old_url=(row.get("linkedin") or {}).get("linkedin_url", ""),
            dossier=evidence,
            profile=profile,
            name=row.get("name", ""),
            match_emails=row.get("match_emails") or [],
            match_phones=row.get("match_phones") or [],
            person_id=(person_ids or [""])[0],
            confidence=result.confidence,
            unverified=result.unverified,
            reason=result.reason,
            source=source,
            prior=prior,
            owner_block=owner_block,
        )
        if prepared.disposition == "cached":
            cached += 1
            continue
        if prepared.disposition == "grandfathered":
            grandfathered += 1
            proposals.append(prepared.proposal)
            continue
        pending.append(prepared)

    if pending:
        if heartbeat:
            heartbeat(0, len(pending))
        concurrency = env_or_profile_int(
            "POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency",
            fallback=identity_evidence.DEFAULT_IDENTITY_CONCURRENCY,
        )
        results = identity_evidence.judge_batch(
            [item.task for item in pending if item.task is not None],
            use_llm=use_llm, owner_block=owner_block, model=model or "", effort=effort,
            concurrency=concurrency, timeout=timeout, max_retries=max_retries,
            on_done=heartbeat,
        )
        for item, result in zip(pending, results):
            proposals.append({
                **item.proposal,
                "judge_fingerprint": str(
                    result.get("fingerprint")
                    or item.proposal.get("judge_fingerprint")
                    or ""
                ),
                "judge_payload": result.get("verdict") or {},
                **judgment_policy.research_reject_fields(
                    result.get("verdict") or {}, confirm_threshold
                ),
            })

    projected = upsert_retargets(db, proposals)
    projected.update(
        {
            "judge_calls": len(pending),
            "cached_verdicts": cached,
            "grandfathered": grandfathered,
        }
    )
    return projected
