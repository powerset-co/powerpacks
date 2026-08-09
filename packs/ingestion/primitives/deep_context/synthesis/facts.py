"""Deterministic reduction of synthesized fact records into one person profile.

Two reductions live here and they are NOT interchangeable. Which one is correct
is decided entirely by what the input records are:

- ``merge_disjoint_fact_records`` — the records describe several DIFFERENT
  people (child identities being blended into one parent). Per-person fields
  are meaningless across them and are dropped; text lists dedupe on exact
  string, because two different people saying similar things is not redundancy.
- ``collapse_fact_records`` — the records are one person's own message-history
  batches. Per-person fields carry real signal and are kept; text lists collapse
  near-duplicates, because twenty batches paraphrasing one event IS redundancy.

Calling the first one where the second belongs is the bug that produced an
87-entry timeline for a single contact, and silently dropped ``is_owner`` on
every multi-batch person in a 550-person paid run.

Changelog:
- 2026-08-08: split into the two functions above and renamed. The batch call
  site (synthesis/runner.py) previously used the disjoint merge.
"""
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
# Omitting them from `present` means merge_disjoint_fact_records' output never carries
# either key in to_payload().
_DISJOINT_FIELDS = frozenset({
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


def merge_disjoint_fact_records(chunks: Iterable[FactRecord]) -> SynthesizedFacts | None:
    """Blend several DIFFERENT people's fact records into one parent row.

    Inputs are disjoint identities — separate children being resolved into one
    parent, or a parent's per-child evidence. Two consequences follow from that
    and are deliberate, not oversights:

    - ``is_owner`` and ``relationship_category`` are dropped (see
      ``_DISJOINT_FIELDS``). Neither is true blanket across unrelated people.
    - Text lists dedupe on EXACT string. Two different people described in
      similar words are two facts, not one, so near-duplicate collapse would
      destroy information here — the opposite of its effect in
      ``collapse_fact_records``.

    Callers: parent construction (merge_candidates/build_parents.py), dossier
    evidence (shared/dossier_evidence.py), and two migration-only paths
    (synthesis/normalization.py, ``merge_facts`` below). It is NOT the reducer
    for one person's own batches — that is ``collapse_fact_records``.
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
        present=_DISJOINT_FIELDS,
    )


# --- Collapse: several results for the SAME person, not several children ------

NEARDUP_THRESHOLD = 0.6  # Same cutoff as collection.email_context.EmailContext's near-dup email filter; nothing about fact summaries argues for a different number.
# Runaway backstops, not editorial limits — same ruling as MAX_TOPICS. These
# were 20 and 15 on the theory that near-dup collapse had already removed the
# redundancy and a cap only trimmed the residue. Measured over a real 550-person
# run, that theory was wrong: the collapse removes 4 of 1,420 timeline entries
# and 4 of 293 shared-context entries. The caps were doing ALL of the thinning,
# which means they were discarding true, evidenced facts (every claim in the
# 87-entry dossier checked out; it was unreadable, not wrong) to hide redundancy
# that is semantic, not textual — no pair of that dossier's 25 topics reaches
# even 0.4 Jaccard.
#
# At 100 nothing on the real corpus is truncated at all (observed maxima: 84
# timeline entries, 22 shared-context). Prompt size is not the constraint that
# argues for a smaller number either: the judge-facing view bounds itself at its
# own boundary (shared/dossier_evidence.py slices topics[:10] and renders [:8],
# and never carries notable_events), so these govern only what the rendered
# dossier keeps. Revisit if semantic dedupe ever lands.
MAX_NOTABLE_EVENTS = 100
MAX_SHARED_CONTEXT = 100
# is_owner and relationship_category are meaningful here (unlike
# _DISJOINT_FIELDS' several-children job below): every input record describes
# the SAME person, so both fields carry real signal instead of being blanket
# across unrelated identities.
_COLLAPSED_FIELDS = _DISJOINT_FIELDS | {"is_owner", "relationship_category"}

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


def collapse_fact_records(chunks: Iterable[FactRecord]) -> SynthesizedFacts | None:
    """Reduce one person's concurrently-fetched batch results into one profile.

    Unlike ``merge_disjoint_fact_records`` above (which blends several DIFFERENT child
    identities into one parent), every input here describes the SAME person
    from a different slice of their message history. That changes what the
    right merge policy is for two kinds of field:

    - ``is_owner``/``relationship_category`` are per-PERSON facts here, not
      per-child context, so — unlike ``_DISJOINT_FIELDS`` — they are kept.
    - ``notable_events``/``shared_context``/``topics`` collapse near-duplicate
      paraphrases (3-gram shingle Jaccard, the same primitive
      ``collection.email_context`` uses for near-dup email removal) instead of
      exact-string dedup.

    HOW MUCH THAT SECOND ONE ACTUALLY BUYS, measured over a real 550-person run
    rather than assumed: it removed 4 of 1,420 timeline entries and 4 of 293
    shared-context entries. Near-identical wording is rare; what these lists are
    really full of is SEMANTIC redundancy — one dossier had 87 timeline entries,
    64 about a single 2022 hackathon, and not one pair of its 25 topics reached
    even 0.4 Jaccard. String similarity cannot see that. So the caps below, not
    this collapse, are what currently keeps a heavy contact's dossier readable,
    and closing the gap properly needs an embedding pass, not a better cutoff.
    Do not read the collapse as solving the redundancy problem.

    The rest of the reduction (canonical-name majority vote, employer union
    with status-upgrade, best-scalar tie-break, alias/identifier union)
    intentionally mirrors ``merge_disjoint_fact_records``'s shape for the same
    field — same job, same answer either way. That parallel structure is pinned,
    not accidental: ``merge_disjoint_fact_records`` keeps its existing callers'
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
    # Same "last valid decision wins by chunk order" policy as merge_disjoint_fact_records.
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
        present=_COLLAPSED_FIELDS,
    )


def merge_facts(chunks: Iterable[dict[str, object]]) -> dict[str, object]:
    """Migration-only dict adapter; delete once no install predates v1.19.0."""
    merged = merge_disjoint_fact_records(
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
