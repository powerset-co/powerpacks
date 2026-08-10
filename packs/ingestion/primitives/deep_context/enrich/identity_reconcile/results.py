"""Write verdict receipts and project/read canonical identity decisions."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.models import (
    ApprovedState,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.queue import build_tasks
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile import judgment_policy
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judgment_policy import stored_judgments
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.models import (
    IdentityProjectionResult,
)
from packs.ingestion.primitives.deep_context.enrich.judge_models import (
    IdentityTask,
    IdentityVerdict,
    JudgeProfile,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.settlement import (
    MachineIdentitySettlement,
    settle_machine_identities,
)
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url


@dataclass(frozen=True)
class RetargetProposal:
    """One typed research conclusion ready for canonical identity settlement."""

    candidate_key: str
    new_linkedin_url: str
    confidence: float = 0.0
    reason: str = ""
    source: str = "deep-research"
    judge_fingerprint: str = ""
    new_public_identifier: str = ""
    approved: str = ""
    judge_payload: IdentityVerdict | None = None
    llm_reject: str | None = None
    llm_reject_confidence: str = ""
    llm_reject_reason: str = ""
    # Distinguishes "no reject-check ran" (leave the stored reject columns
    # alone) from an explicit clean pass (write llm_reject=None); see
    # MachineIdentitySettlement.projection, which reads this to decide whether
    # to touch the reject columns at all.
    has_reject_fields: bool = False


@dataclass(frozen=True)
class RetargetProjectionResult:
    path: str
    proposed: int
    preserved_user_rows: int
    total_rows: int


def _judgment_payload_json(payload: IdentityVerdict) -> str:
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
        # Translation, not a decision: the confirm/detach/review -> settlement
        # mapping lives once, in judgment_policy.settled_machine_action.
        machine_action, approved = judgment_policy.settled_machine_action(task.action, verdict)
        settlements.append(
            MachineIdentitySettlement(
                key=key,
                judgment_fingerprint=task.judgment_fingerprint,
                judgment_payload_json=_judgment_payload_json(verdict or IdentityVerdict.from_payload({})),
                machine_action=machine_action,
                machine_approved=approved,
                machine_confidence=verdict.confidence if verdict else 0.0,
                machine_reason=verdict.reason if verdict else "",
                machine_judgment=(verdict.value if verdict and verdict.value else None),
                # True only for a threshold-cleared auto-detach — not a
                # review-pending "detach" hint — so downstream callers can tell
                # trusted machine removal apart from a mere suggestion.
                authoritative_detach=(machine_action == "detach" and approved == "auto"),
                judgment_artifact_path=str(artifact_path) if artifact_path else None,
                source=source.value,
            )
        )
    projected, preserved, total_rows = settle_machine_identities(db, settlements)
    accepted = [settlement for settlement in settlements if settlement.key in projected]
    verified = sum(row.machine_action == "verify" and row.machine_approved == "auto" for row in accepted)
    detached = sum(row.machine_action == "detach" and row.machine_approved == "auto" for row in accepted)
    pending = sum(row.machine_approved is None for row in accepted)
    return IdentityProjectionResult(
        path=str(db.db_path),
        detached=detached,
        verified=verified,
        pending=pending,
        preserved_user_rows=len(preserved),
        total_rows=total_rows,
    )


def upsert_retargets(
    db: Db,
    proposals: list[RetargetProposal],
) -> RetargetProjectionResult:
    settlements = []
    proposed = 0
    for proposal in proposals:
        candidate_key = proposal.candidate_key.lower()
        new_url = normalize_linkedin_url(proposal.new_linkedin_url)
        if not candidate_key or not new_url:
            continue
        approved = proposal.approved.lower() or None
        if approved is None and judgment_policy.auto_approve_clean_reject(
            proposal.has_reject_fields, proposal.llm_reject
        ):
            # Caller left approval unset but a reject-check ran and found
            # nothing: a clean reject-check is treated as an implicit auto-approve.
            approved = ApprovedState.AUTO.value
        # Synthesized only when the caller supplies no real judge output —
        # llm_reject presence alone decides confirmed vs needs_review here.
        payload = proposal.judge_payload or IdentityVerdict.from_payload(
            {
                "verdict": "confirmed" if not proposal.llm_reject else "needs_review",
                "confidence": proposal.confidence,
                "reason": proposal.reason,
            }
        )
        settlements.append(
            MachineIdentitySettlement(
                key=candidate_key,
                judgment_fingerprint=proposal.judge_fingerprint,
                judgment_payload_json=_judgment_payload_json(payload),
                machine_action="retarget",
                machine_approved=approved,
                machine_confidence=proposal.confidence,
                machine_reason=proposal.reason,
                machine_judgment=None,
                machine_proposed_url=new_url,
                machine_proposed_public_identifier=str(
                    proposal.new_public_identifier or extract_public_identifier(new_url)
                ).lower(),
                paid_profile=True,
                source=proposal.source or WriterSource.DEEP_RESEARCH.value,
                machine_reject=(proposal.llm_reject or None if proposal.has_reject_fields else None),
                machine_reject_confidence=(
                    float(proposal.llm_reject_confidence or 0) if proposal.has_reject_fields else 0.0
                ),
                machine_reject_reason=(proposal.llm_reject_reason or None if proposal.has_reject_fields else None),
                has_reject_fields=proposal.has_reject_fields,
            )
        )
        proposed += 1
    projected, preserved, total_rows = settle_machine_identities(db, settlements)
    return RetargetProjectionResult(
        path=str(db.db_path),
        proposed=min(proposed, len(projected)),
        preserved_user_rows=len(preserved),
        total_rows=total_rows,
    )


def write_verdicts(path: Path, tasks: list[IdentityTask]) -> None:
    """Always-written JSONL receipt — the audit trail survives ``--no-overrides``,
    which only skips the DB projection below."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for task in tasks:
            stream.write(
                json.dumps(
                    task.as_artifact_dict(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )


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


def merge_subset_tasks(db: Db, fresh: list[IdentityTask]) -> list[IdentityTask]:
    """Fill in every parent this run didn't re-judge from stored verdicts, so a
    ``--slug``/``--limit`` run still settles and reports on the whole graph."""
    replaced = {task.parent_id or task.parent_slug for task in fresh}
    prior = [task for task in load_tasks_from_store(db) if (task.parent_id or task.parent_slug) not in replaced]
    return prior + fresh
