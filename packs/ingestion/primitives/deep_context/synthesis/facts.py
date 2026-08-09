"""Deterministic reduction of synthesized fact chunks into one person profile."""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from typing import TypeVar

from packs.ingestion.primitives.deep_context.shared.text_similarity import (
    jaccard,
    shingles,
)
from packs.ingestion.primitives.deep_context.synthesis.models import (
    EmployerFact,
    FactRecord,
    NetworkWorthFact,
    NotableEvent,
    OwnedIdentifiers,
    SharedContextFact,
    SynthesizedFacts,
)

# Effectively unbounded, by owner ruling: a topic list is cheap to render and a
# real one should not be truncated. The old value of 25 actively did harm — on a
# heavy contact the list filled with semantically-overlapping phrasings and
# genuinely distinct topics fell off the end, so the cap discarded signal rather
# than noise. Kept as a number rather than deleted so the merge still has a
# runaway backstop if a model ever emits thousands.
MAX_TOPICS = 1000
# Canonical decision vocabulary. models.NetworkWorthFact.from_payload does not
# check against this — it accepts any non-empty string — so this is the only
# place (plus db/projectors.py, which imports it) an out-of-vocabulary value
# actually gets treated as absent.
NETWORK_WORTH_VALUES = ("yes", "maybe", "no")
# is_owner and relationship_category are deliberately absent: a merged parent
# combines several child identities, and neither field means anything blanket
# across them (owners are pre-filtered by callers; category is per-child context).
# Omitting them from `present` means merge_fact_records' output never carries
# either key in to_payload().
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
        # Tie-break order: highest source confidence wins, then the longer string
        # (assumed more informative), then lexicographic for determinism.
        candidates = [
            (fact.confidence, len(value), value)
            for fact in facts
            if (value := str(getattr(fact, field)).strip())
        ]
        return max(candidates)[2] if candidates else ""

    names = [fact.canonical_name.strip() for fact in facts if fact.canonical_name.strip()]
    # Majority vote across chunks; a tie goes to whichever name appeared first.
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
            # Asymmetric merge: status only ever upgrades toward "current", but role
            # keeps the first non-empty value seen rather than the newest one.
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
    # Last valid decision wins by chunk order — not by confidence like best_scalar,
    # and not by a yes > maybe > no priority. Callers that want priority (e.g.
    # normalization.py picks a "winning" child by machine_worth) overwrite this
    # field afterward with replace(merged, network_worth=...).
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


# --- Batch merge: several results for the SAME person, not several children --

NEARDUP_THRESHOLD = 0.6  # Same cutoff as collection.email_context.EmailContext's near-dup email filter; nothing about fact summaries argues for a different number.
# Timeline entries are full sentences, heavier than a topic word, so a long list
# reads as noise rather than signal. A measured real dossier had 87 raw entries,
# 64 of them paraphrasing one 2022 hackathon; near-dup collapse turns that into
# one survivor, leaving a small number of genuinely distinct events. This caps
# the pathological remainder, not the common case.
MAX_NOTABLE_EVENTS = 20
# Shared-context facts (mutual schools/employers/connections) are rarer and more
# specific per person than timeline events. The one measured example (22 raw
# entries, ~13 restating one fact) collapses to about 10 distinct facts under
# near-dup merging; 15 leaves headroom above that without an unbounded section.
MAX_SHARED_CONTEXT = 15
# is_owner and relationship_category are meaningful here (unlike
# _MERGED_FIELDS' several-children job below): every input record describes
# the SAME person, so both fields carry real signal instead of being blanket
# across unrelated identities.
_BATCH_MERGED_FIELDS = _MERGED_FIELDS | {"is_owner", "relationship_category"}

_T = TypeVar("_T")


def _collapse_near_duplicates(
    items: Sequence[tuple[str, _T]],
    threshold: float = NEARDUP_THRESHOLD,
) -> list[tuple[str, _T]]:
    """Greedy near-dup clustering by 3-gram shingle Jaccard over ``items[i][0]``.

    ``items`` is a (text, payload) sequence in stable, caller-determined order
    — batch order here, which ``asyncio.gather`` preserves regardless of
    completion order, so the result is deterministic for the same inputs. A
    new item joins the first existing cluster whose ANCHOR (that cluster's
    first-seen text, fixed — not the evolving longest member) is a
    near-duplicate; matching against a fixed anchor instead of a moving
    "current longest" avoids drift where accepting a long, detail-heavy
    member could make a cluster stop recognizing the shorter paraphrases it
    already matched. Each cluster's survivor is its longest member (ties
    broken lexicographically) — the fuller paraphrase is assumed more
    informative — returned in cluster-creation (first-seen) order.
    """
    clusters: list[list[int]] = []  # indices into items, one list per cluster
    anchor_shingles: list[frozenset[str]] = []

    def rank(index: int) -> tuple[int, str]:
        text = items[index][0]
        return (len(text), text)

    for index, (text, _payload) in enumerate(items):
        text_shingles = shingles(text)
        match = next(
            (
                cluster
                for cluster, anchor in enumerate(anchor_shingles)
                if jaccard(text_shingles, anchor) >= threshold
            ),
            None,
        )
        if match is None:
            clusters.append([index])
            anchor_shingles.append(text_shingles)
        else:
            clusters[match].append(index)
    return [items[max(cluster, key=rank)] for cluster in clusters]


def _collapse_events(facts: list[SynthesizedFacts], cap: int) -> tuple[NotableEvent, ...]:
    candidates: list[tuple[str, NotableEvent]] = []
    for fact in facts:
        for event in fact.notable_events:
            summary = event.summary.strip()
            if summary:
                candidates.append((summary, NotableEvent(event.date.strip(), summary)))
    survivors = [event for _text, event in _collapse_near_duplicates(candidates)]
    ordered = sorted(survivors, key=lambda event: (event.date or "9999", event.summary.lower()))
    return tuple(ordered[:cap])


def _collapse_shared_context(facts: list[SynthesizedFacts], cap: int) -> tuple[SharedContextFact, ...]:
    candidates: list[tuple[str, SharedContextFact]] = []
    for fact in facts:
        for context in fact.shared_context:
            detail = context.detail.strip()
            if detail:
                candidates.append((
                    detail,
                    SharedContextFact(context.overlap or "other", detail, context.evidence.strip()),
                ))
    survivors = [context for _text, context in _collapse_near_duplicates(candidates)]
    ordered = sorted(survivors, key=lambda context: (context.overlap, context.detail.lower()))
    return tuple(ordered[:cap])


def _collapse_topics(facts: list[SynthesizedFacts], cap: int) -> tuple[str, ...]:
    candidates: list[tuple[str, str]] = []
    for fact in facts:
        for topic in fact.topics:
            text = str(topic).strip()
            if text:
                candidates.append((text, text))
    survivors = [text for _key, text in _collapse_near_duplicates(candidates)]
    return tuple(survivors[:cap])


def _merge_is_owner(facts: list[SynthesizedFacts]) -> bool | None:
    """Any batch reporting True is a strong signal; absent everywhere stays None."""
    reported = [fact.is_owner for fact in facts if fact.is_owner is not None]
    if not reported:
        return None
    return any(reported)


def _merge_relationship_category(facts: list[SynthesizedFacts]) -> str | None:
    values = [fact.relationship_category for fact in facts if fact.relationship_category]
    return Counter(values).most_common(1)[0][0] if values else None


def merge_batch_facts(chunks: Iterable[FactRecord]) -> SynthesizedFacts | None:
    """Reduce one person's concurrently-fetched batch results into one profile.

    Unlike ``merge_fact_records`` above (which blends several DIFFERENT child
    identities into one parent), every input here describes the SAME person
    from a different slice of their message history. That changes what the
    right merge policy is for two kinds of field:

    - ``is_owner``/``relationship_category`` are per-PERSON facts here, not
      per-child context, so — unlike ``_MERGED_FIELDS`` — they are kept.
    - ``notable_events``/``shared_context``/``topics`` collapse near-duplicate
      paraphrases (3-gram shingle Jaccard, the same primitive
      ``collection.email_context`` uses for near-dup email removal) instead of
      exact-string dedup. Twenty batches describing one event in different
      words is the actual failure this function exists to fix: a real 550-person
      paid run produced one dossier with 87 timeline entries, 64 of them
      paraphrasing a single 2022 hackathon.

    The rest of the reduction (canonical-name majority vote, employer union
    with status-upgrade, best-scalar tie-break, alias/identifier union)
    intentionally mirrors ``merge_fact_records``'s shape for the same field —
    same job, same answer either way. That parallel structure is pinned, not
    accidental: ``merge_fact_records`` keeps its three existing callers'
    output byte-for-byte unchanged, so its body is not touched here.
    """
    records = list(chunks)
    facts = [record.facts for record in records]
    if not facts:
        return None

    def best_scalar(field: str) -> str:
        # Tie-break order: highest source confidence wins, then the longer string
        # (assumed more informative), then lexicographic for determinism.
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

    relationship = max(
        (fact.relationship_to_owner.strip() for fact in facts),
        key=len,
        default="",
    )

    worth: NetworkWorthFact | None = None
    # Same "last valid decision wins by chunk order" policy as merge_fact_records.
    for fact in facts:
        value = fact.network_worth
        if value and value.decision in NETWORK_WORTH_VALUES:
            worth = NetworkWorthFact(value.decision, value.reason.strip())

    return SynthesizedFacts(
        canonical_name=canonical,
        aliases=tuple(aliases),
        employers=tuple(employers.values()),
        title=best_scalar("title"),
        school=best_scalar("school"),
        field_of_study=best_scalar("field_of_study"),
        location=best_scalar("location"),
        relationship_to_owner=relationship,
        relationship_category=_merge_relationship_category(facts),
        topics=_collapse_topics(facts, MAX_TOPICS),
        notable_events=_collapse_events(facts, MAX_NOTABLE_EVENTS),
        identifiers=_unique(facts, "identifiers"),
        owned_identifiers=OwnedIdentifiers(
            tuple(owned["emails"]), tuple(owned["phones"]), tuple(owned["urls"]),
        ),
        shared_context=_collapse_shared_context(facts, MAX_SHARED_CONTEXT),
        confidence=max((fact.confidence for fact in facts), default=0.0),
        is_owner=_merge_is_owner(facts),
        network_worth=worth,
        present=_BATCH_MERGED_FIELDS,
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
    # 80-char cap keeps the headline UI-sized; break on the last space before the
    # cutoff (not mid-word) and drop trailing punctuation before the ellipsis.
    prefix = relationship[:80].rsplit(" ", 1)[0].rstrip(",;:")
    return f"{prefix}…"
