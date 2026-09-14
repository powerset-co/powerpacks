"""Project machine identity conclusions while preserving untouched link columns."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields, replace

from packs.ingestion.primitives.common.jsonio import now_iso, parse_json_object
from packs.ingestion.primitives.deep_context.db.models import (
    ApprovedState,
    IdentityMachineProjection,
    LinkSnapshotRow,
    _IdentityMachineFields,
)
from packs.ingestion.primitives.deep_context.db.identity_policy import IdentityPolicy
from packs.ingestion.primitives.deep_context.db.identity_queries import finished_unlinked_keys, links, review_row_count
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError

USER_APPROVED = {ApprovedState.YES.value, ApprovedState.NO.value}
# Preserve effective yes/no decisions, including legacy machine approvals.


@dataclass(frozen=True)
class MachineIdentitySettlement:
    """One machine conclusion translated to the links projection exactly once."""

    key: str
    judgment_fingerprint: str | None
    judgment_payload_json: str | None
    machine_action: str | None
    machine_approved: str | None
    machine_confidence: float | None
    machine_reason: str | None
    machine_judgment: str | None
    authoritative_detach: bool = False
    judgment_artifact_path: str | None = None
    source: str | None = ""
    machine_proposed_url: str | None = None
    machine_proposed_public_identifier: str | None = None
    paid_profile: bool = False

    @classmethod
    def from_link(cls, row: LinkSnapshotRow) -> MachineIdentitySettlement:
        """Start a payload update with the link's identity fields unchanged."""
        return cls(key=row.row_key, **{
            field.name: getattr(row, field.name) for field in fields(cls) if field.name != "key"
        })

    def _preserves_identity(self, row: LinkSnapshotRow) -> bool:
        return all(getattr(self, field.name) == getattr(row, field.name)
                   for field in fields(self) if field.name not in {"key", "judgment_payload_json"})

    @property
    def outcome(self) -> str:
        """The tally bucket this settlement lands in — "pending" /
        "verify_auto" / "detach_auto" / "other". Spelled here, next to the
        fields it reads, so report counts can never drift from the row shape
        (write_overrides used to re-test these field combinations inline)."""
        if self.machine_approved is None:
            return "pending"
        if self.machine_approved == ApprovedState.AUTO.value:
            if self.machine_action == "verify":
                return "verify_auto"
            if self.machine_action == "detach":
                return "detach_auto"
        return "other"

    def projection(self, row: LinkSnapshotRow) -> IdentityMachineProjection:
        """Build the typed Db projection while preserving untouched columns."""
        retarget = self.machine_action == "retarget"
        # Seed from the row's current values so only the fields this settlement
        # actually decides get overwritten below — every other column round-trips.
        values = {field.name: getattr(row, field.name) for field in fields(_IdentityMachineFields)}
        values.update(
            {
                "machine_action": self.machine_action,
                "machine_approved": self.machine_approved,
                "machine_confidence": self.machine_confidence,
                "machine_judgment": self.machine_judgment,
                "machine_reason": self.machine_reason,
                "judgment_fingerprint": self.judgment_fingerprint,
                "judgment_payload_json": self.judgment_payload_json,
                "source": self.source,
                "updated_at": now_iso(),
            }
        )
        if self._preserves_identity(row):
            return IdentityMachineProjection(row.row_key, **values)
        if retarget:
            values.update(
                {
                    "machine_proposed_url": self.machine_proposed_url,
                    "machine_proposed_public_identifier": (self.machine_proposed_public_identifier),
                    "paid_profile": self.paid_profile,
                }
            )
        else:
            values.update(
                {
                    "authoritative_detach": self.authoritative_detach,
                    "judgment_artifact_path": self.judgment_artifact_path,
                    # Retire any earlier retarget proposal: a row settling through
                    # confirm/detach/verify shouldn't carry a stale suggested URL.
                    "machine_proposed_url": None,
                    "machine_proposed_public_identifier": None,
                }
            )
        return IdentityMachineProjection(row.row_key, **values)


def settle_machine_identities(
    db: Db,
    settlements: list[MachineIdentitySettlement],
) -> tuple[set[str], set[str], int]:
    """Project every machine identity conclusion through one SQLite path.

    This is the ONLY writer of the `links` machine_* columns. Every caller
    (write_overrides, upsert_retargets, and any healing/guided path) must
    route a decision through a MachineIdentitySettlement and this function —
    never call db.project_rows with an IdentityMachineProjection directly, or
    the fingerprint requirement and the human-decision guard below are bypassed.
    """
    total_rows = review_row_count(db)
    finished = finished_unlinked_keys(db)
    settlement_keys = tuple(row.key.lower() for row in settlements if row.key)
    link_rows = {row.row_key: row for row in links(db, row_keys=settlement_keys)}
    projections: list[IdentityMachineProjection] = []
    preserved: set[str] = set()
    projected: set[str] = set()
    for settlement in settlements:
        key = settlement.key.lower()
        if not key:
            continue
        row: LinkSnapshotRow | None = link_rows.get(key)
        if row is None:
            # No matching link row for this key — a settlement built from a
            # stale or mismatched query, not a transient condition; fail loudly.
            raise StoreError(f"unknown identity candidate: {key}")
        decision = IdentityPolicy.effective_decision(
            decision_action=row.decision_action,
            decision_approved=row.decision_approved,
            replacement_url=row.replacement_url,
            replacement_public_identifier=row.replacement_public_identifier,
            machine_action=row.machine_action,
            machine_approved=row.machine_approved,
            machine_proposed_url=row.machine_proposed_url,
            machine_proposed_public_identifier=row.machine_proposed_public_identifier,
            linkedin_url=row.linkedin_url,
            public_identifier=row.public_identifier,
        )
        if decision.approved.lower() in USER_APPROVED:
            preserved.add(key)
            continue
        preserves_identity = settlement._preserves_identity(row)
        # Relationship checkpoints can precede an identity verdict. Only a
        # payload-only update may retain that absent identity fingerprint.
        if not settlement.judgment_fingerprint and not preserves_identity:
            raise StoreError(f"machine identity settlement lacks decision fingerprint: {key}")
        if key in finished and not preserves_identity:
            preserved.add(key)
            continue
        prior = parse_json_object(row.judgment_payload_json)
        payload = parse_json_object(settlement.judgment_payload_json)
        if "relationship_judgment" in prior and "relationship_decision" not in payload:
            payload.setdefault("relationship_judgment", prior["relationship_judgment"])
            settlement = replace(settlement, judgment_payload_json=json.dumps(payload, ensure_ascii=False))
        if preserves_identity and payload == prior:
            continue
        projections.append(settlement.projection(row))
        projected.add(key)
    if projections:
        db.project_rows(tuple(projections))
    return projected, preserved, total_rows
