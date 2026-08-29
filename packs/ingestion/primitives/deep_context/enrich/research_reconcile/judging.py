"""Judge and project proposed LinkedIn retargets from completed research."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable

from packs.ingestion.primitives.common.paths import DEFAULT_PROFILE_CACHE_DIR
from packs.ingestion.primitives.deep_context.db import identity_queries as queries
from packs.ingestion.primitives.deep_context.db.models import (
    ApprovedState,
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
    reason: str,
    source: str,
    prior: ReviewExportRow | None,
    stored: judgment_policy.StoredJudgment | None = None,
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
        reason=reason,
        source=source,
        judge_fingerprint=fingerprint,
    )
    # Same evidence/profile/model/effort hashed to the same fingerprint last
    # time — the judge would reach the same verdict, so skip paying for it.
    # Reuse the identity stage's parser and verdict-membership policy. A judge
    # error may leave a fingerprint beside an empty/malformed payload; equality
    # alone would pin that failure forever as if it were a paid answer.
    if judgment_policy.reuses_stored_verdict(stored, fingerprint, force=False):
        return PreparedResearchProposal(proposal, None, "cached")
    prior_fingerprint = (prior.llm_judge_fingerprint or "").strip() if prior else ""
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
    # Research is parent-level: one stable handle can target several rejected
    # candidate links. Read that result once, then apply it to every target row.
    handles = {row.parent_slug for row in subset if row.parent_slug}
    results = {
        handle: (provided_results or {}).get(handle) or _research_result(db, handle=handle)
        for handle in handles
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
    stored = judgment_policy.stored_judgments(db)
    if targets:
        # Warms the profile cache for every candidate URL before judging, so the
        # loop below can prefer the fuller cached profile over the thin research
        # snippet (judge.prefer_cached_profile).
        projection.hydrate_profiles(targets, cache_dir, db=db)
    owner_block = owner_block or owner_background(db)
    profiles = projection.profile_payloads(db)
    proposals: list[RetargetProposal] = []
    pending: list[PreparedResearchProposal] = []
    cached = grandfathered = judge_errors = 0
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
            reason=result.reason,
            source=source,
            prior=prior,
            stored=stored.get(row_key),
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
            owner_block=owner_block,
            model=judge_config.model,
            effort=judge_config.effort,
            concurrency=None,
            timeout=timeout,
            max_retries=max_retries,
            on_done=heartbeat,
        )
        for item, judge_result in zip(pending, judge_results, strict=True):
            verdict: IdentityVerdict | None = judge_result.verdict
            if verdict is None:
                judge_errors += 1
                continue
            proposals.append(
                replace(
                    item.proposal,
                    judge_fingerprint=judge_result.fingerprint or item.proposal.judge_fingerprint,
                    judge_payload=verdict,
                    approved=(
                        ApprovedState.AUTO.value
                        if verdict.value == "confirmed" and verdict.confidence >= confirm_threshold
                        else ""
                    ),
                )
            )

    projected = upsert_retargets(db, proposals)
    return RetargetRunResult(
        proposed=projected,
        judge_calls=len(pending),
        cached_verdicts=cached,
        grandfathered=grandfathered,
        judge_errors=judge_errors,
    )


def _research_result(
    db: Db,
    *,
    handle: str,
) -> ResearchResult | None:
    """Read the one parent-level research result for this stable handle."""
    row = next(iter(queries.research_rows(db, handle=handle)), None)
    return ResearchResult.from_json(row.result_json) if row is not None else None
