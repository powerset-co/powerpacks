"""Project machine identity conclusions while preserving untouched link columns."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields, replace

from packs.ingestion.primitives.common.jsonio import now_iso, parse_json_object
from packs.ingestion.primitives.deep_context.db.models import (
    IdentityMachineProjection,
    LinkSnapshotRow,
    _IdentityMachineFields,
)
from packs.ingestion.primitives.deep_context.db.identity_queries import links
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.db.identity_policy import AFFIRMATIVE_MACHINE_ACTIONS, AFFIRMATIVE_MACHINE_APPROVALS
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.name_policy import profile_name_verdict, profile_names

@dataclass(frozen=True)
class MachineIdentitySettlement:
    """One machine conclusion translated to the links projection exactly once."""

    key: str
    judgment_fingerprint: str
    judgment_payload_json: str | None
    machine_action: str
    machine_approved: str | None
    machine_confidence: float | None
    machine_reason: str
    machine_judgment: str | None
    source: str = ""
    machine_proposed_url: str | None = None
    machine_proposed_public_identifier: str | None = None
    paid_profile: bool = False

    @classmethod
    def from_link(cls, row: LinkSnapshotRow) -> MachineIdentitySettlement:
        """Retain the current machine fields while changing one conclusion."""
        values = {field.name: getattr(row, field.name) for field in fields(cls) if field.name != "key"}
        values["judgment_fingerprint"] = row.judgment_fingerprint or ""
        return cls(key=row.row_key, **values)

    def projection(self, row: LinkSnapshotRow) -> IdentityMachineProjection:
        """Build the typed Db projection while preserving untouched columns."""
        # Seed from the row's current values so only the fields this settlement
        # actually decides get overwritten below — every other column round-trips.
        values = {field.name: getattr(row, field.name) for field in fields(_IdentityMachineFields)}
        payload = json.loads(self.judgment_payload_json or "{}")
        previous = parse_json_object(row.judgment_payload_json)
        if not ({"relationship_judgment", "relationship_decision"} & payload.keys()):
            for key in ("relationship_judgment", "relationship_decision"):
                if key in previous:
                    payload[key] = previous[key]
        values.update(
            {
                "machine_action": self.machine_action,
                "machine_approved": self.machine_approved,
                "machine_confidence": self.machine_confidence,
                "machine_judgment": self.machine_judgment,
                "machine_reason": self.machine_reason,
                "judgment_fingerprint": self.judgment_fingerprint,
                "judgment_payload_json": json.dumps(payload, ensure_ascii=False) if payload else self.judgment_payload_json,
                "source": self.source,
                "updated_at": now_iso(),
                "machine_proposed_url": self.machine_proposed_url,
                "machine_proposed_public_identifier": self.machine_proposed_public_identifier,
                "paid_profile": self.paid_profile,
            }
        )
        return IdentityMachineProjection(row.row_key, **values)


def settle_machine_identities(
    db: Db,
    settlements: list[MachineIdentitySettlement],
) -> set[str]:
    """Project every machine identity conclusion through one SQLite path.

    This is the ONLY writer of the `links` machine_* columns. Every caller
    (upsert_retargets, for batch and guided research) must route a decision
    through a MachineIdentitySettlement and this function — never call
    db.project_rows with an IdentityMachineProjection directly, or the
    fingerprint requirement and the human-decision guard below are bypassed.
    """
    settlement_keys = tuple(row.key.lower() for row in settlements if row.key)
    link_rows = {row.row_key: row for row in links(db, row_keys=settlement_keys)}
    projections: list[IdentityMachineProjection] = []
    projected: set[str] = set()
    for settlement in settlements:
        key = settlement.key.lower()
        if not key:
            continue
        if not settlement.judgment_fingerprint:
            raise StoreError(f"machine identity settlement lacks decision fingerprint: {key}")
        row: LinkSnapshotRow | None = link_rows.get(key)
        if row is None:
            # No matching link row for this key — a settlement built from a
            # stale or mismatched query, not a transient condition; fail loudly.
            raise StoreError(f"unknown identity candidate: {key}")
        if row.decision_action:
            continue
        if settlement.machine_action in AFFIRMATIVE_MACHINE_ACTIONS and settlement.machine_approved in AFFIRMATIVE_MACHINE_APPROVALS:
            url = settlement.machine_proposed_url if settlement.machine_action == "retarget" else row.linkedin_url
            veto = profile_name_verdict(db, row.parent_id, profile_names(db, row.parent_id, key, url or ""))
            if veto is not None:
                settlement = replace(settlement, machine_approved=None, machine_confidence=veto.confidence,
                                     machine_judgment=veto.value, machine_reason=veto.reason,
                                     judgment_payload_json=json.dumps(veto.as_dict()))
        projection = settlement.projection(row)
        if all(getattr(projection, field.name) == getattr(row, field.name)
               for field in fields(_IdentityMachineFields) if field.name != "updated_at"):
            continue
        projections.append(projection)
        projected.add(key)
    db.project_rows(tuple(projections))
    return projected
