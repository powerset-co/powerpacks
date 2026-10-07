"""The 16 schema fields of one candidate's facts, and the collapse of several batch answers into one.

Every batch answer describes the same candidate from a different slice of its messages, so
per-candidate fields (is_owner, relationship_category) are kept, and timeline, shared-context
and topic lists collapse near-duplicate paraphrases (3-gram shingle Jaccard).

Created: 2026-10-06
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Sequence, TypeVar

from packs.ingestion.primitives.deep_context_v2.text_similarity import jaccard, shingles

NEARDUP_THRESHOLD = 0.6

_T = TypeVar("_T")


@dataclass(frozen=True)
class EmployerFact:
    name: str
    role: str
    status: str  # current | past | unknown

    def to_payload(self) -> dict[str, str]:
        return {"name": self.name, "role": self.role, "status": self.status}


@dataclass(frozen=True)
class NotableEvent:
    date: str
    summary: str

    def to_payload(self) -> dict[str, str]:
        return {"date": self.date, "summary": self.summary}


@dataclass(frozen=True)
class SharedContextFact:
    overlap: str
    detail: str
    evidence: str

    def to_payload(self) -> dict[str, str]:
        return {"overlap": self.overlap, "detail": self.detail, "evidence": self.evidence}


@dataclass(frozen=True)
class OwnedIdentifiers:
    emails: tuple[str, ...]
    phones: tuple[str, ...]
    urls: tuple[str, ...]

    def to_payload(self) -> dict[str, list[str]]:
        return {"emails": list(self.emails), "phones": list(self.phones), "urls": list(self.urls)}


@dataclass(frozen=True)
class SynthesizedFacts:
    canonical_name: str
    aliases: tuple[str, ...]
    employers: tuple[EmployerFact, ...]
    title: str
    school: str
    field_of_study: str
    location: str
    relationship_to_owner: str
    relationship_category: str
    topics: tuple[str, ...]
    notable_events: tuple[NotableEvent, ...]
    identifiers: tuple[str, ...]
    owned_identifiers: OwnedIdentifiers
    shared_context: tuple[SharedContextFact, ...]
    confidence: float
    is_owner: bool

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SynthesizedFacts:
        """One strict-schema answer. The schema guarantees every key and its type; nothing is coerced."""
        employers: list[EmployerFact] = []
        for value in payload["employers"]:
            employers.append(EmployerFact(value["name"], value["role"], value["status"]))
        events: list[NotableEvent] = []
        for value in payload["notable_events"]:
            events.append(NotableEvent(value["date"], value["summary"]))
        shared: list[SharedContextFact] = []
        for value in payload["shared_context"]:
            shared.append(SharedContextFact(value["overlap"], value["detail"], value["evidence"]))
        owned = payload["owned_identifiers"]
        return cls(
            canonical_name=payload["canonical_name"],
            aliases=tuple(payload["aliases"]),
            employers=tuple(employers),
            title=payload["title"],
            school=payload["school"],
            field_of_study=payload["field_of_study"],
            location=payload["location"],
            relationship_to_owner=payload["relationship_to_owner"],
            relationship_category=payload["relationship_category"],
            topics=tuple(payload["topics"]),
            notable_events=tuple(events),
            identifiers=tuple(payload["identifiers"]),
            owned_identifiers=OwnedIdentifiers(tuple(owned["emails"]), tuple(owned["phones"]), tuple(owned["urls"])),
            shared_context=tuple(shared),
            confidence=payload["confidence"],
            is_owner=payload["is_owner"],
        )

    def to_payload(self) -> dict[str, Any]:
        """The 16 fields in schema order."""
        employers = []
        for employer in self.employers:
            employers.append(employer.to_payload())
        events = []
        for event in self.notable_events:
            events.append(event.to_payload())
        shared = []
        for context in self.shared_context:
            shared.append(context.to_payload())
        return {
            "canonical_name": self.canonical_name,
            "aliases": list(self.aliases),
            "employers": employers,
            "title": self.title,
            "school": self.school,
            "field_of_study": self.field_of_study,
            "location": self.location,
            "relationship_to_owner": self.relationship_to_owner,
            "relationship_category": self.relationship_category,
            "topics": list(self.topics),
            "notable_events": events,
            "identifiers": list(self.identifiers),
            "owned_identifiers": self.owned_identifiers.to_payload(),
            "shared_context": shared,
            "confidence": self.confidence,
            "is_owner": self.is_owner,
        }


def _unique(values: list[str]) -> tuple[str, ...]:
    """Stripped, non-empty, first spelling of each case-insensitive value."""
    kept: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if text and text.lower() not in seen:
            kept.append(text)
            seen.add(text.lower())
    return tuple(kept)


def _collapse_near_duplicates(items: Sequence[tuple[str, _T]]) -> list[tuple[str, _T]]:
    """Greedy clustering by shingle Jaccard against each cluster's first text.

    `items` is (text, payload) in batch order. Each item joins the first cluster whose first
    text is a near-duplicate, else starts one. Each cluster keeps its longest member (ties to
    the lexicographically greatest), in cluster-creation order.
    """
    clusters: list[list[int]] = []
    anchor_shingles: list[frozenset[str]] = []
    for index, (text, _payload) in enumerate(items):
        text_shingles = shingles(text)
        match = None
        for cluster, anchor in enumerate(anchor_shingles):
            if jaccard(text_shingles, anchor) >= NEARDUP_THRESHOLD:
                match = cluster
                break
        if match is None:
            clusters.append([index])
            anchor_shingles.append(text_shingles)
        else:
            clusters[match].append(index)
    survivors = []
    for cluster in clusters:
        best = cluster[0]
        for index in cluster[1:]:
            if (len(items[index][0]), items[index][0]) > (len(items[best][0]), items[best][0]):
                best = index
        survivors.append(items[best])
    return survivors


def _event_order(event: NotableEvent) -> tuple[str, str]:
    return (event.date or "9999", event.summary.lower())


def _context_order(context: SharedContextFact) -> tuple[str, str]:
    return (context.overlap, context.detail.lower())


def _best_scalar(facts: list[SynthesizedFacts], field: str) -> str:
    """Highest batch confidence wins, then the longer string, then the lexicographically greater one."""
    best: tuple[float, int, str] | None = None
    for fact in facts:
        value = str(getattr(fact, field)).strip()
        if not value:
            continue
        candidate = (fact.confidence, len(value), value)
        if best is None or candidate > best:
            best = candidate
    if best is None:
        return ""
    return best[2]


def collapse(facts: list[SynthesizedFacts]) -> SynthesizedFacts:
    """One candidate's batch answers reduced to one facts object."""
    names = []
    for fact in facts:
        if fact.canonical_name.strip():
            names.append(fact.canonical_name.strip())
    canonical = ""
    if names:
        canonical = Counter(names).most_common(1)[0][0]  # majority; a tie goes to the first seen

    # Batches arrive newest first. The first time an employer name appears, that batch's role and
    # status are kept: the newest view of each employer wins, and the list reads newest first.
    employers: list[EmployerFact] = []
    employer_names: set[str] = set()
    for fact in facts:
        for employer in fact.employers:
            name = employer.name.strip()
            if not name or name.lower() in employer_names:
                continue
            employer_names.add(name.lower())
            employers.append(EmployerFact(name, employer.role.strip(), employer.status))

    aliases: list[str] = []
    owned: dict[str, list[str]] = {"emails": [], "phones": [], "urls": []}
    owned_seen: dict[str, set[str]] = {"emails": set(), "phones": set(), "urls": set()}
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

    relationship = ""
    for fact in facts:
        text = fact.relationship_to_owner.strip()
        if len(text) > len(relationship):
            relationship = text

    categories = []
    for fact in facts:
        if fact.relationship_category:
            categories.append(fact.relationship_category)
    category = ""
    if categories:
        category = Counter(categories).most_common(1)[0][0]

    topic_items: list[tuple[str, str]] = []
    event_items: list[tuple[str, NotableEvent]] = []
    context_items: list[tuple[str, SharedContextFact]] = []
    identifiers: list[str] = []
    for fact in facts:
        for topic in fact.topics:
            text = str(topic).strip()
            if text:
                topic_items.append((text, text))
        for event in fact.notable_events:
            summary = event.summary.strip()
            if summary:
                event_items.append((summary, NotableEvent(event.date.strip(), summary)))
        for context in fact.shared_context:
            detail = context.detail.strip()
            if detail:
                context_items.append((detail, SharedContextFact(context.overlap or "other", detail, context.evidence.strip())))
        identifiers.extend(fact.identifiers)
    topics = []
    for _key, text in _collapse_near_duplicates(topic_items):
        topics.append(text)
    events = []
    for _key, event in _collapse_near_duplicates(event_items):
        events.append(event)
    events.sort(key=_event_order)
    contexts = []
    for _key, context in _collapse_near_duplicates(context_items):
        contexts.append(context)
    contexts.sort(key=_context_order)

    confidence = facts[0].confidence
    is_owner = False
    for fact in facts:
        if fact.confidence > confidence:
            confidence = fact.confidence
        if fact.is_owner:
            is_owner = True

    return SynthesizedFacts(
        canonical_name=canonical,
        aliases=tuple(aliases),
        employers=tuple(employers),
        title=_best_scalar(facts, "title"),
        school=_best_scalar(facts, "school"),
        field_of_study=_best_scalar(facts, "field_of_study"),
        location=_best_scalar(facts, "location"),
        relationship_to_owner=relationship,
        relationship_category=category,
        topics=tuple(topics),
        notable_events=tuple(events),
        identifiers=_unique(identifiers),
        owned_identifiers=OwnedIdentifiers(tuple(owned["emails"]), tuple(owned["phones"]), tuple(owned["urls"])),
        shared_context=tuple(contexts),
        confidence=confidence,
        is_owner=is_owner,
    )
