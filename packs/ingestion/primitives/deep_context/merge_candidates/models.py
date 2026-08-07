"""Typed merge-candidate people hydrated from canonical SQLite."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from packs.ingestion.primitives.deep_context.common import normalize_name, phone_digits
from packs.ingestion.primitives.deep_context.db.models import IdentifierKind
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.dossier_evidence import DossierEvidence
from packs.ingestion.primitives.deep_context.dossier.models import SynthesizedFacts


@dataclass(frozen=True)
class MergePerson:
    """One canonical parent; ``person_id`` is its schema-v8 cache anchor child."""

    slug: str
    person_id: str
    name: str
    name_key: str
    parent_id: str = ""
    member_person_ids: tuple[str, ...] = ()
    emails: tuple[str, ...] = ()
    extra_emails: tuple[str, ...] = ()
    phone_digits: tuple[str, ...] = ()
    extra_phones: tuple[str, ...] = ()
    evidence: DossierEvidence = field(default_factory=DossierEvidence)


@dataclass(frozen=True)
class MergeDecision:
    """One parsed deterministic or LLM same-person decision."""

    same_person: bool
    confidence: float
    tone_consistent: bool
    reason: str
    judge: str

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        judge: str,
    ) -> MergeDecision:
        """Parse the raw judge response once at the provider/cache boundary."""
        return cls(
            same_person=bool(payload.get("same_person")),
            confidence=float(payload.get("confidence") or 0),
            tone_consistent=bool(payload.get("tone_consistent")),
            reason=str(payload.get("reason") or ""),
            judge=judge,
        )


@dataclass(frozen=True)
class MergePairVerdict:
    left: int
    right: int
    signature: str
    decision: MergeDecision


@dataclass(frozen=True)
class CachedMergeVerdict:
    signature: str
    decision: MergeDecision


@dataclass(frozen=True)
class MergeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def __add__(self, other: MergeUsage) -> MergeUsage:
        return type(self)(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.reasoning_tokens + other.reasoning_tokens,
        )

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> MergeUsage:
        return cls(
            int(payload.get("input_tokens") or 0),
            int(payload.get("output_tokens") or 0),
            int(payload.get("reasoning_tokens") or 0),
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


@dataclass(frozen=True)
class MergeJudgeResult:
    decision: MergeDecision
    usage: MergeUsage
    error: str = ""


@dataclass(frozen=True)
class ConfirmedMergeRow:
    slug_a: str
    name_a: str
    slug_b: str
    name_b: str
    confidence: float
    tone_consistent: bool
    reason: str

    def csv_dict(self) -> dict[str, Any]:
        return {
            "slug_a": self.slug_a,
            "name_a": self.name_a,
            "slug_b": self.slug_b,
            "name_b": self.name_b,
            "confidence": self.confidence,
            "tone_consistent": self.tone_consistent,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PairSurvey:
    people: list[MergePerson]
    pairs: list[tuple[int, int]]
    slam: list[tuple[int, int, MergeDecision]]
    shared_unsettled: list[tuple[int, int]]
    reused: list[MergePairVerdict]
    to_judge: list[tuple[int, int, str]]


def identifier_emails(identifiers: tuple[str, ...]) -> set[str]:
    values = (str(identifier).strip() for identifier in identifiers or [])
    return {
        value.lower()
        for value in values
        if "@" in value and "." in value.rsplit("@", 1)[-1]
    }


def identifier_phones(identifiers: tuple[str, ...]) -> set[str]:
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
    """Hydrate exactly one merge-judge input per canonical parent."""
    snapshot = canonical_snapshot(db)
    facts = {
        row.parent_id: row
        for row in snapshot.facts
        if row.person_id is None and row.parent_id
    }
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
    members: dict[str, list] = {}
    for person in snapshot.people:
        members.setdefault(person.parent_id, []).append(person)
    people: list[MergePerson] = []
    for parent in snapshot.parents:
        parent_members = sorted(
            members.get(parent.parent_id, ()), key=lambda row: row.person_id,
        )
        fact = facts.get(parent.parent_id)
        if not parent_members or fact is None:
            continue
        member_ids = tuple(row.person_id for row in parent_members)
        representative = parent_members[0]
        try:
            fact_payload = SynthesizedFacts.from_payload(
                json.loads(fact.facts_json or "{}")
            )
        except json.JSONDecodeError:
            fact_payload = None
        fact_payload = fact_payload or SynthesizedFacts()
        evidence = DossierEvidence.from_parent(parent.parent_id, snapshot)
        owned = fact_payload.owned_identifiers
        emails = tuple(sorted({
            value
            for person_id in member_ids
            for value in identifiers.get(person_id, {}).get(
                IdentifierKind.EMAIL.value, []
            )
        }))
        phones = tuple(sorted({
            phone_digits(value)
            for person_id in member_ids
            for value in identifiers.get(person_id, {}).get(IdentifierKind.PHONE.value, [])
            if phone_digits(value)
        }))
        extra_emails = tuple(sorted(
            identifier_emails(owned.emails) - set(emails) - owner_emails
        ))
        extra_phones = tuple(sorted(
            identifier_phones(owned.phones) - set(phones) - owner_phones
        ))
        name = parent.display_name or fact_payload.canonical_name
        people.append(MergePerson(
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
