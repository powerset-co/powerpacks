"""Project and read canonical identity decisions."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.models import (
    ApprovedState,
    ReviewAction,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.queue import build_tasks
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile import judgment_policy
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judgment_policy import stored_judgments
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.models import (
    IdentityProjectionResult,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import (
    IdentityTask,
    IdentityVerdict,
    JudgeProfile,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.settlement import (
    MachineIdentitySettlement,
    settle_machine_identities,
)
from packs.ingestion.primitives.deep_context.shared.coerce import text
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url


@dataclass(frozen=True)
class RetargetProposal:
    """One typed research conclusion ready for canonical identity settlement."""

    candidate_key: str
    new_linkedin_url: str
    reason: str = ""
    source: str = "deep-research"
    judge_fingerprint: str = ""
    new_public_identifier: str = ""
    approved: str = ""
    judge_payload: IdentityVerdict | None = None


def _judgment_payload_json(payload: IdentityVerdict | None) -> str | None:
    if payload is None:
        return None
    return json.dumps(
        payload.as_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def write_overrides(
    db: Db,
    tasks: list[IdentityTask],
    *,
    artifact_path: Path | None = None,
    source: WriterSource = WriterSource.RECONCILE,
) -> IdentityProjectionResult:
    settlements = []
    for task in tasks:
        key = task.candidate_key.lower()
        if not key:
            continue
        verdict = task.verdict
        rule = task.rule
        machine_action = (
            rule.action.value if rule else task.action or ReviewAction.REVIEW.value
        )
        approved = (
            ApprovedState.AUTO.value
            if machine_action in {ReviewAction.VERIFY.value, ReviewAction.DETACH.value}
            else None
        )
        settlements.append(
            MachineIdentitySettlement(
                key=key,
                judgment_fingerprint=task.judgment_fingerprint,
                judgment_payload_json=_judgment_payload_json(verdict),
                machine_action=machine_action,
                machine_approved=approved,
                machine_confidence=verdict.confidence if verdict else None,
                machine_reason=verdict.reason if verdict else rule.reason if rule else "",
                machine_judgment=(verdict.value if verdict and verdict.value else None),
                # True only for a threshold-cleared auto-detach — not a
                # review-pending "detach" hint — so downstream callers can tell
                # trusted machine removal apart from a mere suggestion.
                authoritative_detach=(
                    machine_action == ReviewAction.DETACH.value
                    and approved == ApprovedState.AUTO.value
                ),
                judgment_artifact_path=text(artifact_path),
                source=source.value,
            )
        )
    projected, preserved, total_rows = settle_machine_identities(db, settlements)
    outcomes = Counter(
        settlement.outcome for settlement in settlements if settlement.key in projected
    )
    return IdentityProjectionResult(
        path=str(db.db_path),
        detached=outcomes["detach_auto"],
        verified=outcomes["verify_auto"],
        pending=outcomes["pending"],
        preserved_user_rows=len(preserved),
        total_rows=total_rows,
    )


@dataclass(frozen=True)
class Settled:
    """One settlement pass: the stamped tasks, the bars applied, the write tally."""

    tasks: tuple[IdentityTask, ...]
    thresholds: judgment_policy.ResolvedThresholds
    overrides: IdentityProjectionResult


def settle(
    db: Db,
    tasks: list[IdentityTask],
    *,
    confirm: float | None = None,
    detach: float | None = None,
    artifact_path: Path | None = None,
    source: WriterSource = WriterSource.RECONCILE,
) -> Settled:
    """THE judge-path settlement door: decide → stamp → write → tally.

    Every path whose verdicts came from the judge goes through here, so the
    decide step and the action-stamping exist once. ``confirm``/``detach``
    of None take the origin defaults (see resolve_thresholds); the exact bars
    applied ride back on ``.thresholds`` for callers that gate follow-up work
    (deep_research_eligible).

    healing.terminate deliberately calls write_overrides directly because its
    dead-link and synthetic rules must not enter sibling arbitration.
    """
    decided = judgment_policy.decide_actions(tasks, confirm, detach)
    stamped = [
        replace(task, action=action.action, via=action.via)
        for task, action in zip(tasks, decided.actions, strict=True)
    ]
    overrides = write_overrides(
        db,
        stamped,
        artifact_path=artifact_path,
        source=source,
    )
    return Settled(tuple(stamped), decided.thresholds, overrides)


def upsert_retargets(
    db: Db,
    proposals: list[RetargetProposal],
) -> int:
    settlements = []
    proposed = 0
    for proposal in proposals:
        candidate_key = proposal.candidate_key.lower()
        new_url = normalize_linkedin_url(proposal.new_linkedin_url)
        if not candidate_key or not new_url:
            continue
        approved = proposal.approved.lower() or None
        payload = proposal.judge_payload
        settlements.append(
            MachineIdentitySettlement(
                key=candidate_key,
                judgment_fingerprint=proposal.judge_fingerprint,
                judgment_payload_json=_judgment_payload_json(payload),
                machine_action="retarget",
                machine_approved=approved,
                machine_confidence=payload.confidence if payload else None,
                machine_reason=payload.reason if payload else proposal.reason,
                machine_judgment=payload.value if payload else None,
                machine_proposed_url=new_url,
                machine_proposed_public_identifier=str(
                    proposal.new_public_identifier or extract_public_identifier(new_url)
                ).lower(),
                paid_profile=True,
                source=proposal.source or WriterSource.DEEP_RESEARCH.value,
            )
        )
        proposed += 1
    projected, _, _ = settle_machine_identities(db, settlements)
    return min(proposed, len(projected))


def load_tasks_from_store(db: Db) -> list[IdentityTask]:
    """Rebuild tasks from persisted verdicts for ``reapply``."""
    verdicts = stored_judgments(db)
    return [
        replace(
            task,
            linkedin=JudgeProfile.from_payload(
                {
                    "public_identifier": task.linkedin.public_identifier,
                    "linkedin_url": task.linkedin.linkedin_url,
                }
            ),
            verdict=verdicts[task.candidate_key].verdict,
            judgment_fingerprint=verdicts[task.candidate_key].fingerprint,
            error="",
        )
        for task in build_tasks(db)
        if task.candidate_key in verdicts
    ]
