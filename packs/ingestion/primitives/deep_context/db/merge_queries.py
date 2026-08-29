"""Typed SQLite reads for merge-candidate judging."""

from __future__ import annotations

import json

from packs.ingestion.primitives.common.contact_fields import identifier_emails, identifier_phones
from packs.ingestion.primitives.deep_context.shared.common import normalize_name, phone_digits
from packs.ingestion.primitives.deep_context.db.context_queries import dossier_evidence_rows
from packs.ingestion.primitives.deep_context.db.models import IdentifierKind, PersonRow
from packs.ingestion.primitives.deep_context.db.queries import (
    facts as fact_rows,
    identifiers as identifier_rows,
    parents as parent_rows,
    people as person_rows,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence
from packs.ingestion.primitives.deep_context.synthesis.models import SynthesizedFacts
from packs.ingestion.primitives.deep_context.merge_candidates.models import MergePerson


def merge_people(db: Db) -> list[MergePerson]:
    """Hydrate exactly one merge-judge input per canonical parent."""
    facts = {row.parent_id: row for row in fact_rows(db, parent_owned=True) if row.parent_id}
    identifiers: dict[str, dict[str, list[str]]] = {}
    for row in identifier_rows(db):
        identifiers.setdefault(row.person_id, {}).setdefault(row.kind, []).append(row.normalized_value)
    people_rows = person_rows(db)
    owner_ids = {row.person_id for row in people_rows if row.is_owner}
    owner_emails = {
        value
        for person_id in owner_ids
        for value in identifiers.get(person_id, {}).get(IdentifierKind.EMAIL.value, [])
    }
    owner_phones = {
        phone_digits(value)
        for person_id in owner_ids
        for value in identifiers.get(person_id, {}).get(IdentifierKind.PHONE.value, [])
        if phone_digits(value)
    }
    members: dict[str, list[PersonRow]] = {}
    for person in people_rows:
        members.setdefault(person.parent_id, []).append(person)
    parents = parent_rows(db)
    evidence_rows = dossier_evidence_rows(db, tuple(parent.parent_id for parent in parents))
    people: list[MergePerson] = []
    for parent in parents:
        parent_members = sorted(
            members.get(parent.parent_id, ()),
            key=lambda row: row.person_id,
        )
        fact = facts.get(parent.parent_id)
        if not parent_members or fact is None:
            continue
        member_ids = tuple(row.person_id for row in parent_members)
        representative = parent_members[0]
        try:
            fact_payload = SynthesizedFacts.from_payload(json.loads(fact.facts_json or "{}"))
        except json.JSONDecodeError:
            fact_payload = None
        fact_payload = fact_payload or SynthesizedFacts()
        evidence = DossierEvidence.from_rows((parent.parent_id,), evidence_rows)
        owned = fact_payload.owned_identifiers
        emails = tuple(
            sorted(
                {
                    value
                    for person_id in member_ids
                    for value in identifiers.get(person_id, {}).get(IdentifierKind.EMAIL.value, [])
                }
            )
        )
        phones = tuple(
            sorted(
                {
                    phone_digits(value)
                    for person_id in member_ids
                    for value in identifiers.get(person_id, {}).get(IdentifierKind.PHONE.value, [])
                    if phone_digits(value)
                }
            )
        )
        extra_emails = tuple(sorted(identifier_emails(owned.emails) - set(emails) - owner_emails))
        extra_phones = tuple(sorted(identifier_phones(owned.phones) - set(phones) - owner_phones))
        name = parent.display_name or fact_payload.canonical_name
        people.append(
            MergePerson(
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
        )
    return people
