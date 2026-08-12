"""Narrow typed reads for candidate, research, guidance, and review state."""

from __future__ import annotations

import json
from collections.abc import Sequence

from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context.db._view_sql import WORTH_CTE
from packs.ingestion.primitives.deep_context.db.identity_policy import IdentityPolicy
from packs.ingestion.primitives.deep_context.db.models import (
    CandidatePersonRow,
    GuidanceDetailSnapshot,
    GuidanceRequestSnapshot,
    GuidanceSnapshotRow,
    IdentifierKind,
    LinkSnapshotRow,
    ResearchRow,
    ReviewExportRow,
    SyntheticProfileRow,
)
from packs.ingestion.primitives.deep_context.db.queries import typed_rows
from packs.ingestion.primitives.deep_context.db.store import Db


def links(
    db: Db,
    *,
    row_key: str | None = None,
    row_keys: Sequence[str] | None = None,
    parent_id: str | None = None,
    parent_ids: Sequence[str] | None = None,
    kind: str | None = None,
) -> tuple[LinkSnapshotRow, ...]:
    clauses: list[str] = []
    params: list[str] = []
    selected_row_keys = (row_key,) if row_key is not None else row_keys
    if selected_row_keys is not None:
        selected = tuple(dict.fromkeys(selected_row_keys))
        if not selected:
            return ()
        placeholders = ",".join("?" for _ in selected)
        clauses.append(f"row_key IN ({placeholders})")
        params.extend(selected)
    selected_parent_ids = (parent_id,) if parent_id is not None else parent_ids
    if selected_parent_ids is not None:
        selected = tuple(dict.fromkeys(selected_parent_ids))
        if not selected:
            return ()
        placeholders = ",".join("?" for _ in selected)
        clauses.append(f"parent_id IN ({placeholders})")
        params.extend(selected)
    if kind is not None:
        clauses.append("kind=?")
        params.append(kind)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return typed_rows(
        db,
        f"SELECT * FROM links{where} ORDER BY row_key",
        LinkSnapshotRow,
        tuple(params),
    )


def memberships(
    db: Db,
    *,
    row_key: str | None = None,
    parent_id: str | None = None,
) -> tuple[CandidatePersonRow, ...]:
    if parent_id is not None:
        return typed_rows(
            db,
            "SELECT cp.* FROM candidate_people cp WHERE cp.parent_id=? ORDER BY cp.row_key, cp.person_id",
            CandidatePersonRow,
            (parent_id,),
        )
    where = " WHERE row_key=?" if row_key is not None else ""
    params = (row_key,) if row_key is not None else ()
    return typed_rows(
        db,
        f"SELECT * FROM candidate_people{where} ORDER BY row_key, person_id",
        CandidatePersonRow,
        params,
    )


def research_rows(
    db: Db,
    *,
    handle: str | None = None,
    parent_id: str | None = None,
) -> tuple[ResearchRow, ...]:
    if handle is not None:
        return typed_rows(
            db,
            "SELECT * FROM research WHERE handle=? ORDER BY handle",
            ResearchRow,
            (handle,),
        )
    where = " WHERE parent_id=?" if parent_id is not None else ""
    params = (parent_id,) if parent_id is not None else ()
    return typed_rows(
        db,
        f"SELECT * FROM research{where} ORDER BY handle",
        ResearchRow,
        params,
    )


def synthetic_profiles(
    db: Db,
    *,
    candidate_key: str | None = None,
) -> tuple[SyntheticProfileRow, ...]:
    where = " WHERE candidate_key=?" if candidate_key is not None else ""
    params = (candidate_key,) if candidate_key is not None else ()
    return typed_rows(
        db,
        f"SELECT * FROM synthetic_profiles{where} ORDER BY public_identifier",
        SyntheticProfileRow,
        params,
    )


def parent_has_contact_identifier(db: Db, parent_id: str) -> bool:
    """True when any non-owner member of the parent has an email or phone.

    The same identifier source the enrichment-queue view reads: URL-less guided
    research is addressed by one of these, so guidance intake gates on this
    predicate before persisting a request.
    """
    return bool(db.query(
        """
SELECT 1 FROM people pe JOIN person_identifiers i USING(person_id)
WHERE pe.parent_id=? AND pe.is_owner=0 AND i.kind IN (?, ?)
LIMIT 1
""",
        (parent_id, IdentifierKind.EMAIL.value, IdentifierKind.PHONE.value),
    ))


def _optional_text(value: object) -> str | None:
    text = str(value or "")
    return text or None


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
        "slug",
        "row_key",
        "name",
        "guidance",
        "state",
        "detail",
        "submitted_at",
        "updated_at",
        "new_url",
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


def guidance_rows(db: Db) -> tuple[GuidanceSnapshotRow, ...]:
    result: list[GuidanceSnapshotRow] = []
    for row in db.query("SELECT * FROM guidance ORDER BY submitted_at, handle"):
        detail_payload = parse_json_object(row["detail_json"])
        result.append(
            GuidanceSnapshotRow(
                handle=row["handle"],
                parent_id=row["parent_id"],
                guidance=row["guidance"],
                state=row["state"],
                candidate_key=row["candidate_key"],
                submitted_at=row["submitted_at"],
                applied_url=row["applied_url"],
                detail=_guidance_detail(detail_payload) if detail_payload else None,
            )
        )
    return tuple(result)


def _review_row(row: LinkSnapshotRow, person_id: str | None) -> ReviewExportRow:
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
        person_id=person_id,
        source=row.decision_source or row.source or "",
        updated_at=row.decided_at or row.updated_at,
        llm_reject=row.machine_reject,
        llm_reject_confidence=(None if row.machine_reject_confidence is None else str(row.machine_reject_confidence)),
        llm_reject_reason=row.machine_reject_reason,
        llm_judge_fingerprint=row.judgment_fingerprint,
    )


def review_rows(
    db: Db,
    *,
    key: str | None = None,
    include_worth: bool = True,
) -> tuple[ReviewExportRow, ...]:
    selected_links = (
        links(db, row_key=key) if key and not key.startswith("parent-worth:") else (() if key else links(db))
    )
    selected_keys = {row.row_key for row in selected_links}
    membership_rows = (
        () if not selected_links else (memberships(db, row_key=key) if key in selected_keys else memberships(db))
    )
    people_by_link: dict[str, str] = {}
    for membership in membership_rows:
        if membership.row_key in selected_keys:
            people_by_link.setdefault(membership.row_key, membership.person_id)
    result = [_review_row(row, people_by_link.get(row.row_key)) for row in selected_links]
    if include_worth and (key is None or key.startswith("parent-worth:")):
        parent_id = key.removeprefix("parent-worth:") if key else None
        where = "WHERE w.parent_id=?" if parent_id else ""
        params = (parent_id,) if parent_id else ()
        worth_rows = db.query(
            WORTH_CTE
            + f"""
SELECT w.*, p.source AS parent_source, p.updated_at AS parent_updated_at
FROM worth w JOIN parents p USING(parent_id)
{where}
ORDER BY w.parent_id
""",
            params,
        )
        result.extend(
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
    return tuple(result)
