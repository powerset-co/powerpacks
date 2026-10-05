"""Read operator feedback and restore only explicit, uniquely scoped human choices."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from packs.ingestion.primitives.common.contact_fields import (
    emails_from_row, normalize_email, normalize_phone, phones_from_row,
)
from packs.ingestion.primitives.deep_context.db import identity_queries, queries
from packs.ingestion.primitives.deep_context.db.models import (
    CandidatePeopleProjection, CandidatePersonRow, LinkRow, ReviewSource, RowKind,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.guidance import linkedin_url_in_guidance
from packs.ingestion.primitives.deep_context.migration.human_decisions import (
    CarryStatus, DecisionKind, DecisionResult, HumanSnapshot, _proof, _scope,
)
from packs.ingestion.primitives.pipeline.contract import PeopleRow
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url
from packs.powerset.primitives.pull_runtime_keys.pull_runtime_keys import api_base, bearer_token, fetch_endpoint
from packs.shared.csv_io import CsvIO


PAGE_SIZE = 200
FEEDBACK_COLUMNS = (
    "feedback_id", "operator_id", "person_ids", "candidate_key", "valid_linkedin_url",
    "resolution_action", "resolution_person_ids", "resolved_at", "resolved_by", "resolution_note", "original_guidance",
)


class FeedbackGuidance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")
    guidance: str = ""


class FeedbackMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")
    source: str | None = None
    action: str | None = None
    person_ids: tuple[str, ...] = ()
    candidate_key: str | None = None
    public_identifier: str | None = None
    linkedin_url: str | None = None
    match_emails: tuple[str, ...] = ()
    match_phones: tuple[str, ...] = ()
    retarget_guidance: tuple[FeedbackGuidance, ...] = ()


class FeedbackRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow", str_strip_whitespace=True)
    id: str
    operator_id: str
    feedback_type: str
    created_at: AwareDatetime
    comment: str | None = None
    metadata: FeedbackMetadata | None = None
    valid_linkedin_url: str | None = None
    resolution_action: Literal["linkedin", "synthetic", "exclude", "unresolved"] | None = None
    resolution_person_ids: tuple[str, ...] | None = Field(default=None, min_length=1)
    resolution_note: str | None = None
    resolved_by: str | None = Field(default=None, min_length=1)
    resolved_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _resolved_target(self) -> FeedbackRow:
        if self.resolution_action not in {None, "unresolved"} and (self.resolved_by is None or self.resolved_at is None):
            raise ValueError("support resolution requires its actor and timestamp")
        if self.valid_linkedin_url is not None and self.resolution_action != "linkedin":
            raise ValueError("valid LinkedIn URL requires a support LinkedIn resolution")
        if self.resolution_action == "linkedin" and not linkedin_url_in_guidance(self.valid_linkedin_url or "")[0]:
            raise ValueError("LinkedIn resolution requires a valid profile URL")
        return self

    @property
    def decision_at(self) -> datetime:
        return self.resolved_at or self.created_at


@dataclass(frozen=True)
class FeedbackSnapshot:
    operator_id: str
    rows: tuple[FeedbackRow, ...]
    raw: tuple[dict[str, Any], ...]


def read_feedback(operator_id: str, *, feedback_json: Path | None = None,
                  env_file: Path | None = None) -> FeedbackSnapshot:
    operator_id = str(UUID(operator_id))
    if feedback_json is not None:
        raw = json.loads(Path(feedback_json).read_text())
    else:
        base = api_base(env_file)
        try:
            token = bearer_token(env_file)
        except SystemExit as exc:
            raise StoreError(str(exc)) from exc
        status, summary = fetch_endpoint(base, "/v2/contact-datalake/summary", token)
        if status != "ok" or not summary or summary.get("operator_id") != operator_id:
            raise StoreError("authenticated operator does not match the recovery operator")
        raw = []
        while True:
            status, page = fetch_endpoint(base,
                f"/v2/feedback?limit={PAGE_SIZE}&offset={len(raw)}", token)
            if status != "ok" or not isinstance(page, list):
                raise StoreError("operator feedback could not be read; recovery has not started")
            if any(str(item.get("operator_id")) != operator_id for item in page):
                raise StoreError("feedback operator does not match the recovery operator")
            raw.extend(page)
            if len(page) < PAGE_SIZE:
                break
    if not isinstance(raw, list):
        raise StoreError("feedback JSON must contain the API response row list")
    rows = tuple(FeedbackRow.model_validate(item) for item in raw)
    if any(row.operator_id != operator_id for row in rows):
        raise StoreError("feedback operator does not match the recovery operator")
    return FeedbackSnapshot(operator_id, rows, tuple(raw))


def write_feedback_csv(path: Path, feedback: FeedbackSnapshot) -> None:
    rows = []
    for row in feedback.rows:
        metadata = row.metadata or FeedbackMetadata()
        if not (row.resolution_action not in {None, "unresolved"} or metadata.source == "powerpacks-directory"
                and metadata.action in {"retarget", "detach"}):
            continue
        rows.append({
            "feedback_id": row.id, "operator_id": row.operator_id,
            "person_ids": "|".join(metadata.person_ids),
            "candidate_key": metadata.candidate_key or "",
            "valid_linkedin_url": row.valid_linkedin_url or "",
            "resolution_action": row.resolution_action or "unresolved",
            "resolution_person_ids": "|".join(row.resolution_person_ids or ()),
            "resolved_at": row.resolved_at.isoformat() if row.resolved_at else "",
            "resolved_by": row.resolved_by or "",
            "resolution_note": row.resolution_note or "",
            "original_guidance": "\n".join(dict.fromkeys((row.comment or "", *(item.guidance for item in metadata.retarget_guidance)))),
        })
    CsvIO.write_dict_rows(path, FEEDBACK_COLUMNS, rows)


def _contact(metadata: FeedbackMetadata, snapshot: HumanSnapshot,
             current: dict[str, PeopleRow]) -> tuple[tuple[str, ...], CarryStatus, str]:
    ids = metadata.person_ids
    key = metadata.candidate_key or metadata.public_identifier or ""
    if not ids and key.startswith("candidate:"):
        ids = snapshot.candidate_people.get(key, (key,))
    if not ids and metadata.candidate_key:
        ids = snapshot.candidate_people.get(metadata.candidate_key, ())
    if ids:
        if len(ids) != 1:
            return (), CarryStatus.HELD, "feedback contact scope is not unique"
        original_id = ids[0]
        original = snapshot.source_people.get(original_id)
        if original and "linkedin_csv" in original.source_channels.split(","):
            return ids, CarryStatus.HELD, "LinkedIn or combined source row does not prove contact ownership"
        if original_id in current:
            status, reason = _scope(snapshot, current, (original_id,))
            return (original_id,), status, reason
        if original is None or not (emails_from_row(original.to_row()) or phones_from_row(original.to_row())):
            return (), CarryStatus.HELD, "original feedback contact endpoints are unavailable"
        matches = tuple(person_id for person_id in current
                        if _scope(snapshot, current, (person_id,), original_person_id=original_id)[0] == CarryStatus.APPLIED)
        if len(matches) != 1:
            return matches, CarryStatus.HELD, "original source endpoints and name do not identify one fresh contact"
        return matches, CarryStatus.APPLIED, "exact original source endpoints and name"
    endpoints = [(normalize_email(value), emails_from_row) for value in metadata.match_emails]
    endpoints.extend((normalize_phone(value), phones_from_row) for value in metadata.match_phones)
    if not endpoints:
        return (), CarryStatus.HELD, "feedback lacks exact contact scope"
    matches = tuple(person_id for person_id, person in current.items()
                    if all(value and value in identifiers(person.to_row()) for value, identifiers in endpoints))
    if len(matches) != 1:
        return matches, CarryStatus.HELD, "feedback endpoints do not identify one fresh contact"
    return matches, CarryStatus.APPLIED, "explicit unique source endpoints"


def apply_feedback(db: Db, feedback: FeedbackSnapshot, original: HumanSnapshot) -> tuple[DecisionResult, ...]:
    rows = []
    for row in feedback.rows:
        if row.resolution_action not in {None, "unresolved"} and row.resolution_person_ids is not None:
            metadata = row.metadata or FeedbackMetadata()
            rows.extend(row.model_copy(update={"metadata": metadata.model_copy(update={"person_ids": (person_id,)})})
                        for person_id in row.resolution_person_ids)
        else:
            rows.append(row)
    return _apply_feedback(db, tuple(rows), original)


def _apply_feedback(db: Db, rows: tuple[FeedbackRow, ...], original: HumanSnapshot) -> tuple[DecisionResult, ...]:
    current = {row.id: row for row in queries.imported_people(db)}
    parents = {row.person_id: row.parent_id for row in queries.people(db)}
    members = {}
    for person_id, parent_id in parents.items():
        members.setdefault(parent_id, []).append(person_id)
    original_choices = {}
    for link in original.identity:
        if link.decision_source not in {ReviewSource.REVIEW.value, ReviewSource.USER_GUIDANCE.value}:
            continue
        scope = original.candidate_people.get(link.row_key, ())
        for person_id in scope:
            if _proof(original, person_id, link) is not None:
                original_choices.setdefault(person_id, []).append((link, scope))
    times = {}
    for link in (*original.identity, *identity_queries.links(db)):
        if not link.decision_action or link.decision_source == "sibling-settle":
            continue
        try:
            at = datetime.fromisoformat(link.decided_at.replace("Z", "+00:00")) if link.decided_at else None
            times[(link.row_key, link.decided_at)] = at if at and at.tzinfo else None
        except ValueError:
            times[(link.row_key, link.decided_at)] = None
    results = []
    for row in sorted(rows, key=lambda item: item.decision_at):
        metadata = row.metadata or FeedbackMetadata()
        if not (row.resolution_action not in {None, "unresolved"} or metadata.source == "powerpacks-directory"
                and metadata.action in {"retarget", "detach"}):
            continue
        target = linkedin_url_in_guidance(row.valid_linkedin_url or "")[0]
        decided_at = row.decision_at
        old_url = linkedin_url_in_guidance(metadata.linkedin_url or "")[0]
        action = {"linkedin": "retarget", "synthetic": "verify", "exclude": "exclude", "unresolved": None}.get(
            row.resolution_action or "unresolved")
        note = "\n".join(dict.fromkeys(text for text in (row.comment or "", *(item.guidance for item in metadata.retarget_guidance)) if text))
        person_ids, status, reason = _contact(metadata, original, current)
        if status == CarryStatus.APPLIED and action is None:
            status, reason = CarryStatus.HELD, "feedback has no resolved LinkedIn mapping"
        if status == CarryStatus.APPLIED and len(members[parents[person_ids[0]]]) != 1:
            status, reason = CarryStatus.HELD, "fresh contact is not separate from other source contacts"
        if status == CarryStatus.APPLIED:
            person_id = person_ids[0]
            parent_id = parents[person_id]
            key = f"feedback:{row.id}:{person_id}"
            current_choices = [link for link in identity_queries.links(db, parent_id=parent_id)
                               if link.decision_action and link.decision_source != "sibling-settle"]
            original_ids = set(metadata.person_ids) | set(person_ids)
            original_ids.update(original_id for original_id in original.source_people
                                if _scope(original, current, person_ids, original_person_id=original_id)[0] == CarryStatus.APPLIED)
            prior = [item for original_id in original_ids
                     for item in original_choices.get(original_id, ())]
            choices = [*current_choices, *(link for link, _ in prior)]
            current_worth = queries.parents(db, parent_id=parent_id)[0]
            worth = [current_worth] if current_worth.human_worth is not None else []
            worth.extend(parent for parent in original.worth
                         if any(person_id in original_ids and _proof(original, person_id, parent) is not None
                                for person_id in original.parent_people.get(parent.parent_id, ())))
            worth_times = []
            for parent in worth if action == "exclude" else ():
                try:
                    at = datetime.fromisoformat(parent.human_worth_at.replace("Z", "+00:00")) if parent.human_worth_at else None
                    worth_times.append(at if at and at.tzinfo else None)
                except ValueError:
                    worth_times.append(None)
            already_excluded = (
                action == "exclude" and current_worth.human_worth == "no"
                and current_worth.human_worth_at == decided_at.isoformat()
                and any(link.row_key == key and link.decision_action == "exclude"
                        and link.decision_approved == "yes"
                        and times[(link.row_key, link.decided_at)] == decided_at for link in current_choices)
            )
            if any(at is None or at > decided_at or at == decided_at
                   and (not already_excluded or parent.human_worth != "no")
                   for parent, at in zip(worth, worth_times)):
                status, reason = CarryStatus.HELD, "newer or undated human worth choice is preserved"
            elif any(times[(link.row_key, link.decided_at)] is None for link in choices):
                status, reason = CarryStatus.HELD, "existing human identity choice lacks a timestamp"
            elif any(len(scope) != 1 and times[(link.row_key, link.decided_at)] >= decided_at for link, scope in prior):
                status, reason = CarryStatus.HELD, "newer original human choice has ambiguous contact scope"
            elif already_excluded and all(times[(link.row_key, link.decided_at)] <= decided_at for link in choices):
                status, reason = CarryStatus.APPLIED, "already applied"
            elif newer := [link for link in choices
                           if times[(link.row_key, link.decided_at)] >= decided_at]:
                same = all(link.decision_action == action and link.decision_approved == "yes"
                           and (link.kind == "synthetic" if row.resolution_action == "synthetic" else
                                normalize_linkedin_url(link.replacement_url or link.linkedin_url or "") == (target or old_url))
                           for link in newer)
                same = same and any(link in current_choices for link in newer)
                status = CarryStatus.APPLIED if same else CarryStatus.HELD
                reason = "already applied; newer human choice preserved" if same else "newer original or current choice is preserved; feedback not applied"
            else:
                synthetic = row.resolution_action == "synthetic"
                kind = RowKind.SYNTHETIC.value if synthetic else RowKind.RESEARCH.value
                db.project_rows((
                    LinkRow(key, parent_id, key if synthetic or not (old_url or target) else extract_public_identifier(old_url or target), kind,
                            None if synthetic else old_url or None, display_name=current[person_id].full_name,
                            candidate_origin=True, source=ReviewSource.REVIEW.value),
                    CandidatePeopleProjection(key, (CandidatePersonRow(key, person_id, parent_id),)),
                ))
                db.decide_identity(key, action, replacement_url=target if action == "retarget" else None,
                                   replacement_public_identifier=extract_public_identifier(target) if action == "retarget" else None,
                                   source=ReviewSource.REVIEW.value, note=note or None, decided_at=decided_at.isoformat())
                if action == "exclude":
                    db.decide_worth(parent_id, "no", source=ReviewSource.REVIEW.value,
                                    note=note or None, decided_at=decided_at.isoformat())
                times[(key, decided_at.isoformat())] = decided_at
        results.append(DecisionResult(
            DecisionKind.IDENTITY, row.id, person_ids, status, reason, target or old_url or None,
            decision_action=action, decision_approved="yes" if action else None,
            source=ReviewSource.REVIEW.value, note=note or None, decided_at=decided_at.isoformat(),
        ))
    return tuple(results)
