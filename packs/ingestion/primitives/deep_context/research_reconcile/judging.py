"""Judge and project proposed LinkedIn retargets from completed research."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable

from packs.indexing.lib.openai_usage_tiers import env_or_profile_int
from packs.ingestion.primitives.common.paths import DEFAULT_PROFILE_CACHE_DIR
from packs.ingestion.primitives.deep_context.db.models import (
    IdentityOrigin,
    RESEARCH_CONFIRM_THRESHOLD,
    ReviewExportRow,
)
from packs.ingestion.primitives.deep_context.db.snapshots import (
    canonical_snapshot,
    identity_snapshot,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.view_models import EnrichmentQueueRow
from packs.ingestion.primitives.deep_context.dossier_evidence import (
    DossierEvidence,
    owner_background,
)
from packs.ingestion.primitives.deep_context import identity_evidence
from packs.ingestion.primitives.deep_context.identity_reconcile.queue import (
    identity_profile_source,
    linkedin_view,
)
from packs.ingestion.primitives.deep_context.judge_models import (
    IdentityVerdict,
    JudgeProfile,
)
from packs.ingestion.primitives.deep_context.identity_reconcile.results import (
    RetargetProposal,
    upsert_retargets,
)
from packs.ingestion.primitives.deep_context.identity_reconcile import judgment_policy
from packs.ingestion.primitives.deep_context import profile_projection
from packs.ingestion.primitives.deep_context.profile_models import ProfileTarget
from packs.ingestion.primitives.deep_context.research_reconcile.models import (
    PreparedResearchProposal,
    RetargetRunResult,
)
from packs.ingestion.primitives.deep_context.research_result import ResearchResult
from packs.ingestion.schemas.people_schema import (
    extract_public_identifier,
    normalize_linkedin_url,
)

def proposal_fingerprint(
    row_key: str,
    new_url: str,
    evidence: DossierEvidence,
    profile_view: JudgeProfile,
    owner_block: str = "",
) -> str:
    del row_key, new_url
    return identity_evidence.judgment_fingerprint(
        evidence, profile_view, IdentityOrigin.RESEARCH, owner_block
    )


def prepare_research_proposal(
    *,
    row_key: str,
    new_url: str,
    dossier: DossierEvidence,
    profile: JudgeProfile,
    name: str,
    confidence: float,
    unverified: bool,
    reason: str,
    source: str,
    prior: ReviewExportRow | None,
    owner_block: str = "",
) -> PreparedResearchProposal:
    """Apply the existing main-path cache and grandfather rules once."""
    evidence = dossier
    fingerprint = proposal_fingerprint(
        row_key, new_url, evidence, profile, owner_block
    )
    proposal = RetargetProposal(
        candidate_key=row_key,
        new_linkedin_url=new_url,
        confidence=confidence,
        reason=reason,
        source=source,
        judge_fingerprint=fingerprint,
    )
    prior_retarget = bool(prior and (prior.action or "").strip().lower() == "retarget")
    prior_fingerprint = (prior.llm_judge_fingerprint or "").strip() if prior else ""
    if prior_retarget and prior_fingerprint == fingerprint:
        return PreparedResearchProposal(proposal, None, "cached")
    if (
        prior_retarget
        and not prior_fingerprint
        and prior is not None
        and (prior.new_linkedin_url or "").strip() == normalize_linkedin_url(new_url)
    ):
        return PreparedResearchProposal(proposal, None, "grandfathered")
    task = identity_evidence.research_proposal_task(
        evidence,
        profile,
        name=name,
        confidence=confidence,
        unverified=unverified,
    )
    return PreparedResearchProposal(proposal, task, "pending")


def propose_retargets(
    subset: list[EnrichmentQueueRow] | tuple[EnrichmentQueueRow, ...],
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
) -> RetargetRunResult:
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
            candidate_key=row.row_key,
        )
        for row in subset
        if (handle := row.parent_slug)
    }
    targets = [
        ProfileTarget(
            extract_public_identifier(result.linkedin_url).lower(),
            result.linkedin_url,
            row.row_key.lower(),
            row.parent_id.lower(),
        )
        for row in subset
        if (result := results.get(row.parent_slug))
        and result.linkedin_url
        and row.row_key
        and row.parent_id
    ]
    existing = {row.key: row for row in identity.review_rows}
    if targets:
        profile_projection.hydrate_profiles(targets, cache_dir, db=db)
    graph = canonical_snapshot(db)
    owner_block = owner_block or owner_background(graph)
    profiles = profile_projection.profile_payloads(graph)
    proposals: list[RetargetProposal] = []
    pending: list[PreparedResearchProposal] = []
    cached = grandfathered = 0
    for row in subset:
        handle = row.parent_slug
        result: ResearchResult | None = results.get(handle)
        if result is None:
            continue
        new_url = result.linkedin_url
        row_key = row.row_key.lower()
        if not new_url or not row_key:
            continue
        evidence = DossierEvidence.from_parent(row.parent_id, graph)
        profile = identity_evidence.prefer_cached_profile(
            JudgeProfile.from_research(result.identity_profile()),
            linkedin_view(
                identity_profile_source(linkedin_url=new_url),
                profiles.get(row_key),
            ),
        )
        prior: ReviewExportRow | None = existing.get(row_key)
        prepared = prepare_research_proposal(
            row_key=row_key,
            new_url=new_url,
            dossier=evidence,
            profile=profile,
            name=row.name,
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
            verdict: IdentityVerdict = (
                result.verdict or IdentityVerdict.from_payload({})
            )
            rejection = judgment_policy.research_reject_fields(
                verdict, confirm_threshold
            )
            proposals.append(replace(
                item.proposal,
                judge_fingerprint=result.fingerprint or item.proposal.judge_fingerprint,
                judge_payload=verdict,
                llm_reject=rejection.llm_reject,
                llm_reject_confidence=rejection.llm_reject_confidence,
                llm_reject_reason=rejection.llm_reject_reason,
                confidence=float(rejection.confidence or item.proposal.confidence),
                has_reject_fields=True,
            ))

    projected = upsert_retargets(db, proposals)
    return RetargetRunResult(
        path=projected.path,
        proposed=projected.proposed,
        preserved_user_rows=projected.preserved_user_rows,
        total_rows=projected.total_rows,
        judge_calls=len(pending),
        cached_verdicts=cached,
        grandfathered=grandfathered,
    )
