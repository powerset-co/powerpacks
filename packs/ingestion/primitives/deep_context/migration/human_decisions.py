"""Carry raw human decisions only when original source scope remains unique."""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from enum import StrEnum
from pathlib import Path
import sqlite3

from packs.ingestion.primitives.common.contact_fields import emails_from_row, normalize_name_key, phones_from_row
from packs.ingestion.primitives.deep_context.db.models import (
    CandidatePeopleProjection, CandidatePersonRow, GuidanceRow, HUMAN_DECISION_SOURCES,
    LinkRow, LinkSnapshotRow, ParentSnapshotRow, ReviewExportRow, RowKind,
)
from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.guidance import linkedin_url_in_guidance
from packs.ingestion.primitives.pipeline.contract import PeopleRow
from packs.ingestion.schemas.people_schema import normalize_linkedin_url
from packs.shared.csv_io import CsvIO


class CarryStatus(StrEnum):
    APPLIED = "applied"
    HELD = "held"
    UNMATCHED = "unmatched"


class DecisionKind(StrEnum):
    WORTH = "worth"
    IDENTITY = "identity"


@dataclass(frozen=True)
class DecisionResult:
    kind: DecisionKind
    original_key: str
    person_ids: tuple[str, ...]
    status: CarryStatus
    reason: str
    target_url: str | None = None
    human_worth: str | None = None
    decision_action: str | None = None
    decision_approved: str | None = None
    source: str | None = None
    note: str | None = None
    decided_at: str | None = None


@dataclass(frozen=True)
class HumanSnapshot:
    worth: tuple[ParentSnapshotRow, ...]
    identity: tuple[LinkSnapshotRow, ...]
    parent_people: dict[str, tuple[str, ...]]
    candidate_people: dict[str, tuple[str, ...]]
    source_people: dict[str, PeopleRow]
    seeded: bool
    review: tuple[ReviewExportRow, ...]
    excluded_machine: int
    guidance: tuple[GuidanceRow, ...]

    @classmethod
    def read(cls, connection: sqlite3.Connection, review_csv: Path) -> HumanSnapshot:
        connection.row_factory = sqlite3.Row
        parents = {}
        for row in connection.execute("SELECT parent_id, person_id FROM people ORDER BY person_id"):
            parents.setdefault(row["parent_id"], []).append(row["person_id"])
        candidates = {}
        for row in connection.execute("SELECT row_key, person_id FROM candidate_people ORDER BY person_id"):
            candidates.setdefault(row["row_key"], []).append(row["person_id"])
        review_fields = {field.name for field in fields(ReviewExportRow)} - {"key"}
        review = tuple(ReviewExportRow(
            key=row.get("key") or row.get("public_identifier", ""),
            **{key: value for key, value in row.items() if key in review_fields},
        ) for row in CsvIO.read_dict_rows(review_csv)) if review_csv.is_file() else ()
        worth = [ParentSnapshotRow(**dict(row)) for row in connection.execute(
            "SELECT * FROM parents WHERE human_worth IS NOT NULL ORDER BY parent_id")]
        identity = [LinkSnapshotRow(**dict(row)) for row in connection.execute(
            "SELECT * FROM links WHERE decision_action IS NOT NULL ORDER BY row_key")]
        worth_keys = {row.public_identifier for row in worth}
        identity_keys = {row.row_key for row in identity}
        excluded_machine = 0
        for saved in review:
            if saved.network_worth in {"yes", "no"} and saved.key not in worth_keys:
                person_ids = tuple(sorted(set((saved.worth_person_ids or saved.person_id or "").split("|")) - {""}))
                duplicate = any(tuple(parents.get(row.parent_id, ())) == person_ids
                                and row.human_worth == saved.network_worth and row.human_worth_at == saved.updated_at
                                and row.human_worth_note == (saved.user_worth_note or None) for row in worth)
                if not duplicate:
                    parents[saved.key] = list(person_ids)
                    worth.append(ParentSnapshotRow(
                        saved.key, saved.key, human_worth=saved.network_worth,
                        human_worth_source=saved.source, human_worth_note=saved.user_worth_note or None,
                        human_worth_at=saved.updated_at,
                    ))
            if saved.action in {"verify", "detach", "retarget", "exclude"} and saved.approved in {"yes", "no"}:
                if saved.key in identity_keys:
                    continue
                members = candidates.get(saved.key, [])
                if not members and saved.person_id:
                    members = [saved.person_id]
                if not members and saved.key.startswith("candidate:"):
                    members = [saved.key]
                candidates[saved.key] = members
                raw = connection.execute("SELECT * FROM links WHERE row_key=?", (saved.key,)).fetchone()
                link = LinkSnapshotRow(**dict(raw)) if raw else LinkSnapshotRow(
                    saved.key, "", saved.public_identifier,
                    RowKind.CANDIDATE_EMAIL.value if saved.key.startswith("candidate:email:") else
                    RowKind.CANDIDATE_PHONE.value if saved.key.startswith("candidate:phone:") else RowKind.PUB.value,
                    saved.linkedin_url or None,
                )
                identity.append(replace(link, decision_action=saved.action, decision_approved=saved.approved,
                                        decision_source=saved.source, decided_at=saved.updated_at,
                                        replacement_url=saved.new_linkedin_url or None,
                                        replacement_public_identifier=saved.new_public_identifier or None))
            elif saved.network_worth not in {"yes", "no"}:
                excluded_machine += 1
        return cls(
            tuple(worth), tuple(identity),
            {key: tuple(value) for key, value in parents.items()},
            {key: tuple(value) for key, value in candidates.items()},
            {row["person_id"]: PeopleRow.model_validate_json(row["row_json"])
             for row in connection.execute("SELECT person_id, row_json FROM imported_people")},
            bool(connection.execute("SELECT 1 FROM meta WHERE key IN ('seeded_at', 'legacy_imported_at')").fetchone()),
            review,
            excluded_machine,
            tuple(GuidanceRow(**dict(row)) for row in connection.execute("SELECT * FROM guidance")),
        )


def _scope(snapshot: HumanSnapshot, current: dict[str, PeopleRow], person_ids: tuple[str, ...],
           *, original_person_id: str | None = None) -> tuple[CarryStatus, str]:
    if len(person_ids) != 1:
        return CarryStatus.HELD, "original contact scope is not unique"
    person_id = person_ids[0]
    if person_id not in current:
        return CarryStatus.UNMATCHED, "original contact is absent from fresh sources"
    original = snapshot.source_people.get(original_person_id or person_id)
    if original is None:
        return CarryStatus.HELD, "original source record is unavailable"
    name = normalize_name_key(original.full_name)
    if not name or name != normalize_name_key(current[person_id].full_name):
        return CarryStatus.HELD, "source name changed or is unresolved"
    for identifiers in (emails_from_row, phones_from_row):
        if set(identifiers(original.to_row())) != set(identifiers(current[person_id].to_row())):
            return CarryStatus.HELD, "source endpoints changed"
    return CarryStatus.APPLIED, "exact original contact and source name"


def _explicit_guidance_target(snapshot: HumanSnapshot, row: LinkSnapshotRow) -> bool:
    if row.decision_action not in {"verify", "retarget"} or row.decision_approved != "yes":
        return False
    target = normalize_linkedin_url(row.replacement_url or row.linkedin_url or "")
    guidance = tuple(item for item in snapshot.guidance if item.candidate_key == row.row_key)
    text = row.decision_note or ""
    if guidance:
        if len(guidance) != 1:
            return False
        original = guidance[0]
        if (original.parent_id != row.parent_id or original.state != "applied"
                or normalize_linkedin_url(original.applied_url or "") != target):
            return False
        text = original.guidance
    submitted_url, _ = linkedin_url_in_guidance(text)
    return bool(submitted_url and submitted_url == target)


def _proof(snapshot: HumanSnapshot, person_id: str, row: ParentSnapshotRow | LinkSnapshotRow) -> str | None:
    source = row.human_worth_source if isinstance(row, ParentSnapshotRow) else row.decision_source
    if source not in HUMAN_DECISION_SOURCES:
        return None
    at = row.human_worth_at if isinstance(row, ParentSnapshotRow) else row.decided_at
    if not at:
        return None
    if isinstance(row, LinkSnapshotRow) and source == "user-guidance" and not _explicit_guidance_target(snapshot, row):
        return None
    if isinstance(row, LinkSnapshotRow) and any(
        saved.key == row.row_key and saved.source in HUMAN_DECISION_SOURCES
        and normalize_linkedin_url(saved.linkedin_url or "") != normalize_linkedin_url(row.linkedin_url or "")
        for saved in snapshot.review
    ):
        return None
    if not snapshot.seeded:
        return source
    matches = []
    for saved in snapshot.review:
        scope = tuple(sorted(set((saved.worth_person_ids or saved.person_id or saved.key).split("|")))) if isinstance(row, ParentSnapshotRow) else (saved.person_id or saved.key,)
        if saved.source not in HUMAN_DECISION_SOURCES or scope != (person_id,):
            continue
        if isinstance(row, ParentSnapshotRow):
            mark = saved.network_worth or ("no" if saved.action == "exclude" and saved.approved == "yes" else None)
            if (mark != row.human_worth
                    or saved.updated_at != row.human_worth_at
                    or (saved.user_worth_note or None) != row.human_worth_note):
                continue
        elif (saved.action != row.decision_action or saved.approved != row.decision_approved
              or saved.updated_at != row.decided_at
              or normalize_linkedin_url(saved.linkedin_url or "") != normalize_linkedin_url(row.linkedin_url or "")
              or normalize_linkedin_url(saved.new_linkedin_url or "") != normalize_linkedin_url(row.replacement_url or "")
              or (saved.new_public_identifier or None) != row.replacement_public_identifier):
            continue
        matches.append(saved.source)
    return matches[0] if len(matches) == 1 else None


def carry_human_decisions(db: Db, snapshot: HumanSnapshot) -> tuple[tuple[DecisionResult, ...], int]:
    current = {row.id: row for row in queries.imported_people(db)}
    parents = {row.person_id: row.parent_id for row in queries.people(db)}
    results = []
    worth_choices = {}
    for row in snapshot.worth:
        if row.human_worth_source in HUMAN_DECISION_SOURCES:
            for person_id in snapshot.parent_people.get(row.parent_id, ()):
                worth_choices.setdefault(person_id, set()).add(row.human_worth)
    for row in snapshot.worth:
        person_ids = snapshot.parent_people.get(row.parent_id, ())
        status, reason = _scope(snapshot, current, person_ids)
        source = _proof(snapshot, person_ids[0], row) if status == CarryStatus.APPLIED else None
        if status == CarryStatus.APPLIED and source is None:
            status, reason = CarryStatus.HELD, "original human provenance is unproved"
        if status == CarryStatus.APPLIED and len(worth_choices.get(person_ids[0], ())) > 1:
            status, reason = CarryStatus.HELD, "conflicting human worth choices"
        if status == CarryStatus.APPLIED:
            db.decide_worth(parents[person_ids[0]], row.human_worth, source=source,
                            note=row.human_worth_note, decided_at=row.human_worth_at)
        results.append(DecisionResult(DecisionKind.WORTH, row.parent_id, person_ids, status, reason,
                                      human_worth=row.human_worth, source=source or row.human_worth_source,
                                      note=row.human_worth_note, decided_at=row.human_worth_at))

    # Competing old affirmative choices must not settle each other during carry.
    affirmatives = {}
    identity_choices = {}
    for row in snapshot.identity:
        if row.decision_source in HUMAN_DECISION_SOURCES:
            for person_id in snapshot.candidate_people.get(row.row_key, ()):
                key = (person_id, normalize_linkedin_url(row.linkedin_url or ""))
                identity_choices.setdefault(key, set()).add((row.decision_action, row.decision_approved,
                                                             normalize_linkedin_url(row.replacement_url or "")))
        if row.decision_source in HUMAN_DECISION_SOURCES and row.decision_approved == "yes" and row.decision_action in {"verify", "retarget"}:
            for person_id in snapshot.candidate_people.get(row.row_key, ()):
                affirmatives.setdefault(person_id, set()).add(normalize_linkedin_url(row.replacement_url or row.linkedin_url or ""))
    excluded_generated = 0
    for row in snapshot.identity:
        if row.decision_source == "sibling-settle":
            excluded_generated += 1
            continue
        person_ids = snapshot.candidate_people.get(row.row_key, ())
        status, reason = _scope(snapshot, current, person_ids)
        source = _proof(snapshot, person_ids[0], row) if status == CarryStatus.APPLIED else None
        if status == CarryStatus.APPLIED and source is None:
            status, reason = CarryStatus.HELD, "original human provenance is unproved"
        if status == CarryStatus.APPLIED and len(affirmatives.get(person_ids[0], ())) > 1:
            status, reason = CarryStatus.HELD, "conflicting human profile choices"
        if status == CarryStatus.APPLIED and len(identity_choices.get((person_ids[0], normalize_linkedin_url(row.linkedin_url or "")), ())) > 1:
            status, reason = CarryStatus.HELD, "conflicting human profile choices"
        if status == CarryStatus.APPLIED and row.kind == RowKind.SYNTHETIC.value:
            status, reason = CarryStatus.HELD, "synthetic profile has no exact reusable target"
        if status == CarryStatus.APPLIED and row.decision_action != "exclude" and not row.linkedin_url:
            status, reason = CarryStatus.HELD, "original profile target is unavailable"
        if status == CarryStatus.APPLIED:
            person_id = person_ids[0]
            parent_id = parents[person_id]
            key = row.row_key
            existing = db.query("SELECT parent_id, linkedin_url FROM links WHERE row_key=?", (key,))
            if existing and (existing[0]["parent_id"] != parent_id or normalize_linkedin_url(existing[0]["linkedin_url"] or "") != normalize_linkedin_url(row.linkedin_url or "")):
                status, reason = CarryStatus.HELD, "candidate target changed in fresh sources"
            else:
                if not existing:
                    db.project_rows((
                        LinkRow(key, parent_id, row.public_identifier, row.kind, row.linkedin_url,
                                source=source),
                        CandidatePeopleProjection(key, (CandidatePersonRow(key, person_id, parent_id),)),
                    ))
                db.decide_identity(key, row.decision_action, approved=row.decision_approved,
                                   replacement_url=row.replacement_url,
                                   replacement_public_identifier=row.replacement_public_identifier,
                                   source=source, note=row.decision_note, decided_at=row.decided_at)
        clicked = next((saved for saved in snapshot.review if saved.key == row.row_key), None)
        target = (clicked.new_linkedin_url or clicked.linkedin_url) if clicked else row.replacement_url or row.linkedin_url
        results.append(DecisionResult(DecisionKind.IDENTITY, row.row_key, person_ids, status, reason, target,
                                      decision_action=row.decision_action, decision_approved=row.decision_approved,
                                      source=source or row.decision_source, note=row.decision_note, decided_at=row.decided_at))
    return tuple(results), excluded_generated
