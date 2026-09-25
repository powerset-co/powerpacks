"""Typed SQLite reads for merge-candidate judging.

Changelog:
- 2026-09-25: evidence is read in MERGE_SURVEY_BATCH-parent batches and grouped per
  parent once; the whole-install packet no longer sits in memory or gets
  rescanned per parent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from packs.ingestion.primitives.common.contact_fields import identifier_emails, identifier_phones
from packs.ingestion.primitives.deep_context.shared.common import normalize_name, phone_digits
from packs.ingestion.primitives.deep_context.db.context_queries import dossier_evidence_rows
from packs.ingestion.primitives.deep_context.db.models import FactRow, IdentifierKind, ParentSnapshotRow, PersonRow
from packs.ingestion.primitives.deep_context.db.queries import (
    facts as fact_rows,
    identifiers as identifier_rows,
    parents as parent_rows,
    people as person_rows,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.view_models import DossierEvidenceRows
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence
from packs.ingestion.primitives.deep_context.synthesis.models import SynthesizedFacts
from packs.ingestion.primitives.deep_context.merge_candidates.models import MergePerson


# Parents per evidence read. Source bundles carry message text, so one batch of
# evidence is the peak the survey holds; the MergePerson list keeps only
# identifiers and rendered samples.
MERGE_SURVEY_BATCH = 500

_NO_ROWS = DossierEvidenceRows((), (), (), (), ())


def _evidence_by_parent(rows: DossierEvidenceRows) -> dict[str, DossierEvidenceRows]:
    """Split one batch's family rows into each parent's own narrow packet.

    Row order within every tuple is the query's, so `DossierEvidence.from_rows`
    sees exactly what the single-parent read would have given it."""
    parent_of_person = {row.person_id: row.parent_id for row in rows.people}
    grouped: dict[str, dict[str, list]] = {}

    def bucket(parent_id: str, field: str, row: object) -> None:
        grouped.setdefault(parent_id, {}).setdefault(field, []).append(row)

    for row in rows.parents:
        bucket(row.parent_id, "parents", row)
    for row in rows.people:
        bucket(row.parent_id, "people", row)
    for row in rows.facts:
        bucket(row.parent_id, "facts", row)
    for row in rows.source_bundles:
        bucket(row.parent_id, "source_bundles", row)
    for row in rows.identifiers:
        bucket(parent_of_person[row.person_id], "identifiers", row)
    return {
        parent_id: DossierEvidenceRows(
            tuple(fields.get("parents", ())),
            tuple(fields.get("people", ())),
            tuple(fields.get("facts", ())),
            tuple(fields.get("source_bundles", ())),
            tuple(fields.get("identifiers", ())),
        )
        for parent_id, fields in grouped.items()
    }


def merge_people(db: Db) -> list[MergePerson]:
    """Hydrate exactly one merge-judge input per canonical parent.

    Evidence is read MERGE_SURVEY_BATCH parents at a time and dropped before
    the next batch, so source-bundle text is held one batch at a time; the
    returned MergePerson list (identifiers and rendered samples) grows with
    the install, as the pair judge needs every person."""
    roster = _Roster.load(db)
    parents = parent_rows(db)
    people: list[MergePerson] = []
    for start in range(0, len(parents), MERGE_SURVEY_BATCH):
        batch = parents[start:start + MERGE_SURVEY_BATCH]
        by_parent = _evidence_by_parent(
            dossier_evidence_rows(db, tuple(parent.parent_id for parent in batch))
        )
        for parent in batch:
            person = _merge_person(parent, roster, by_parent.get(parent.parent_id, _NO_ROWS))
            if person is not None:
                people.append(person)
    return people


@dataclass(frozen=True)
class _Roster:
    """The install-wide lookups every merge person shares, read once: facts,
    identifiers and members for every person (tens of MB at 40k people)."""

    facts: dict[str, FactRow]
    identifiers: dict[str, dict[str, list[str]]]
    members: dict[str, list[PersonRow]]
    owner_emails: set[str]
    owner_phones: set[str]

    @classmethod
    def load(cls, db: Db) -> _Roster:
        identifiers: dict[str, dict[str, list[str]]] = {}
        for row in identifier_rows(db):
            identifiers.setdefault(row.person_id, {}).setdefault(row.kind, []).append(row.normalized_value)
        people_rows = person_rows(db)
        owner_ids = {row.person_id for row in people_rows if row.is_owner}
        members: dict[str, list[PersonRow]] = {}
        for person in people_rows:
            members.setdefault(person.parent_id, []).append(person)
        return cls(
            facts={row.parent_id: row for row in fact_rows(db, parent_owned=True) if row.parent_id},
            identifiers=identifiers,
            members=members,
            owner_emails={
                value
                for person_id in owner_ids
                for value in identifiers.get(person_id, {}).get(IdentifierKind.EMAIL.value, [])
            },
            owner_phones={
                phone_digits(value)
                for person_id in owner_ids
                for value in identifiers.get(person_id, {}).get(IdentifierKind.PHONE.value, [])
                if phone_digits(value)
            },
        )


def _merge_person(parent: ParentSnapshotRow, roster: _Roster, evidence_rows: DossierEvidenceRows) -> MergePerson | None:
    parent_members = sorted(roster.members.get(parent.parent_id, ()), key=lambda row: row.person_id)
    fact = roster.facts.get(parent.parent_id)
    if not parent_members or fact is None:
        return None
    member_ids = tuple(row.person_id for row in parent_members)
    representative = parent_members[0]
    try:
        fact_payload = SynthesizedFacts.from_payload(json.loads(fact.facts_json or "{}"))
    except json.JSONDecodeError:
        fact_payload = None
    fact_payload = fact_payload or SynthesizedFacts()
    evidence = DossierEvidence.from_rows((parent.parent_id,), evidence_rows)
    owned = fact_payload.owned_identifiers
    emails = tuple(sorted({
        value
        for person_id in member_ids
        for value in roster.identifiers.get(person_id, {}).get(IdentifierKind.EMAIL.value, [])
    }))
    phones = tuple(sorted({
        phone_digits(value)
        for person_id in member_ids
        for value in roster.identifiers.get(person_id, {}).get(IdentifierKind.PHONE.value, [])
        if phone_digits(value)
    }))
    extra_emails = tuple(sorted(identifier_emails(owned.emails) - set(emails) - roster.owner_emails))
    extra_phones = tuple(sorted(identifier_phones(owned.phones) - set(phones) - roster.owner_phones))
    name = parent.display_name or fact_payload.canonical_name
    return MergePerson(
        parent_id=parent.parent_id,
        slug=parent.display_slug or representative.child_slug or parent.parent_id,
        person_id=representative.person_id,
        member_person_ids=member_ids,
        name=name,
        name_key=normalize_name(name),
        emails=emails,
        extra_emails=extra_emails,
        phone_digits=phones,
        extra_phones=extra_phones,
        evidence=evidence,
    )
