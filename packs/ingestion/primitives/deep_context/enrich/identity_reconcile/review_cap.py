"""Finish unresolved identities and reserve at most 100 useful parent questions."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from typing import Literal

from packs.ingestion.primitives.deep_context.db.identity_queries import links, protected_parent_ids
from packs.ingestion.primitives.deep_context.db.identity_views import pending_parent_ids
from packs.ingestion.primitives.deep_context.db.models import WriterSource
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.settlement import (
    MachineIdentitySettlement, settle_machine_identities,
)

REVIEW_LIMIT = 100


@dataclass(frozen=True)
class RelationshipDecision:
    parent_id: str
    decision: Literal["keep", "exclude"]
    reason: str
    useful_answerable_question: bool
    human_question: str
    priority: int
    fingerprint: str
    message_count: int = 0


def cache_relationship_judgment(db: Db, decision: RelationshipDecision) -> None:
    """Checkpoint a paid relationship judgment without deciding any identity."""
    settlements = []
    for row in links(db, parent_id=decision.parent_id):
        if row.decision_action or row.machine_approved in {"auto", "yes", "no"}:
            continue
        payload = json.loads(row.judgment_payload_json or "{}")
        payload["relationship_judgment"] = asdict(decision)
        settlements.append(replace(MachineIdentitySettlement.from_link(row),
            judgment_payload_json=json.dumps(payload, ensure_ascii=False)))
    settle_machine_identities(db, settlements)


def finish_reviews(
    db: Db,
    decisions: Sequence[RelationshipDecision],
    *,
    limit: int = REVIEW_LIMIT,
) -> dict[str, object]:
    if not 0 <= limit <= REVIEW_LIMIT:
        raise StoreError(f"review limit must be between 0 and {REVIEW_LIMIT}")
    pending = pending_parent_ids(db)
    by_parent = {decision.parent_id: decision for decision in decisions}
    if len(by_parent) != len(decisions):
        raise StoreError("duplicate relationship decisions")
    missing = pending - by_parent.keys()
    if missing:
        raise StoreError(f"missing relationship decisions for {len(missing)} pending parents")
    for parent_id in pending:
        decision = by_parent[parent_id]
        if (decision.decision not in {"keep", "exclude"}
                or not 0 <= decision.priority <= 3 or not decision.fingerprint):
            raise StoreError(f"invalid relationship decision: {parent_id}")

    protected = protected_parent_ids(db)
    questions = sorted((by_parent[parent_id] for parent_id in sorted(pending)
        if (by_parent[parent_id].decision == "keep" or parent_id in protected)
        and by_parent[parent_id].useful_answerable_question
        and by_parent[parent_id].human_question.strip()
        and by_parent[parent_id].priority > 0),
        key=lambda decision: (decision.priority, decision.message_count),
        reverse=True)
    selected = {decision.parent_id for decision in questions[:limit]}
    settlements = []
    for row in links(db, parent_ids=sorted(pending)):
        if row.decision_action or row.machine_approved in {"auto", "yes", "no"}:
            continue
        decision = by_parent[row.parent_id]
        payload = json.loads(row.judgment_payload_json or "{}")
        payload.pop("relationship_judgment", None)
        payload["relationship_decision"] = asdict(decision)
        if row.parent_id in selected:
            settlements.append(replace(MachineIdentitySettlement.from_link(row),
                judgment_fingerprint=row.judgment_fingerprint or decision.fingerprint,
                judgment_payload_json=json.dumps(payload, ensure_ascii=False),
                machine_reason=f"{decision.human_question} {decision.reason}",
            ))
            continue
        action = "exclude" if decision.decision == "exclude" and row.parent_id not in protected else "detach"
        reason = f"{decision.reason} LinkedIn identity remains unverified."
        payload.update(verdict="needs_review", confidence=0, reason=reason,
                       recommend_deep_research=False, linkedin_plausibly_absent=False)
        settlements.append(MachineIdentitySettlement(
            key=row.row_key,
            judgment_fingerprint=decision.fingerprint,
            judgment_payload_json=json.dumps(payload, ensure_ascii=False),
            machine_action=action,
            machine_approved="auto",
            machine_confidence=None,
            machine_reason=reason,
            machine_judgment="needs_review",
            source=WriterSource.RECONCILE.value,
        ))
    projected, preserved, _ = settle_machine_identities(db, settlements)
    return {"review_parents": len(selected), "review_parent_ids": sorted(selected),
            "completed_parents": len(pending - selected), "projected_rows": len(projected),
            "preserved_rows": len(preserved)}
