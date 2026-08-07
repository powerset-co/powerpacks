"""Deterministic reduction of synthesized fact chunks into one person profile."""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from packs.ingestion.primitives.deep_context.dossier.models import (
    EmployerFact,
    FactRecord,
    NetworkWorthFact,
    NotableEvent,
    OwnedIdentifiers,
    SharedContextFact,
    SynthesizedFacts,
)

MAX_TOPICS = 25
NETWORK_WORTH_VALUES = ("yes", "maybe", "no")
_MERGED_FIELDS = frozenset({
    "canonical_name",
    "aliases",
    "employers",
    "title",
    "school",
    "field_of_study",
    "location",
    "relationship_to_owner",
    "topics",
    "notable_events",
    "identifiers",
    "owned_identifiers",
    "shared_context",
    "network_worth",
    "confidence",
})


def _unique(facts: list[SynthesizedFacts], field: str) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for fact in facts:
        for value in getattr(fact, field):
            text = str(value).strip()
            if text and text.lower() not in seen:
                values.append(text)
                seen.add(text.lower())
    return tuple(values)


def merge_fact_records(chunks: Iterable[FactRecord]) -> SynthesizedFacts | None:
    """Reduce parsed synthesis records into one typed parent fact row."""
    records = list(chunks)
    facts = [record.facts for record in records]
    if not facts:
        return None

    def best_scalar(field: str) -> str:
        candidates = [
            (fact.confidence, len(value), value)
            for fact in facts
            if (value := str(getattr(fact, field)).strip())
        ]
        return max(candidates)[2] if candidates else ""

    names = [fact.canonical_name.strip() for fact in facts if fact.canonical_name.strip()]
    canonical = Counter(names).most_common(1)[0][0] if names else ""
    employers: dict[str, EmployerFact] = {}
    status_rank = {"current": 2, "past": 1, "unknown": 0}
    for fact in facts:
        for employer in fact.employers:
            name = employer.name.strip()
            if not name:
                continue
            key = name.lower()
            candidate = EmployerFact(
                name,
                employer.role.strip(),
                employer.status or "unknown",
            )
            incumbent: EmployerFact | None = employers.get(key)
            if incumbent is None:
                employers[key] = candidate
                continue
            status = (
                candidate.status
                if status_rank.get(candidate.status, 0)
                > status_rank.get(incumbent.status, 0)
                else incumbent.status
            )
            employers[key] = EmployerFact(
                incumbent.name,
                incumbent.role or candidate.role,
                status,
            )

    aliases: list[str] = []
    owned: dict[str, list[str]] = {"emails": [], "phones": [], "urls": []}
    owned_seen: dict[str, set[str]] = {kind: set() for kind in owned}
    for fact in facts:
        for value in fact.aliases:
            text = value.strip()
            if text and text != canonical and text not in aliases:
                aliases.append(text)
        for kind in owned:
            for value in getattr(fact.owned_identifiers, kind):
                text = value.strip()
                if text and text.lower() not in owned_seen[kind]:
                    owned[kind].append(text)
                    owned_seen[kind].add(text.lower())

    events: dict[tuple[str, str], NotableEvent] = {}
    for fact in facts:
        for event in fact.notable_events:
            summary = event.summary.strip()
            if summary:
                date = event.date.strip()
                events[(date, summary.lower())] = NotableEvent(date, summary)
    relationship = max(
        (fact.relationship_to_owner.strip() for fact in facts),
        key=len,
        default="",
    )

    worth: NetworkWorthFact | None = None
    for fact in facts:
        value = fact.network_worth
        if value and value.decision in NETWORK_WORTH_VALUES:
            worth = NetworkWorthFact(value.decision, value.reason.strip())
    shared: dict[str, SharedContextFact] = {}
    for fact in facts:
        for context in fact.shared_context:
            detail = context.detail.strip()
            if detail:
                shared[detail.lower()] = SharedContextFact(
                    context.overlap or "other",
                    detail,
                    context.evidence.strip(),
                )

    return SynthesizedFacts(
        canonical_name=canonical,
        aliases=tuple(aliases),
        employers=tuple(employers.values()),
        title=best_scalar("title"),
        school=best_scalar("school"),
        field_of_study=best_scalar("field_of_study"),
        location=best_scalar("location"),
        relationship_to_owner=relationship,
        topics=_unique(facts, "topics")[:MAX_TOPICS],
        notable_events=tuple(sorted(events.values(), key=lambda event: event.date or "9999")),
        identifiers=_unique(facts, "identifiers"),
        owned_identifiers=OwnedIdentifiers(
            tuple(owned["emails"]), tuple(owned["phones"]), tuple(owned["urls"]),
        ),
        shared_context=tuple(shared.values()),
        confidence=max((fact.confidence for fact in facts), default=0.0),
        network_worth=worth,
        present=_MERGED_FIELDS,
    )


def merge_facts(chunks: Iterable[dict[str, object]]) -> dict[str, object]:
    """Migration-only dict adapter; delete once no install predates v1.19.0."""
    merged = merge_fact_records(
        record
        for chunk in chunks
        if (record := FactRecord.from_payload(chunk)) is not None
    )
    return merged.to_payload() if merged else {}


def headline(merged: SynthesizedFacts | None) -> str:
    if merged is None:
        return ""
    current: EmployerFact | None = next(
        (employer for employer in merged.employers if employer.status == "current"),
        merged.employers[0] if merged.employers else None,
    )
    company = current.name if current else ""
    if merged.title and company:
        return f"{merged.title} at {company}"
    if merged.title or company:
        return merged.title or company
    relationship = merged.relationship_to_owner.strip()
    if len(relationship) <= 80:
        return relationship
    prefix = relationship[:80].rsplit(" ", 1)[0].rstrip(",;:")
    return f"{prefix}…"
