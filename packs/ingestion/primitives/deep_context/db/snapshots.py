"""Typed producer snapshots from canonical Deep Context SQLite state."""
from __future__ import annotations

import json
from typing import TypeVar

from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context.db._view_sql import WORTH_CTE
from packs.ingestion.primitives.deep_context.db.identity_policy import IdentityPolicy
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    CandidatePersonRow,
    CanonicalSnapshot,
    DossierSnapshotRow,
    FactRow,
    GuidanceDetailSnapshot,
    GuidanceRequestSnapshot,
    GuidanceSnapshotRow,
    IdentitySnapshot,
    LinkDecisionSnapshotRow,
    LinkSnapshotRow,
    MergeVerdictRow,
    OwnerEducation,
    OwnerContextRow,
    OwnerProfile,
    OwnerWork,
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

_BOOLEAN_COLUMNS = frozenset({
    "accepted",
    "authoritative_detach",
    "candidate_origin",
    "is_ghost",
    "is_owner",
    "paid_profile",
    "raw_import",
    "same_person",
    "tone_consistent",
})


def _rows(db: Db, sql: str, row_type: type[RowT]) -> tuple[RowT, ...]:
    result: list[RowT] = []
    for row in db.query(sql):
        values = dict(row)
        for column in _BOOLEAN_COLUMNS.intersection(values):
            values[column] = bool(values[column])
        result.append(row_type(**values))
    return tuple(result)


def _optional_text(value: object) -> str | None:
    text = str(value or "")
    return text or None


def _owner_profile(payload: dict[str, object]) -> OwnerProfile:
    def date(value: object) -> int | str | None:
        return value if isinstance(value, (int, str)) else None

    education: list[OwnerEducation] = []
    for item in payload.get("education") or ():
        if not isinstance(item, dict):
            continue
        education.append(OwnerEducation(
            school=str(item.get("school") or ""),
            start=date(item.get("start")),
            end=date(item.get("end")),
            note=str(item.get("note") or ""),
        ))
    work: list[OwnerWork] = []
    for item in payload.get("work") or ():
        if not isinstance(item, dict):
            continue
        work.append(OwnerWork(
            company=str(item.get("company") or ""),
            title=str(item.get("title") or ""),
            start=date(item.get("start")),
            end=date(item.get("end")),
        ))
    return OwnerProfile(
        name=str(payload.get("name") or ""),
        emails=tuple(str(value) for value in payload.get("emails") or ()),
        phones=tuple(str(value) for value in payload.get("phones") or ()),
        education=tuple(education),
        work=tuple(work),
        locations=tuple(str(value) for value in payload.get("locations") or ()),
        notes=str(payload.get("notes") or ""),
    )


def _guidance_request(payload: object) -> GuidanceRequestSnapshot | None:
    if not isinstance(payload, dict):
        return None
    required = ("slug", "row_key", "name", "guidance")
    if any(key not in payload for key in required):
        return None
    return GuidanceRequestSnapshot(
        slug=str(payload["slug"] or ""),
        row_key=str(payload["row_key"] or ""),
        name=str(payload["name"] or ""),
        guidance=str(payload["guidance"] or ""),
        person_ids=tuple(str(value) for value in payload.get("person_ids") or ()),
        linkedin_url=str(payload.get("linkedin_url") or ""),
        submitted_at=_optional_text(payload.get("submitted_at")),
        match_emails=tuple(str(value) for value in payload.get("match_emails") or ()),
        match_phones=tuple(str(value) for value in payload.get("match_phones") or ()),
    )


def _guidance_detail(payload: dict[str, object]) -> GuidanceDetailSnapshot:
    known = {
        "slug", "row_key", "name", "guidance", "state", "detail",
        "submitted_at", "updated_at", "new_url",
    }
    return GuidanceDetailSnapshot(
        slug=str(payload.get("slug") or ""),
        row_key=str(payload.get("row_key") or ""),
        name=str(payload.get("name") or ""),
        guidance=str(payload.get("guidance") or ""),
        state=str(payload.get("state") or ""),
        detail=str(payload.get("detail") or ""),
        submitted_at=_optional_text(payload.get("submitted_at")),
        updated_at=_optional_text(payload.get("updated_at")),
        new_url=_optional_text(payload.get("new_url")),
        request=_guidance_request(payload.get("request")),
        wire_fields=tuple(payload),
        extra_json=json.dumps(
            {key: value for key, value in payload.items() if key not in known},
            separators=(",", ":"),
        ),
    )

def _dossiers(
    parents: tuple[ParentSnapshotRow, ...],
    people: tuple[PersonRow, ...],
    identifiers: tuple[PersonIdentifierRow, ...],
    sources: tuple[PersonSourceRow, ...],
    artifacts: tuple[ArtifactRow, ...],
) -> tuple[DossierSnapshotRow, ...]:
    values: dict[str, dict[str, list[str]]] = {}
    for row in identifiers:
        display = row.display_value or row.normalized_value
        items = values.setdefault(row.person_id, {}).setdefault(row.kind, [])
        if display not in items:
            items.append(display)
    channels: dict[str, list[str]] = {}
    for row in sources:
        items = channels.setdefault(row.person_id, [])
        if row.source not in items:
            items.append(row.source)
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
    people_by_parent: dict[str, list[PersonRow]] = {}
    for person in people:
        people_by_parent.setdefault(person.parent_id, []).append(person)
        artifact: ArtifactRow | None = person_artifacts.get(person.person_id)
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
            emails=tuple(values.get(person.person_id, {}).get("email", [])),
            phones=tuple(values.get(person.person_id, {}).get("phone", [])),
            parent_id=person.parent_id,
            person_id=person.person_id,
            body=str(payload.get("body") or ""),
            source_channels=tuple(channels.get(person.person_id, [])),
        )
        children.append(row)

    parent_rows = []
    for parent in parents:
        artifact: ArtifactRow | None = parent_artifacts.get(parent.parent_id)
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
            emails=emails,
            phones=phones,
            parent_id=parent.parent_id,
            children=tuple(row.child_slug for row in people_members if row.child_slug),
            body=str(payload.get("body") or ""),
            source_channels=tuple(dict.fromkeys(
                source
                for row in sorted(people_members, key=lambda member: member.person_id)
                for source in channels.get(row.person_id, [])
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
    owner = (
        _owner_profile(parse_json_object(owner_rows[0].payload_json))
        if owner_rows
        else None
    )
    parents = _rows(db, "SELECT * FROM parents ORDER BY parent_id", ParentSnapshotRow)
    people = _rows(db, "SELECT * FROM people ORDER BY person_id", PersonRow)
    identifiers = _rows(
        db,
        "SELECT * FROM person_identifiers ORDER BY person_id, kind, normalized_value",
        PersonIdentifierRow,
    )
    sources = _rows(
        db, "SELECT * FROM person_sources ORDER BY person_id, source", PersonSourceRow,
    )
    artifacts = _rows(db, "SELECT * FROM artifacts ORDER BY artifact_key", ArtifactRow)
    return CanonicalSnapshot(
        owner=owner,
        owner_path=owner_rows[0].path if owner_rows else None,
        parents=parents,
        people=people,
        identifiers=identifiers,
        sources=sources,
        artifacts=artifacts,
        facts=_rows(db, "SELECT * FROM facts ORDER BY subject_key", FactRow),
        dossiers=_dossiers(parents, people, identifiers, sources, artifacts),
        merge_verdicts=_rows(
            db, "SELECT * FROM merge_verdicts ORDER BY person_a, person_b", MergeVerdictRow,
        ),
    )


def _identity_review_row(
    row: LinkSnapshotRow,
    people_by_link: dict[str, str],
) -> ReviewExportRow:
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
    return ReviewExportRow(
        key=row.row_key,
        public_identifier=row.public_identifier,
        action=decision.action or None,
        approved=decision.approved or None,
        new_linkedin_url=decision.new_url or None,
        new_public_identifier=decision.new_public_identifier or None,
        linkedin_url=row.linkedin_url,
        confidence=None if row.machine_confidence is None else str(row.machine_confidence),
        reason=row.machine_reason,
        person_id=people_by_link.get(row.row_key),
        source=row.decision_source or row.source or "",
        updated_at=row.decided_at or row.updated_at,
        llm_reject=row.machine_reject,
        llm_reject_confidence=(
            None if row.machine_reject_confidence is None else str(row.machine_reject_confidence)
        ),
        llm_reject_reason=row.machine_reject_reason,
        llm_judge_fingerprint=row.judgment_fingerprint,
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
    worth_rows = db.query(
        WORTH_CTE
        + """
SELECT w.*, p.source AS parent_source, p.updated_at AS parent_updated_at
FROM worth w JOIN parents p USING(parent_id)
ORDER BY w.parent_id
"""
    )
    review_rows = [_identity_review_row(row, people_by_link) for row in links]
    review_rows.extend(
        ReviewExportRow(
            key=f"parent-worth:{row['parent_id']}",
            public_identifier=row["public_identifier"],
            llm_worth=row["machine_worth"],
            llm_worth_reason=row["machine_worth_reason"],
            network_worth=row["human_worth"],
            user_worth_note=row["human_worth_note"],
            source=row["human_worth_source"] or row["parent_source"] or "",
            updated_at=row["human_worth_at"] or row["parent_updated_at"],
        )
        for row in worth_rows
    )
    guidance: list[GuidanceSnapshotRow] = []
    for row in db.query("SELECT * FROM guidance ORDER BY submitted_at, handle"):
        detail_payload = parse_json_object(row["detail_json"])
        guidance.append(GuidanceSnapshotRow(
            handle=row["handle"],
            parent_id=row["parent_id"],
            guidance=row["guidance"],
            state=row["state"],
            candidate_key=row["candidate_key"],
            submitted_at=row["submitted_at"],
            applied_url=row["applied_url"],
            detail=_guidance_detail(detail_payload) if detail_payload else None,
        ))
    links_by_key = {row.row_key: row for row in links}
    link_decisions = tuple(
        LinkDecisionSnapshotRow(
            row_key=row.key,
            action=row.action,
            approved=row.approved,
            llm_reject=row.llm_reject,
            llm_judge_fingerprint=row.llm_judge_fingerprint,
            new_linkedin_url=row.new_linkedin_url,
            machine_action=links_by_key[row.key].machine_action,
            machine_approved=links_by_key[row.key].machine_approved,
            machine_proposed_url=links_by_key[row.key].machine_proposed_url,
        )
        for row in review_rows
        if row.key in links_by_key
    )
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
