"""Typed producer snapshots and export batons from canonical Deep Context SQLite state.

The single home of the effective-decision projection: ``identity_snapshot``
builds the typed review rows and ``export_batons`` serializes those same rows,
so the CSV baton other stages read can never drift from what producers see.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import TypeVar

from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context.db import batons
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    CandidatePersonRow,
    CanonicalSnapshot,
    DossierSnapshotRow,
    FactRow,
    IdentitySnapshot,
    LinkSnapshotRow,
    MergeVerdictRow,
    OwnerContextRow,
    ParentSnapshotRow,
    PersonIdentifierRow,
    PersonRow,
    PersonSourceRow,
    ResearchRow,
    ReviewAction,
    ReviewExportRow,
    SyntheticProfileRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db


RowT = TypeVar("RowT")


def _rows(db: Db, sql: str, row_type: type[RowT]) -> tuple[RowT, ...]:
    return tuple(row_type(**dict(row)) for row in db.query(sql))

def _dossiers(
    parents: tuple[ParentSnapshotRow, ...],
    people: tuple[PersonRow, ...],
    identifiers: tuple[PersonIdentifierRow, ...],
    artifacts: tuple[ArtifactRow, ...],
) -> tuple[DossierSnapshotRow, ...]:
    values: dict[str, dict[str, list[str]]] = {}
    for row in identifiers:
        display = row.display_value or row.normalized_value
        items = values.setdefault(row.person_id, {}).setdefault(row.kind, [])
        if display not in items:
            items.append(display)
    person_artifacts = {
        row.person_id: row
        for row in artifacts
        if row.kind == "dossier" and row.status == "projected" and row.person_id
    }
    parent_artifacts = {
        row.parent_id: row
        for row in artifacts
        if row.kind == "dossier" and row.status == "projected"
        and not row.person_id and not row.candidate_key
    }

    children = []
    children_by_parent: dict[str, list[DossierSnapshotRow]] = {}
    people_by_parent: dict[str, list[PersonRow]] = {}
    for person in people:
        people_by_parent.setdefault(person.parent_id, []).append(person)
        artifact = person_artifacts.get(person.person_id)
        if artifact is None or not person.child_slug:
            continue
        payload = parse_json_object(artifact.payload_json)
        row = DossierSnapshotRow(
            slug=person.child_slug,
            name=str(payload.get("name") or person.display_name or person.child_slug),
            path=str(payload.get("path") or f"dossiers/{person.child_slug}.md"),
            artifact_path=artifact.path,
            headline=str(payload.get("headline") or ""),
            full_name=str(payload.get("full_name") or person.display_name or ""),
            emails=tuple(payload.get("emails") or values.get(person.person_id, {}).get("email", [])),
            phones=tuple(payload.get("phones") or values.get(person.person_id, {}).get("phone", [])),
            parent_id=person.parent_id,
            person_id=person.person_id,
            body=str(payload.get("body") or ""),
            source_channels=tuple(payload.get("source_channels") or ()),
        )
        children.append(row)
        children_by_parent.setdefault(person.parent_id, []).append(row)

    parent_rows = []
    for parent in parents:
        artifact = parent_artifacts.get(parent.parent_id)
        dossier_members = children_by_parent.get(parent.parent_id, [])
        people_members = people_by_parent.get(parent.parent_id, [])
        if artifact is None or not parent.display_slug or not people_members:
            continue
        payload = parse_json_object(artifact.payload_json)
        member_ids = {row.person_id for row in people_members}
        emails = tuple(dict.fromkeys(
            display
            for person_id in sorted(member_ids)
            for display in values.get(person_id, {}).get("email", [])
        ))
        phones = tuple(dict.fromkeys(
            display
            for person_id in sorted(member_ids)
            for display in values.get(person_id, {}).get("phone", [])
        ))
        parent_rows.append(DossierSnapshotRow(
            slug=parent.display_slug,
            name=str(payload.get("name") or parent.display_name or parent.display_slug),
            path=str(payload.get("path") or f"parents/{parent.display_slug}.md"),
            artifact_path=artifact.path,
            headline=str(payload.get("headline") or ""),
            full_name=str(payload.get("full_name") or parent.display_name or ""),
            emails=tuple(payload.get("emails") or emails),
            phones=tuple(payload.get("phones") or phones),
            parent_id=parent.parent_id,
            children=tuple(
                payload.get("children")
                or [row.child_slug for row in people_members if row.child_slug]
            ),
            body=str(payload.get("body") or ""),
            source_channels=tuple(payload.get("source_channels") or dict.fromkeys(
                source for row in dossier_members for source in row.source_channels
            )),
        ))
    return tuple(sorted(children, key=lambda row: row.slug)) + tuple(
        sorted(parent_rows, key=lambda row: row.slug)
    )


def canonical_snapshot(db: Db) -> CanonicalSnapshot:
    """Canonical people, provenance, and fact/artifact ownership for producers."""
    owner_rows = _rows(
        db, "SELECT * FROM owner_context WHERE context_key='owner'", OwnerContextRow,
    )
    owner = parse_json_object(owner_rows[0].payload_json) if owner_rows else None
    parents = _rows(db, "SELECT * FROM parents ORDER BY parent_id", ParentSnapshotRow)
    people = _rows(db, "SELECT * FROM people ORDER BY person_id", PersonRow)
    identifiers = _rows(
        db,
        "SELECT * FROM person_identifiers ORDER BY person_id, kind, normalized_value",
        PersonIdentifierRow,
    )
    artifacts = _rows(db, "SELECT * FROM artifacts ORDER BY artifact_key", ArtifactRow)
    return CanonicalSnapshot(
        owner=owner,
        owner_path=owner_rows[0].path if owner_rows else None,
        parents=parents,
        people=people,
        identifiers=identifiers,
        sources=_rows(
            db, "SELECT * FROM person_sources ORDER BY person_id, source", PersonSourceRow,
        ),
        artifacts=artifacts,
        facts=_rows(db, "SELECT * FROM facts ORDER BY subject_key", FactRow),
        dossiers=_dossiers(parents, people, identifiers, artifacts),
        merge_verdicts=_rows(
            db, "SELECT * FROM merge_verdicts ORDER BY person_a, person_b", MergeVerdictRow,
        ),
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
    guidance = []
    for row in db.query("SELECT * FROM guidance ORDER BY submitted_at, handle"):
        item = dict(row)
        item["detail"] = parse_json_object(item.pop("detail_json"))
        guidance.append(item)
    link_keys = {row.row_key for row in links}
    link_decisions = {
        row.key: {
            "action": row.action,
            "approved": row.approved,
            "llm_reject": row.llm_reject,
            "llm_judge_fingerprint": row.llm_judge_fingerprint,
            "new_linkedin_url": row.new_linkedin_url,
        }
        for row in review_rows
        if row.key in link_keys
    }
    return IdentitySnapshot(
        links=links,
        memberships=memberships,
        synthetic_profiles=_rows(
            db, "SELECT * FROM synthetic_profiles ORDER BY public_identifier", SyntheticProfileRow,
        ),
        research=_rows(db, "SELECT * FROM research ORDER BY handle", ResearchRow),
        review_rows=tuple(review_rows),
        guidance=tuple(guidance),
        link_decisions=link_decisions,
    )


def export_batons(db: Db, review_csv: Path, synthetic_csv: Path | None = None) -> None:
    """Write the review.csv baton (and synthetic projection) other stages read."""
    snapshot = identity_snapshot(db)
    batons._write_override_rows(
        review_csv, {row.key: asdict(row) for row in snapshot.review_rows},
    )
    if synthetic_csv is None:
        return
    links = {row.row_key: row for row in snapshot.links}
    batons._write_synthetic_rows(synthetic_csv, [
        json.loads(profile.profile_json) | {
            "public_identifier": profile.public_identifier,
            "linkedin_url": profile.linkedin_url or "",
            "approved": (
                "no"
                if links[profile.candidate_key].decision_action
                in {ReviewAction.DETACH.value, ReviewAction.EXCLUDE.value}
                else "yes"
                if links[profile.candidate_key].decision_action == ReviewAction.VERIFY.value
                and links[profile.candidate_key].decision_approved == "yes"
                else links[profile.candidate_key].machine_approved or ""
            ),
        }
        for profile in snapshot.synthetic_profiles
    ])
