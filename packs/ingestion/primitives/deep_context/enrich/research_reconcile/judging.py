"""Judge and project proposed LinkedIn retargets from completed research."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Callable

from packs.ingestion.primitives.common.paths import DEFAULT_PROFILE_CACHE_DIR
from packs.ingestion.primitives.enrich.rapidapi_client import PROFILE_ERROR
from packs.ingestion.primitives.deep_context.db import context_queries, identity_queries as queries
from packs.ingestion.primitives.deep_context.db.identity_views import judge_candidates
from packs.ingestion.primitives.deep_context.db.models import (
    ApprovedState,
    IdentityOrigin,
    LinkSnapshotRow,
    RESEARCH_CONFIRM_THRESHOLD,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.view_models import EnrichmentQueueRow
from packs.ingestion.primitives.deep_context.shared.openai_responses import OpenAIResponsesConfig
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import (
    DossierEvidence, source_evidence,
    owner_background,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile import judge, jev_judge
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.models import (
    IdentityProfileSource,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.queue import (
    linkedin_view,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import (
    StoredJudgment,
    IdentityTask,
    IdentityVerdict,
    JudgeProfile,
    IdentityJudgeResult,
    IdentityUsage,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.results import (
    RetargetProposal,
    upsert_retargets,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile import judgment_policy
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.settlement import (
    MachineIdentitySettlement,
    settle_machine_identities,
)
from packs.ingestion.primitives.deep_context.enrich.profiles import projection
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.selection import RESEARCH_BATCH
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
    stored: StoredJudgment | None = None,
    model: str,
    effort: str,
    owner_block: str = "",
    confirm_threshold: float = RESEARCH_CONFIRM_THRESHOLD,
) -> PreparedResearchProposal:
    """Reuse only the exact current judge request with a valid verdict."""
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
        return PreparedResearchProposal(replace(proposal, judge_payload=stored.verdict,
            approved=ApprovedState.AUTO.value if stored.verdict.value == "confirmed" and stored.verdict.confidence >= confirm_threshold else ""), None, "cached")
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
    stored = queries.stored_judgments(db)
    if targets:
        # Warms the profile cache for every candidate URL before judging, so the
        # loop below can prefer the fuller cached profile over the thin research
        # snippet (judge.prefer_cached_profile).
        projection.hydrate_profiles(targets, cache_dir, db=db)
    owner_block = owner_block or owner_background(db)
    profiles = projection.profile_payloads(db)
    proposals: list[RetargetProposal] = []
    pending: list[PreparedResearchProposal] = []
    cached = judge_errors = 0
    for row in subset:
        handle = row.parent_slug
        result: ResearchResult | None = results.get(handle)
        if result is None:
            continue
        new_url = result.linkedin_url
        row_key = row.row_key.lower()
        if not new_url or not row_key:
            continue
        evidence = source_evidence(db, row.parent_id, DossierEvidence.from_db(db, (row.parent_id,)))
        profile = judge.prefer_cached_profile(
            result.identity_profile(),
            linkedin_view(
                IdentityProfileSource(linkedin_url=new_url),
                profiles.get(row_key),
            ),
        )
        prepared = prepare_research_proposal(
            row_key=row_key,
            new_url=new_url,
            dossier=evidence,
            profile=profile,
            reason=result.reason,
            source=source,
            stored=stored.get(row_key),
            model=judge_config.model,
            effort=judge_config.effort,
            owner_block=owner_block,
            confirm_threshold=confirm_threshold,
        )
        if prepared.disposition == "cached":
            cached += 1
            proposals.append(prepared.proposal)
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
        grandfathered=0,
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


def mapped_identity_tasks(db: Db) -> list[tuple[LinkSnapshotRow, IdentityTask, tuple[str, ...], IdentityJudgeResult | None]]:
    """Prepare current requests once for both the spend estimate and execution."""
    candidates = judge_candidates(db)
    if not candidates:
        return []
    known_urls = queries.imported_linkedin_urls(db, tuple({row.parent_id for row in candidates}))
    profiles = projection.profile_payloads(db)
    research = {row.candidate_key: ResearchResult.from_json(row.result_json)
                for row in queries.research_rows(db) if row.candidate_key}
    evidence_by_parent: dict[str, DossierEvidence] = {}
    parent_ids = sorted({row.parent_id for row in candidates})
    for start in range(0, len(parent_ids), RESEARCH_BATCH):
        batch = parent_ids[start:start + RESEARCH_BATCH]
        evidence_rows = context_queries.dossier_evidence_rows(db, batch)
        evidence_by_parent.update(
            (parent_id, source_evidence(db, parent_id, DossierEvidence.from_rows((parent_id,), evidence_rows)))
            for parent_id in batch
        )
        del evidence_rows
    prepared = []
    stored = queries.stored_judgments(db)
    for row in candidates:
        projected = profiles.get(row.row_key)
        if projected is not None and projected.state == PROFILE_ERROR:
            continue
        result = research.get(row.row_key)
        url = row.machine_proposed_url or row.linkedin_url or (result.linkedin_url if result else "")
        if not url:
            continue
        origin = IdentityOrigin.RESEARCH if result and normalize_linkedin_url(result.linkedin_url) == normalize_linkedin_url(url) else IdentityOrigin.ATTACHED
        source = IdentityProfileSource(
            public_identifier=extract_public_identifier(url).lower(),
            linkedin_url=url,
            display_name=row.display_name or "",
        )
        profile = linkedin_view(source, profiles.get(row.row_key))
        if result and origin == IdentityOrigin.RESEARCH:
            profile = judge.prefer_cached_profile(result.identity_profile(), profile)
        profile = replace(profile, linkedin_url=normalize_linkedin_url(profile.linkedin_url))
        evidence = evidence_by_parent[row.parent_id]
        task = judge.research_proposal_task(evidence, profile) if origin == IdentityOrigin.RESEARCH else judge.IdentityTask(evidence, profile, origin)
        fingerprint = jev_judge.judgment_fingerprint(task, known_urls.get(row.parent_id, ()))
        previous = stored.get(row.row_key)
        outcome = (IdentityJudgeResult(previous.verdict, IdentityUsage(), "", fingerprint)
                   if judgment_policy.reuses_stored_verdict(previous, fingerprint, force=False) else None)
        prepared.append((row, task, known_urls.get(row.parent_id, ()), outcome))
    return prepared


def judge_mapped_candidates(
    db: Db,
    *,
    heartbeat: Callable[[int, int], None] | None = None,
) -> RetargetRunResult:
    """Reuse exact current verdicts and settle every mapped machine candidate."""
    prepared = mapped_identity_tasks(db)
    pending = [item for item in prepared if item[-1] is None]
    cached = [item for item in prepared if item[-1] is not None]
    results = jev_judge.judge_batch(
        [task for _, task, *_ in pending], imported_urls=[urls for _, _, urls, *_ in pending],
        output_dir=db.db_path.parent / "reconcile" / "identity", on_done=heartbeat,
    ) if pending else []
    settlements = []
    errors = 0
    completed = [(*item[:-1], outcome) for item, outcome in zip(pending, results, strict=True)]
    for row, task, _, outcome in (*completed, *cached):
        url, origin = task.linkedin.linkedin_url, task.origin
        verdict = outcome.verdict
        if verdict is None:
            errors += 1
            continue
        confirmed = verdict.value == "confirmed"
        settlements.append(MachineIdentitySettlement(
            key=row.row_key,
            judgment_fingerprint=outcome.fingerprint,
            judgment_payload_json=json.dumps(verdict.as_dict()),
            machine_action="retarget" if origin == IdentityOrigin.RESEARCH else "verify",
            machine_approved=ApprovedState.AUTO.value if confirmed else None,
            machine_confidence=verdict.confidence,
            machine_reason=verdict.reason,
            machine_judgment=verdict.value,
            machine_proposed_url=normalize_linkedin_url(url) if origin == IdentityOrigin.RESEARCH else None,
            machine_proposed_public_identifier=extract_public_identifier(url).lower() if origin == IdentityOrigin.RESEARCH else None,
            paid_profile=row.paid_profile or origin == IdentityOrigin.RESEARCH,
            source=(WriterSource.DEEP_RESEARCH.value if origin == IdentityOrigin.RESEARCH
                    else WriterSource.RECONCILE.value),
        ))
    projected = settle_machine_identities(db, settlements)
    return RetargetRunResult(len(projected), len(pending), len(cached), 0, errors)
