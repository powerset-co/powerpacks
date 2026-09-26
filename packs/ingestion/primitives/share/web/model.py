"""The share UI's read boundary: one typed row per roster person, joined once.

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

from dataclasses import asdict, astuple, dataclass, fields
from functools import cached_property
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context.db import queries, share_views
from packs.ingestion.primitives.deep_context.db.models import ArtifactKind, ParentSnapshotRow
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.ensure_parents.imported_people import (
    ImportedPerson,
    read_imported_people,
)
from packs.ingestion.primitives.deep_context.review.rendering import markdown_to_html
from packs.ingestion.primitives.deep_context.shared.common import DEFAULT_PEOPLE_CSV
from packs.ingestion.primitives.share.labels import ACTIVE_P
from packs.ingestion.primitives.share.questions import CHOICE_LABELS, NOUL_LABELS
from packs.ingestion.primitives.share.store import split_tags
from packs.ingestion.schemas.share_schema import SHARE_CONFIRM, SHARE_NO, SHARE_YES

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
    """One list row: the roster cells plus the decision and the label cells a facet reads."""

    person_id: str
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
class PersonDetail:
    """What only the drawer shows for one person."""

    person_id: str
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
    topics: tuple[str, ...]
    employers: tuple[str, ...]
    school: str
    emails: tuple[str, ...]
    phones: tuple[str, ...]


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

    def __init__(self, db: Db, *, people_csv: Path = DEFAULT_PEOPLE_CSV) -> None:
        self.db = db
        self.people_csv = Path(people_csv)

    @cached_property
    def roster(self) -> dict[str, ImportedPerson]:
        return {row.person_id: row for row in read_imported_people(self.people_csv)}

    def load(self) -> tuple[SharePerson, ...]:
        decisions = {row.person_id: row for row in share_views.share_decisions(self.db)}
        if not decisions:
            return ()
        labels = {row.person_id: row for row in share_views.person_labels(self.db)}
        tags = {row.person_id: row for row in share_views.person_tags(self.db)}
        parents = {row.parent_id: row for row in queries.parents(self.db)}
        parent_of_person = {row.person_id: row.parent_id for row in queries.people(self.db)}
        fact_rows = {row.parent_id: row for row in queries.facts(self.db, parent_owned=True)}
        facts = {parent_id: parse_json_object(row.facts_json) for parent_id, row in fact_rows.items()}
        rows = []
        for imported in self.roster.values():
            decision = decisions.get(imported.person_id)
            if decision is None:
                continue
            parent_id = self._parent_id(imported, parent_of_person)
            parent = parents.get(parent_id)
            # The node writes person_labels and share together: a decision always has its label row.
            label = labels[imported.person_id]
            cells = parse_json_object(label.labels_json)
            held = tags.get(imported.person_id) or next(
                (tags[old] for old in imported.superseded_person_ids if old in tags), None)
            fact = facts.get(parent_id, {})
            fact_title, fact_company = _facts_title(fact)
            rows.append(SharePerson(
                person_id=imported.person_id,
                public_identifier=imported.public_identifier,
                name=imported.display_name,
                has_avatar=bool(imported.avatar_url),
                title=imported.title or fact_title,
                company=imported.company or fact_company,
                location=imported.location or str(fact.get("location") or ""),
                channels=_families(imported.source_channels),
                interactions=sum(imported.interaction_counts.values()),
                last_interaction=imported.last_interaction,
                recency_days=cells.get("recency_days"),
                cadence=str(cells.get("cadence") or ""),
                direction=str(cells.get("direction") or ""),
                worth=str(label.worth or ""),
                worth_source=_worth_source(parent, fact),
                relationship_kind=str(cells.get("relationship_kind") or ""),
                mode=str(cells.get("mode") or ""),
                hierarchy=str(cells.get("hierarchy") or ""),
                intro_source=str(cells.get("intro_source") or ""),
                seniority=str(cells.get("seniority") or ""),
                function=str(cells.get("function") or ""),
                warmth=cells.get("warmth"),
                labels=tuple(name for name in NOUL_LABELS if float(cells.get(name, 0.0)) >= ACTIVE_P),
                flag=str(label.flag or ""),
                share=decision.share,
                reason=decision.reason,
                share_source=decision.source,
                tags=tuple(sorted(split_tags(held.tags))) if held else (),
                is_owner=bool(cells.get("is_owner")),
                linkedin_only=bool(cells.get("linkedin_only")),
                group_chat_only=bool(cells.get("group_chat_only")),
                shared_employer=bool(cells.get("shared_employer")),
                shared_school=bool(cells.get("shared_school")),
                confidence=fact_rows[parent_id].confidence if parent_id in fact_rows else None,
            ))
        return tuple(rows)

    def avatar_url(self, person_id: str) -> str:
        imported = self.roster.get(person_id)
        return imported.avatar_url if imported else ""

    def detail(self, person_id: str) -> PersonDetail | None:
        imported = self.roster.get(person_id)
        if imported is None:
            return None
        parent_of_person = {row.person_id: row.parent_id for row in queries.people(self.db)}
        parent_id = self._parent_id(imported, parent_of_person)
        parent = next(iter(queries.parents(self.db, parent_id=parent_id)), None) if parent_id else None
        fact_rows = queries.facts(self.db, parent_id=parent_id, parent_owned=True) if parent_id else ()
        facts = parse_json_object(fact_rows[0].facts_json) if fact_rows else {}
        label = next((row for row in share_views.person_labels(self.db) if row.person_id == person_id), None)
        cells = parse_json_object(label.labels_json) if label else {}
        held = next((row for row in share_views.person_tags(self.db) if row.person_id == person_id), None)
        dossier = ""
        for artifact in queries.artifacts(self.db, kind=ArtifactKind.DOSSIER.value, parent_id=parent_id):
            body = str(parse_json_object(artifact.payload_json).get("body") or "")
            if body and (artifact.person_id is None or not dossier):
                dossier = body
        return PersonDetail(
            person_id=person_id,
            linkedin_url=imported.linkedin_url,
            headline=imported.headline,
            avatar_url=imported.avatar_url,
            worth_reason=str((parent.machine_worth_reason if parent else None)
                             or (facts.get("network_worth") or {}).get("reason") or ""),
            note=str((held.note if held else None) or ""),
            dossier_html=markdown_to_html(dossier) if dossier else "",
            probabilities={name: float(cells[name]) for name in NOUL_LABELS if name in cells},
            choice_p={name: float(cells[f"{name}_p"]) for name in CHOICE_LABELS if f"{name}_p" in cells},
            worth_note=str((parent.human_worth_note if parent else None) or ""),
            relationship_to_owner=str(facts.get("relationship_to_owner") or ""),
            topics=tuple(str(topic) for topic in facts.get("topics") or []),
            employers=tuple(
                " · ".join(part for part in (str(row.get("role") or ""), str(row.get("name") or "")) if part)
                for row in facts.get("employers") or [] if isinstance(row, dict)),
            school=str(facts.get("school") or ""),
            emails=imported.emails,
            phones=imported.phones,
        )

    @staticmethod
    def _parent_id(imported: ImportedPerson, parent_of_person: dict[str, str]) -> str:
        identities = (imported.person_id, *imported.superseded_person_ids)
        parent_ids = sorted({parent_of_person[key] for key in identities if key in parent_of_person})
        return parent_ids[0] if parent_ids else ""


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
