"""Judge and project proposed LinkedIn retargets from completed research."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable

from packs.ingestion.primitives.common.paths import DEFAULT_PROFILE_CACHE_DIR
from packs.ingestion.primitives.deep_context.db import identity_queries as queries
from packs.ingestion.primitives.deep_context.db.models import (
    IdentityOrigin,
    RESEARCH_CONFIRM_THRESHOLD,
    ReviewExportRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.view_models import EnrichmentQueueRow
from packs.ingestion.primitives.deep_context.shared.openai_responses import OpenAIResponsesConfig
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import (
    DossierEvidence,
    owner_background,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile import judge
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.models import (
    IdentityProfileSource,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.queue import (
    linkedin_view,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import (
    IdentityVerdict,
    JudgeProfile,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.results import (
    RetargetProposal,
    upsert_retargets,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile import judgment_policy
from packs.ingestion.primitives.deep_context.enrich.profiles import projection
from packs.ingestion.primitives.deep_context.enrich.profiles.models import ProfileTarget
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.models import (
    PreparedResearchProposal,
    RetargetRunResult,
)
from packs.ingestion.primitives.deep_context.enrich.parallel_research.result import ResearchResult
from packs.ingestion.schemas.people_schema import (
    extract_public_identifier,
    normalize_linkedin_url,
)


def proposal_fingerprint(
    evidence: DossierEvidence,
    profile_view: JudgeProfile,
    owner_block: str = "",
    *,
    model: str,
    effort: str,
) -> str:
    return judge.judgment_fingerprint(
        evidence, profile_view, IdentityOrigin.RESEARCH, owner_block, model=model, effort=effort
    )


def prepare_research_proposal(
    *,
    row_key: str,
    new_url: str,
    dossier: DossierEvidence,
    profile: JudgeProfile,
    name: str,
    confidence: float,
    reason: str,
    source: str,
    prior: ReviewExportRow | None,
    model: str,
    effort: str,
    owner_block: str = "",
) -> PreparedResearchProposal:
    """Apply the existing main-path cache and grandfather rules once."""
    evidence = dossier
    fingerprint = proposal_fingerprint(evidence, profile, owner_block, model=model, effort=effort)
    proposal = RetargetProposal(
        candidate_key=row_key,
        new_linkedin_url=new_url,
        confidence=confidence,
        reason=reason,
        source=source,
        judge_fingerprint=fingerprint,
    )
    prior_fingerprint = (prior.llm_judge_fingerprint or "").strip() if prior else ""
    # Same evidence/profile/model/effort hashed to the same fingerprint last
    # time — the judge would reach the same verdict, so skip paying for it.
    # Verdict DIRECTION is deliberately not part of this test: a stored
    # rejection is bought and paid for exactly like a stored acceptance. This
    # used to also require action == "retarget", which only a cleared proposal
    # ever reaches — so every REJECTED proposal re-entered the paid queue on
    # byte-identical input, every pass, forever. The fingerprint hashes
    # IdentityOrigin, so an attached-identity verdict can never collide with a
    # research proposal's key. (fingerprint is a sha256 hexdigest, never
    # empty, so equality alone proves the prior row had one.)
    if prior_fingerprint == fingerprint:
        return PreparedResearchProposal(proposal, None, "cached")
    if (
        prior is not None
        and not prior_fingerprint
        and (prior.action or "").strip().lower() == "retarget"
        and (prior.new_linkedin_url or "").strip() == normalize_linkedin_url(new_url)
    ):
        # No stored fingerprint (row predates judgment_fingerprint existing) but the
        # URL still matches what's proposed now — trust the legacy retarget rather
        # than re-judging it.
        return PreparedResearchProposal(proposal, None, "grandfathered")
    task = judge.research_proposal_task(
        evidence,
        profile,
        name=name,
    )
    return PreparedResearchProposal(proposal, task, "pending")


def propose_retargets(
    subset: list[EnrichmentQueueRow] | tuple[EnrichmentQueueRow, ...],
    *,
    db: Db,
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
    cache_dir = Path(profile_cache_dir) if profile_cache_dir is not None else DEFAULT_PROFILE_CACHE_DIR
    # Resolve model/effort ONCE and feed the SAME strings to the proposal
    # fingerprints and the judge. judge_batch re-resolves internally (the
    # POWERPACKS_DEEP_CONTEXT_REASONING_EFFORT override applies there), so
    # hashing the raw caller values here would key the cache with an effort the
    # judge never ran at — with the override set, every stored verdict would
    # miss and re-bill on every pass. Re-resolving resolved values is a no-op,
    # so passing config values back into judge_batch changes nothing else.
    judge_config = OpenAIResponsesConfig.resolve(
        model=model or "", effort=effort, concurrency=None, timeout=timeout, max_retries=max_retries,
    )
    # One research result per handle (last row wins on a handle collision); the loop
    # below applies that same result to every row in subset sharing the handle, so
    # several identity-link rows for one parent can each get proposed against it.
    results = {
        handle: (provided_results or {}).get(handle) or _research_result(db, handle=handle, candidate_key=row.row_key)
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
        if (result := results.get(row.parent_slug)) and result.linkedin_url and row.row_key and row.parent_id
    ]
    existing = {row.key: row for row in queries.review_rows(db)}
    if targets:
        # Warms the profile cache for every candidate URL before judging, so the
        # loop below can prefer the fuller cached profile over the thin research
        # snippet (judge.prefer_cached_profile).
        projection.hydrate_profiles(targets, cache_dir, db=db)
    owner_block = owner_block or owner_background(db)
    profiles = projection.profile_payloads(db)
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
        evidence = DossierEvidence.from_db(db, (row.parent_id,))
        profile = judge.prefer_cached_profile(
            result.identity_profile(),
            linkedin_view(
                IdentityProfileSource(linkedin_url=new_url),
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
            reason=result.reason,
            source=source,
            prior=prior,
            model=judge_config.model,
            effort=judge_config.effort,
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
        # Every "pending" proposal carries a task (the sole producer sets it
        # unconditionally); no filter here, so the strict zip below can never
        # silently pair a verdict with the wrong proposal.
        judge_results = judge.judge_batch(
            [item.task for item in pending],
            use_llm=True,
            owner_block=owner_block,
            model=judge_config.model,
            effort=judge_config.effort,
            concurrency=None,
            timeout=timeout,
            max_retries=max_retries,
            on_done=heartbeat,
        )
        for item, judge_result in zip(pending, judge_results, strict=True):
            verdict: IdentityVerdict = judge_result.verdict or IdentityVerdict.from_payload({})
            # confirm_threshold (0.80 research_confirm by default) decides the outcome:
            # a "confirmed" verdict at/above it clears llm_reject, which upsert_retargets
            # reads as auto-approved — the retarget projects straight into the identity
            # graph. Anything else sets llm_reject="yes": the proposal is still stored
            # (has_reject_fields=True below) but stays unapproved for human review
            # instead of silently retargeting on a shaky match.
            rejection = judgment_policy.research_reject_fields(verdict, confirm_threshold)
            proposals.append(
                replace(
                    item.proposal,
                    judge_fingerprint=judge_result.fingerprint or item.proposal.judge_fingerprint,
                    judge_payload=verdict,
                    llm_reject=rejection.llm_reject,
                    llm_reject_confidence=rejection.llm_reject_confidence,
                    llm_reject_reason=rejection.llm_reject_reason,
                    confidence=float(rejection.confidence or item.proposal.confidence),
                    has_reject_fields=True,
                )
            )

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


def _research_result(
    db: Db,
    *,
    handle: str,
    candidate_key: str | None,
) -> ResearchResult | None:
    """Read the same handle/candidate result without loading unrelated research rows."""
    wanted = (candidate_key or "").strip().lower()
    row = next(
        (
            item
            for item in queries.research_rows(db, handle=handle)
            if not wanted or str(item.candidate_key or "").lower() == wanted
        ),
        None,
    )
    return ResearchResult.from_json(row.result_json) if row is not None else None
