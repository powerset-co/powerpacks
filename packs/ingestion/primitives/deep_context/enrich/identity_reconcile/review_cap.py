"""Reserve at most 100 useful parent questions without accepting unknown identities."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace

from packs.ingestion.primitives.deep_context.db.identity_queries import links
from packs.ingestion.primitives.deep_context.db.identity_views import pending_parent_ids
from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.settlement import (
    MachineIdentitySettlement, settle_machine_identities,
)

REVIEW_LIMIT = 100


@dataclass(frozen=True)
class RelationshipDecision:
    parent_id: str
    reason: str
    useful_answerable_question: bool
    human_question: str
    priority: int
    fingerprint: str
    message_count: int = 0

    def __post_init__(self) -> None:
        useful = self.useful_answerable_question
        if (not self.fingerprint or not self.reason.strip() or not 0 <= self.priority <= 3
                or bool(self.human_question.strip()) != useful or (self.priority > 0) != useful):
            raise StoreError(f"inconsistent review question: {self.parent_id}")


def cache_relationship_judgment(db: Db, decision: RelationshipDecision) -> None:
    """Checkpoint the paid question judgment while retaining identity decisions."""
    settlements = []
    for row in links(db, parent_id=decision.parent_id):
        if row.decision_action or row.machine_approved in {"auto", "yes", "no"}:
            continue
        payload = parse_json_object(row.judgment_payload_json)
        payload["relationship_judgment"] = asdict(decision)
        settlements.append(replace(MachineIdentitySettlement.from_link(row),
            judgment_fingerprint=row.judgment_fingerprint or decision.fingerprint,
            judgment_payload_json=json.dumps(payload, ensure_ascii=False)))
    settle_machine_identities(db, settlements)


def finish_reviews(
    db: Db, decisions: Sequence[RelationshipDecision], *, limit: int = REVIEW_LIMIT,
) -> dict[str, object]:
    """Keep useful questions pending and detach other unresolved LinkedIns."""
    if not 0 <= limit <= REVIEW_LIMIT:
        raise StoreError(f"review limit must be between 0 and {REVIEW_LIMIT}")
    pending = pending_parent_ids(db)
    by_parent = {decision.parent_id: decision for decision in decisions}
    if len(by_parent) != len(decisions):
        raise StoreError("duplicate relationship decisions")
    if missing := pending - by_parent.keys():
        raise StoreError(f"missing relationship decisions for {len(missing)} pending parents")
    questions = sorted((by_parent[parent_id] for parent_id in sorted(pending)
        if by_parent[parent_id].useful_answerable_question),
        key=lambda decision: (decision.priority, decision.message_count), reverse=True)
    selected = {decision.parent_id for decision in questions[:limit]}
    settlements = []
    for row in links(db, parent_ids=sorted(pending)):
        if row.decision_action or row.machine_approved in {"auto", "yes", "no"}:
            continue
        decision = by_parent[row.parent_id]
        payload = parse_json_object(row.judgment_payload_json)
        payload.pop("relationship_judgment", None)
        payload["relationship_decision"] = asdict(decision)
        if row.parent_id in selected:
            settlements.append(replace(MachineIdentitySettlement.from_link(row),
                judgment_fingerprint=row.judgment_fingerprint or decision.fingerprint,
                judgment_payload_json=json.dumps(payload, ensure_ascii=False)))
            continue
        settlements.append(replace(MachineIdentitySettlement.from_link(row),
            judgment_fingerprint=row.judgment_fingerprint or decision.fingerprint,
            judgment_payload_json=json.dumps(payload, ensure_ascii=False),
            machine_action="detach", machine_approved="auto",
            machine_proposed_url=None, machine_proposed_public_identifier=None))
    projected = settle_machine_identities(db, settlements)
    return {"review_parents": len(selected), "review_parent_ids": sorted(selected),
            "completed_parents": len(pending - selected), "projected_rows": len(projected)}
