"""Typed merge-candidate people hydrated from canonical SQLite."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from packs.ingestion.primitives.deep_context.common import normalize_name, phone_digits
from packs.ingestion.primitives.deep_context.db.models import IdentifierKind
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.dossier_evidence import DossierEvidence


@dataclass(frozen=True)
class MergePerson:
    slug: str
    person_id: str
    name: str
    name_key: str
    emails: tuple[str, ...] = ()
    extra_emails: tuple[str, ...] = ()
    phone_digits: tuple[str, ...] = ()
    extra_phones: tuple[str, ...] = ()
    evidence: DossierEvidence = field(default_factory=DossierEvidence)


def identifier_emails(identifiers: list[str]) -> set[str]:
    values = (str(identifier).strip() for identifier in identifiers or [])
    return {
        value.lower()
        for value in values
        if "@" in value and "." in value.rsplit("@", 1)[-1]
    }


def identifier_phones(identifiers: list[str]) -> set[str]:
    phones: set[str] = set()
    for raw in identifiers or []:
        value = str(raw).strip()
        if not value or "@" in value or re.search(r"[a-z]{2,}\.[a-z]{2,}", value.lower()):
            continue
        digits = phone_digits(value)
        if 7 <= len(digits) <= 15:
            phones.add(digits)
    return phones


def load_people(db: Db) -> list[MergePerson]:
    """Hydrate the prior merge-judge input from one typed DB snapshot."""
    snapshot = canonical_snapshot(db)
    facts = {row.person_id: row for row in snapshot.facts if row.person_id}
    identifiers: dict[str, dict[str, list[str]]] = {}
    for row in snapshot.identifiers:
        identifiers.setdefault(row.person_id, {}).setdefault(row.kind, []).append(
            row.normalized_value
        )
    owner_ids = {row.person_id for row in snapshot.people if row.is_owner}
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
    people: list[MergePerson] = []
    for person in snapshot.people:
        fact = facts.get(person.person_id)
        if fact is None:
            continue
        try:
            fact_payload = json.loads(fact.facts_json or "{}")
        except json.JSONDecodeError:
            fact_payload = {}
        evidence = DossierEvidence.from_snapshot([person.person_id], snapshot)
        owned = fact_payload.get("owned_identifiers") or {}
        emails = tuple(sorted(identifiers.get(person.person_id, {}).get(
            IdentifierKind.EMAIL.value, []
        )))
        phones = tuple(sorted({
            phone_digits(value)
            for value in identifiers.get(person.person_id, {}).get(
                IdentifierKind.PHONE.value, []
            )
            if phone_digits(value)
        }))
        extra_emails = tuple(sorted(
            identifier_emails(owned.get("emails") or []) - set(emails) - owner_emails
        ))
        extra_phones = tuple(sorted(
            identifier_phones(owned.get("phones") or []) - set(phones) - owner_phones
        ))
        name = person.display_name or str(fact_payload.get("canonical_name") or "")
        people.append(MergePerson(
            slug=person.child_slug or person.person_id,
            person_id=person.person_id,
            name=name,
            name_key=normalize_name(name),
            emails=emails,
            extra_emails=extra_emails,
            phone_digits=phones,
            extra_phones=extra_phones,
            evidence=evidence,
        ))
    return people


def all_emails(person: MergePerson) -> set[str]:
    return set(person.emails) | set(person.extra_emails)


def all_phones(person: MergePerson) -> set[str]:
    return set(person.phone_digits) | set(person.extra_phones)


def fmt_phone(digits: str) -> str:
    if len(digits) == 10:
        return f"+1 ({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return f"+{digits}"
