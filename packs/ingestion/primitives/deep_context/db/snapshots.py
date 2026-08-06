"""Typed producer snapshots from canonical Deep Context SQLite state."""
from __future__ import annotations

from typing import TypeVar

from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    CandidatePersonRow,
    CanonicalSnapshot,
    FactRow,
    IdentitySnapshot,
    LinkSnapshotRow,
    ParentSnapshotRow,
    PersonIdentifierRow,
    PersonRow,
    PersonSourceRow,
    ResearchRow,
    ReviewExportRow,
    SyntheticProfileRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db


RowT = TypeVar("RowT")


def _rows(db: Db, sql: str, row_type: type[RowT]) -> tuple[RowT, ...]:
    return tuple(row_type(**dict(row)) for row in db._query(sql))


def canonical_snapshot(db: Db) -> CanonicalSnapshot:
    """Canonical people, provenance, and fact/artifact ownership for producers."""
    return CanonicalSnapshot(
        parents=_rows(db, "SELECT * FROM parents ORDER BY parent_id", ParentSnapshotRow),
        people=_rows(db, "SELECT * FROM people ORDER BY person_id", PersonRow),
        identifiers=_rows(
            db,
            "SELECT * FROM person_identifiers ORDER BY person_id, kind, normalized_value",
            PersonIdentifierRow,
        ),
        sources=_rows(
            db, "SELECT * FROM person_sources ORDER BY person_id, source", PersonSourceRow,
        ),
        artifacts=_rows(db, "SELECT * FROM artifacts ORDER BY artifact_key", ArtifactRow),
        facts=_rows(db, "SELECT * FROM facts ORDER BY subject_key", FactRow),
    )


def identity_snapshot(db: Db) -> IdentitySnapshot:
    """Identity candidates, membership, research, and synthetic producer inputs."""
    links = _rows(db, "SELECT * FROM links ORDER BY row_key", LinkSnapshotRow)
    memberships = _rows(
        db, "SELECT * FROM candidate_people ORDER BY row_key, person_id", CandidatePersonRow,
    )
    people_by_link: dict[str, str] = {}
    for row in memberships:
        people_by_link.setdefault(row.row_key, row.person_id)
    parents = _rows(db, "SELECT * FROM parents ORDER BY parent_id", ParentSnapshotRow)
    review_rows = [
        ReviewExportRow(
            key=row.row_key,
            public_identifier=row.public_identifier,
            action=row.decision_action or row.machine_action or "",
            approved=row.decision_approved or row.machine_approved or "",
            new_linkedin_url=(
                row.replacement_url
                or (row.machine_proposed_url if row.decision_action is None else None)
                or ""
            ),
            new_public_identifier=(
                row.replacement_public_identifier
                or (row.machine_proposed_public_identifier if row.decision_action is None else None)
                or ""
            ),
            linkedin_url=row.linkedin_url or "",
            confidence="" if row.machine_confidence is None else str(row.machine_confidence),
            reason=row.machine_reason or "",
            person_id=people_by_link.get(row.row_key, ""),
            source=row.decision_source or row.source or "",
            updated_at=row.decided_at or row.updated_at or "",
            llm_reject=row.machine_reject or "",
            llm_reject_confidence=(
                "" if row.machine_reject_confidence is None else str(row.machine_reject_confidence)
            ),
            llm_reject_reason=row.machine_reject_reason or "",
            llm_judge_fingerprint=row.judgment_fingerprint or "",
        )
        for row in links
    ]
    review_rows.extend(
        ReviewExportRow(
            key=f"parent-worth:{row.parent_id}",
            public_identifier=row.public_identifier,
            llm_worth=row.machine_worth or "",
            llm_worth_reason=row.machine_worth_reason or "",
            network_worth=row.human_worth or "",
            user_worth_note=row.human_worth_note or "",
            source=row.human_worth_source or row.source or "",
            updated_at=row.human_worth_at or row.updated_at or "",
        )
        for row in parents
    )
    return IdentitySnapshot(
        links=links,
        memberships=memberships,
        synthetic_profiles=_rows(
            db, "SELECT * FROM synthetic_profiles ORDER BY public_identifier", SyntheticProfileRow,
        ),
        research=_rows(db, "SELECT * FROM research ORDER BY handle", ResearchRow),
        review_rows=tuple(review_rows),
    )
