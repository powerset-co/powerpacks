"""Settle parent identity choices while preserving human decisions."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal

from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context.db.identity_queries import links
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.schemas.people_schema import normalize_linkedin_url
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.settlement import (
    MachineIdentitySettlement, settle_machine_identities,
)


@dataclass(frozen=True)
class CandidateDecision:
    url: str
    verdict: Literal['yes', 'no', 'review']
    reason: str
    confidence: float

    def __post_init__(self) -> None:
        if not self.url or self.verdict not in {'yes', 'no', 'review'} or not self.reason.strip() or not 0 <= self.confidence <= 1:
            raise ValueError('invalid identity decision')


@dataclass(frozen=True)
class RelationshipDecision:
    parent_id: str
    fingerprint: str
    candidates: tuple[CandidateDecision, ...]

    def __post_init__(self) -> None:
        by_url = {}
        for candidate in self.candidates:
            if candidate.url in by_url and by_url[candidate.url] != candidate.verdict:
                raise ValueError('conflicting decisions for the same URL')
            by_url[candidate.url] = candidate.verdict
        if not self.fingerprint or sum(verdict == 'yes' for verdict in by_url.values()) > 1:
            raise ValueError('multiple distinct identity winners')

    @classmethod
    def from_payload(cls, parent_id: str, fingerprint: str,
                     payload: dict[str, Any]) -> RelationshipDecision:
        return cls(parent_id, fingerprint, tuple(CandidateDecision(**candidate) for candidate in payload['candidates']))


def cache_relationship_judgment(db: Db, decision: RelationshipDecision) -> None:
    settlements = []
    for row in links(db, parent_id=decision.parent_id):
        if row.decision_action:
            continue
        payload = parse_json_object(row.judgment_payload_json)
        payload['relationship_judgment'] = asdict(decision)
        settlements.append(replace(MachineIdentitySettlement.from_link(row),
            judgment_fingerprint=row.judgment_fingerprint or decision.fingerprint,
            judgment_payload_json=json.dumps(payload, ensure_ascii=False)))
    settle_machine_identities(db, settlements)


def finish_reviews(db: Db, decisions: tuple[RelationshipDecision, ...]) -> dict[str, object]:
    settlements = []
    review_parents = set()
    for decision in decisions:
        rows = links(db, parent_id=decision.parent_id)
        if any(row.decision_action in {'verify', 'retarget'} and row.decision_approved in {'yes', 'auto'} for row in rows):
            continue
        choices = {candidate.url: candidate for candidate in decision.candidates}
        urls = {normalize_linkedin_url(row.machine_proposed_url or row.linkedin_url) for row in rows if not row.decision_action and row.kind != 'synthetic'} - {''}
        if choices.keys() != urls:
            raise StoreError(f'identity decision URLs differ from candidates: {decision.parent_id}')
        for row in rows:
            if row.decision_action or row.kind == 'synthetic':
                continue
            candidate = choices.get(normalize_linkedin_url(row.machine_proposed_url or row.linkedin_url))
            if candidate is None:
                continue
            verdict = {'yes': 'confirmed', 'no': 'wrong_person', 'review': 'needs_review'}[candidate.verdict]
            if candidate.verdict == 'review':
                review_parents.add(decision.parent_id)
            payload = parse_json_object(row.judgment_payload_json)
            payload.pop('relationship_judgment', None)
            payload['relationship_decision'] = asdict(decision)
            payload.update(verdict=verdict, reason=candidate.reason, confidence=candidate.confidence)
            action = row.machine_action
            if candidate.verdict == "yes":
                action = "retarget" if row.machine_proposed_url else "verify"
            elif candidate.verdict == "no":
                action = "detach"
            settlements.append(replace(MachineIdentitySettlement.from_link(row),
                judgment_fingerprint=row.judgment_fingerprint or decision.fingerprint,
                judgment_payload_json=json.dumps(payload, ensure_ascii=False),
                machine_action=action,
                machine_proposed_url=row.machine_proposed_url if action == 'retarget' else None,
                machine_proposed_public_identifier=row.machine_proposed_public_identifier if action == 'retarget' else None,
                machine_approved='auto' if candidate.verdict != 'review' else None,
                machine_judgment=verdict, machine_reason=candidate.reason, machine_confidence=candidate.confidence))
    projected = settle_machine_identities(db, settlements)
    return {'review_parents': len(review_parents), 'review_parent_ids': sorted(review_parents),
            'completed_parents': len(decisions) - len(review_parents), 'projected_rows': len(projected)}
