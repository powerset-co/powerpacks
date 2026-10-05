"""The share UI's read boundary: one typed row per parent, joined once.

Flow: the roster (people.csv through `imported_people`, the one roster reader)
+ the canonical store (`share`, `person_labels`, `person_tags`, `people`,
`parents`, `facts`) -> `SharePerson` rows -> one JSON payload for the page.
`person_detail(id)` adds what only the drawer shows: the dossier, every label
cell, the worth note, the contact identifiers.

The page filters client-side, so the list row carries only what a facet or a
column reads; the 27 probabilities and the dossier stay per-person.

Changelog:
  2026-09-26: created.
"""

from __future__ import annotations

from dataclasses import asdict, astuple, dataclass, fields, replace
from typing import Any

from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context.db import queries, share_views
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ParentSnapshotRow,
    PersonTagRow,
    ShareDecisionRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.ensure_parents.imported_people import (
    ImportedPerson,
    stored_imported_people,
)
from packs.ingestion.primitives.deep_context.review.dossier_html import markdown_to_html
from packs.ingestion.primitives.share.labels import ACTIVE_P
from packs.ingestion.primitives.share.questions import CHOICE_LABELS, NOUL_LABELS
from packs.ingestion.primitives.share.store import split_tags
from packs.ingestion.schemas.share_schema import SHARE_CONFIRM, SHARE_NO, SHARE_YES
from packs.ingestion.schemas.people_schema import merge_interaction_counts

WORTH_HUMAN = "human"
WORTH_MACHINE = "machine"

# A people.csv channel name -> the channel the page shows (one icon per family).
CHANNEL_FAMILIES = {
    "gmail_msgvault": "gmail",
    "gmail": "gmail",
    "imessage": "imessage",
    "imessage_group": "imessage",
    "whatsapp": "whatsapp",
    "linkedin_csv": "linkedin",
    "linkedin": "linkedin",
}


@dataclass(frozen=True)
class SharePerson:
    """One list row per parent: the roster cells of the people merged under it,
    the decision and the label cells a facet reads."""

    parent_id: str
    public_identifier: str
    name: str
    has_avatar: bool
    title: str
    company: str
    location: str
    channels: tuple[str, ...]
    interactions: int
    last_interaction: str
    recency_days: int | None
    cadence: str
    direction: str
    worth: str
    worth_source: str
    relationship_kind: str
    mode: str
    hierarchy: str
    intro_source: str
    seniority: str
    function: str
    warmth: float | None
    labels: tuple[str, ...]
    flag: str
    share: str
    reason: str
    share_source: str
    tags: tuple[str, ...]
    is_owner: bool
    linkedin_only: bool
    group_chat_only: bool
    shared_employer: bool
    shared_school: bool
    confidence: float | None


@dataclass(frozen=True)
class ShareCounts:
    total: int
    upload: int
    confirm: int
    private: int

    @classmethod
    def of(cls, rows: tuple[SharePerson, ...]) -> "ShareCounts":
        return cls(
            total=len(rows),
            upload=sum(row.share == SHARE_YES for row in rows),
            confirm=sum(row.share == SHARE_CONFIRM for row in rows),
            private=sum(row.share == SHARE_NO for row in rows),
        )


@dataclass(frozen=True)
class FactEvent:
    """One dated line of the relationship, from the dossier's notable events."""

    date: str
    summary: str


@dataclass(frozen=True)
class PersonDetail:
    """What only the drawer shows for one parent."""

    parent_id: str
    linkedin_url: str
    headline: str
    avatar_url: str
    worth_reason: str
    note: str
    dossier_html: str
    probabilities: dict[str, float]
    choice_p: dict[str, float]
    worth_note: str
    relationship_to_owner: str
    events: tuple[FactEvent, ...]
    shared_context: tuple[str, ...]
    topics: tuple[str, ...]
    employers: tuple[str, ...]
    school: str
    location: str
    aliases: tuple[str, ...]
    emails: tuple[str, ...]
    phones: tuple[str, ...]


def _events(facts: dict[str, Any]) -> tuple[FactEvent, ...]:
    """The notable events, most recent first; dates are ISO prefixes, so text order is time order."""
    events = (FactEvent(date=str(row.get("date") or ""), summary=str(row.get("summary") or ""))
              for row in facts.get("notable_events") or [] if isinstance(row, dict))
    return tuple(sorted((event for event in events if event.summary), key=lambda event: event.date, reverse=True))


def _shared_context(facts: dict[str, Any]) -> tuple[str, ...]:
    return tuple(f"{row.get('overlap')}: {row.get('detail')}" if row.get("overlap") else str(row.get("detail"))
                 for row in facts.get("shared_context") or [] if isinstance(row, dict) and row.get("detail"))


def _families(channels: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(CHANNEL_FAMILIES.get(channel, channel) for channel in channels))


def _facts_title(facts: dict[str, Any]) -> tuple[str, str]:
    title = str(facts.get("title") or "")
    employers = [row for row in facts.get("employers") or [] if isinstance(row, dict)]
    current = next((row for row in employers if str(row.get("status") or "") == "current"), None)
    company = str((current or (employers[0] if employers else {})).get("name") or "")
    return title, company


class SharePeople:
    """The roster joined to the store. Construct once; `load()` re-reads the
    store (it changes as the human tags), the roster is read on first use."""

    def __init__(self, db: Db) -> None:
        self._families: dict[str, tuple[ImportedPerson, ...]] = {}
        self._families_stamp = 0
        self.db = db

    @property
    def roster(self) -> dict[str, ImportedPerson]:
        return {row.person_id: row for row in stored_imported_people(self.db)}

    def families(self) -> dict[str, tuple[ImportedPerson, ...]]:
        """The roster people the list decides on, grouped under the store's parent:
        one row per parent, however many emails and phones the roster kept."""
        stamp = self.db.db_path.stat().st_mtime_ns
        if self._families_stamp != stamp:
            decided = {row.person_id for row in share_views.share_decisions(self.db)}
            parent_of_person = {row.person_id: row.parent_id for row in queries.people(self.db)}
            decided_parents = {parent_of_person[person_id] for person_id in decided}
            families: dict[str, list[ImportedPerson]] = {}
            for imported in self.roster.values():
                key = parent_of_person[imported.person_id]
                if key not in decided_parents:
                    continue
                families.setdefault(key, []).append(replace(imported, superseded_person_ids=tuple(
                    old for old in imported.superseded_person_ids if parent_of_person.get(old, key) == key)))
            self._families = {parent_id: tuple(members) for parent_id, members in families.items()}
            self._families_stamp = stamp
        return self._families

    def load(self) -> tuple[SharePerson, ...]:
        families = self.families()
        if not families:
            return ()
        decisions = {row.person_id: row for row in share_views.share_decisions(self.db)}
        labels = {row.person_id: row for row in share_views.person_labels(self.db)}
        tags = {row.person_id: row for row in share_views.person_tags(self.db)}
        parents = {row.parent_id: row for row in queries.parents(self.db)}
        fact_rows = {row.parent_id: row for row in sorted(
            queries.facts(self.db, parent_owned=True), key=lambda row: row.artifact_key.startswith("parent-facts:"))}
        facts = {parent_id: parse_json_object(row.facts_json) for parent_id, row in fact_rows.items()}
        rows = []
        for parent_id, members in families.items():
            ordered = _by_weight(members)
            primary = ordered[0]
            # The node writes person_labels and share together: a decision always has its label row.
            decided_by = _decided_by(ordered, decisions)
            decision = decisions[decided_by.person_id]
            held = _held((decided_by, *ordered), tags)
            label = labels[decided_by.person_id]
            jev = parse_json_object(label.labels_json)
            parent = parents.get(parent_id)
            fact = facts.get(parent_id, {})
            fact_title, fact_company = _facts_title(fact)
            rows.append(SharePerson(
                parent_id=parent_id,
                public_identifier=_first(member.public_identifier for member in ordered),
                name=_name(fact, parent, primary),
                has_avatar=any(member.avatar_url for member in members),
                title=_first(member.title for member in ordered) or fact_title,
                company=_first(member.company for member in ordered) or fact_company,
                location=_first(member.location for member in ordered) or str(fact.get("location") or ""),
                channels=_families(tuple(channel for member in ordered for channel in member.source_channels)),
                interactions=sum(merge_interaction_counts(*(member.interaction_counts for member in members)).values()),
                last_interaction=max((member.last_interaction for member in members), default=""),
                recency_days=jev.get("recency_days"),
                cadence=str(jev.get("cadence") or ""),
                direction=str(jev.get("direction") or ""),
                worth=str(label.worth or ""),
                worth_source=_worth_source(parent, fact),
                relationship_kind=str(jev.get("relationship_kind") or ""),
                mode=str(jev.get("mode") or ""),
                hierarchy=str(jev.get("hierarchy") or ""),
                intro_source=str(jev.get("intro_source") or ""),
                seniority=str(jev.get("seniority") or ""),
                function=str(jev.get("function") or ""),
                warmth=jev.get("warmth"),
                labels=tuple(name for name in NOUL_LABELS if float(jev.get(name, 0.0)) >= ACTIVE_P),
                flag=str(label.flag or ""),
                share=decision.share,
                reason=decision.reason,
                share_source=decision.source,
                tags=tuple(sorted(split_tags(held.tags))) if held else (),
                is_owner=bool(jev.get("is_owner")),
                linkedin_only=bool(jev.get("linkedin_only")),
                group_chat_only=bool(jev.get("group_chat_only")),
                shared_employer=bool(jev.get("shared_employer")),
                shared_school=bool(jev.get("shared_school")),
                confidence=fact_rows[parent_id].confidence if parent_id in fact_rows else None,
            ))
        return tuple(rows)

    def avatar_url(self, parent_id: str) -> str:
        return _first(member.avatar_url for member in _by_weight(self.families().get(parent_id, ())))

    def detail(self, parent_id: str) -> PersonDetail | None:
        members = self.families().get(parent_id)
        if not members:
            return None
        ordered = _by_weight(members)
        primary = ordered[0]
        linked = next((member for member in ordered if member.linkedin_url), primary)
        parent = next(iter(queries.parents(self.db, parent_id=parent_id)), None)
        fact_rows = sorted(queries.facts(self.db, parent_id=parent_id, parent_owned=True),
                           key=lambda row: row.artifact_key.startswith("parent-facts:"))
        facts = parse_json_object(fact_rows[-1].facts_json) if fact_rows else {}
        decisions = {row.person_id: row for row in share_views.share_decisions(self.db)}
        decided_by = _decided_by(ordered, decisions)
        label = next(row for row in share_views.person_labels(self.db) if row.person_id == decided_by.person_id)
        cells = parse_json_object(label.labels_json)
        tags = {row.person_id: row for row in share_views.person_tags(self.db)}
        held = _held((decided_by, *ordered), tags)
        dossier = ""
        for artifact in sorted(queries.artifacts(self.db, kind=ArtifactKind.DOSSIER.value,
                                                parent_id=parent_id, status="projected"),
                               key=lambda row: (row.person_id is None, row.projected_at or "",
                                                row.artifact_key.startswith("dossier:"))):
            body = str(parse_json_object(artifact.payload_json).get("body") or "")
            if body:
                dossier = body
        name = _name(facts, parent, primary)
        known_as = (*(member.display_name for member in ordered), *(str(alias) for alias in facts.get("aliases") or []))
        return PersonDetail(
            parent_id=parent_id,
            linkedin_url=linked.linkedin_url,
            headline=linked.headline or primary.headline,
            avatar_url=_first(member.avatar_url for member in ordered),
            worth_reason=str((parent.machine_worth_reason if parent else None)
                             or (facts.get("network_worth") or {}).get("reason") or ""),
            note=str((held.note if held else None) or ""),
            dossier_html=markdown_to_html(dossier) if dossier else "",
            probabilities={name: float(cells[name]) for name in NOUL_LABELS if name in cells},
            choice_p={name: float(cells[f"{name}_p"]) for name in CHOICE_LABELS if f"{name}_p" in cells},
            worth_note=str((parent.human_worth_note if parent else None) or ""),
            relationship_to_owner=str(facts.get("relationship_to_owner") or ""),
            events=_events(facts),
            shared_context=_shared_context(facts),
            topics=tuple(str(topic) for topic in facts.get("topics") or []),
            employers=tuple(
                " · ".join(part for part in (str(row.get("role") or ""), str(row.get("name") or "")) if part)
                for row in facts.get("employers") or [] if isinstance(row, dict)),
            school=str(facts.get("school") or ""),
            location=_first(member.location for member in ordered) or str(facts.get("location") or ""),
            aliases=tuple(dict.fromkeys(alias for alias in known_as if alias and alias != name)),
            emails=tuple(dict.fromkeys(email for member in ordered for email in member.emails)),
            phones=tuple(dict.fromkeys(phone for member in ordered for phone in member.phones)),
        )


def _by_weight(members: tuple[ImportedPerson, ...]) -> tuple[ImportedPerson, ...]:
    """The people under one parent, the one with the most to say first."""
    return tuple(sorted(members, key=lambda member: (
        -sum(member.interaction_counts.values()), not member.linkedin_url, member.person_id)))


def _decided_by(ordered: tuple[ImportedPerson, ...], decisions: dict[str, ShareDecisionRow]) -> ImportedPerson:
    """A saved human decision wins over other members' machine decisions."""
    decided = tuple(member for member in ordered if member.person_id in decisions)
    return next((member for member in decided if decisions[member.person_id].source == "human"), decided[0])


def _held(members: tuple[ImportedPerson, ...], tags: dict[str, PersonTagRow]) -> PersonTagRow | None:
    return next((tags[key] for member in members for key in (member.person_id, *member.superseded_person_ids)
                 if key in tags), None)


def _name(facts: dict[str, Any], parent: ParentSnapshotRow | None, primary: ImportedPerson) -> str:
    """The dossier's canonical name, else the parent's, else the roster's."""
    return str(facts.get("canonical_name") or "") or str((parent.display_name if parent else None) or "") \
        or primary.display_name


def _first(values: Any) -> str:
    return next((value for value in values if value), "")

def _worth_source(parent: ParentSnapshotRow | None, facts: dict[str, Any]) -> str:
    """Who decided the effective worth: the human, else the model (on the parent or its fact)."""
    if parent is not None and parent.human_worth:
        return WORTH_HUMAN
    if (parent is not None and parent.machine_worth) or (facts.get("network_worth") or {}).get("decision"):
        return WORTH_MACHINE
    return ""


PEOPLE_COLUMNS = tuple(field.name for field in fields(SharePerson))


def people_payload(rows: tuple[SharePerson, ...]) -> dict[str, Any]:
    """The page's one payload: the header counts, the column names once, one
    array per person. Columnar because 28k rows of repeated keys is 22 MB;
    the same rows as arrays are under 9 MB and parse in well under a second."""
    return {
        "counts": asdict(ShareCounts.of(rows)),
        "columns": list(PEOPLE_COLUMNS),
        "rows": [list(astuple(row)) for row in rows],
    }
