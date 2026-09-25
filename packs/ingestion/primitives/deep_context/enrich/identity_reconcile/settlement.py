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
from packs.ingestion.primitives.deep_context.db.identity_queries import links, review_rows
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError

USER_APPROVED = {ApprovedState.YES.value, ApprovedState.NO.value}
# "auto" (machine-only) is deliberately excluded here — only an explicit human
# yes/no blocks settle_machine_identities from overwriting a row.


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

    def projection(self, row: LinkSnapshotRow) -> IdentityMachineProjection:
        """Build the typed Db projection while preserving untouched columns."""
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
    existing = {row.key: row for row in review_rows(db)}
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
        if key in existing and str(existing[key].approved or "").lower() in USER_APPROVED:
            # A human already decided yes/no on this row: the fresh machine
            # conclusion loses, silently, rather than raising.
            continue
        row: LinkSnapshotRow | None = link_rows.get(key)
        if row is None:
            # No matching link row for this key — a settlement built from a
            # stale or mismatched query, not a transient condition; fail loudly.
            raise StoreError(f"unknown identity candidate: {key}")
        projections.append(settlement.projection(row))
        projected.add(key)
    db.project_rows(tuple(projections))
    return projected
