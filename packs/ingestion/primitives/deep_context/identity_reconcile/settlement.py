"""Project machine identity conclusions while preserving untouched link columns."""

from __future__ import annotations

from dataclasses import dataclass, fields

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db.models import (
    ApprovedState,
    IdentityMachineProjection,
    LinkSnapshotRow,
    _IdentityMachineFields,
)
from packs.ingestion.primitives.deep_context.db.snapshots import identity_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError

USER_APPROVED = {ApprovedState.YES.value, ApprovedState.NO.value}


@dataclass(frozen=True)
class MachineIdentitySettlement:
    """One machine conclusion translated to the links projection exactly once."""

    key: str
    judgment_fingerprint: str
    judgment_payload_json: str
    machine_action: str
    machine_approved: str | None
    machine_confidence: float
    machine_reason: str
    machine_judgment: str | None
    authoritative_detach: int = 0
    judgment_artifact_path: str | None = None
    source: str = ""
    machine_proposed_url: str | None = None
    machine_proposed_public_identifier: str | None = None
    paid_profile: int = 0
    machine_reject: str | None = None
    machine_reject_confidence: float = 0.0
    machine_reject_reason: str | None = None
    has_reject_fields: bool = False

    def projection(self, row: LinkSnapshotRow) -> IdentityMachineProjection:
        """Build the typed Db projection while preserving untouched columns."""
        retarget = self.machine_action == "retarget"
        values = {
            field.name: getattr(row, field.name)
            for field in fields(_IdentityMachineFields)
        }
        values.update({
            "machine_action": self.machine_action,
            "machine_approved": self.machine_approved,
            "machine_confidence": self.machine_confidence,
            "machine_reason": self.machine_reason,
            "judgment_fingerprint": self.judgment_fingerprint,
            "judgment_payload_json": self.judgment_payload_json,
            "source": self.source,
            "updated_at": now_iso(),
        })
        if retarget:
            values.update({
                "machine_proposed_url": self.machine_proposed_url,
                "machine_proposed_public_identifier": (
                    self.machine_proposed_public_identifier
                ),
                "paid_profile": self.paid_profile,
            })
            if self.has_reject_fields:
                values.update({
                    "machine_reject": self.machine_reject,
                    "machine_reject_confidence": self.machine_reject_confidence,
                    "machine_reject_reason": self.machine_reject_reason,
                })
        else:
            values.update({
                "machine_judgment": self.machine_judgment,
                "authoritative_detach": self.authoritative_detach,
                "judgment_artifact_path": self.judgment_artifact_path,
            })
        return IdentityMachineProjection(row.row_key, **values)


def settle_machine_identities(
    db: Db,
    settlements: list[MachineIdentitySettlement],
) -> tuple[set[str], set[str], int]:
    """Project every machine identity conclusion through one SQLite path."""
    snapshot = identity_snapshot(db)
    existing = {row.key: row for row in snapshot.review_rows}
    projections = []
    preserved: set[str] = set()
    projected: set[str] = set()
    for settlement in settlements:
        key = settlement.key.lower()
        if not key:
            continue
        if not settlement.judgment_fingerprint:
            raise StoreError(f"machine identity settlement lacks judge fingerprint: {key}")
        if key in existing and str(existing[key].approved or "").lower() in USER_APPROVED:
            preserved.add(key)
            continue
        row = next((row for row in snapshot.links if row.row_key == key), None)
        if row is None:
            raise StoreError(f"unknown identity candidate: {key}")
        projections.append(settlement.projection(row))
        projected.add(key)
    db.project_rows(tuple(projections))
    return projected, preserved, len(existing)
