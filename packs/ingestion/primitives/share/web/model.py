"""The People page's read boundary: one typed row per family, joined once from the v2 store.

Flow: the store's `families` table (realize's row per family: name, profile fields, channels, counts) +
its `person_labels`, `current_tags` and `current_worth` -> `SharePerson` rows -> one JSON payload for the page. `detail(id)` adds what only the drawer
shows: the dossier (the review card's fragment), every label cell, the worth reason, the contact
identifiers.

The page filters client-side, so the list row carries only what a facet or a column reads; the
27 probabilities and the dossier stay per-person.

Changelog:
  2026-10-23: the roster is the families table realize writes; no in-memory rows, no rebuild per request.
  2026-10-07: v2. A row is a family on its parent id; the roster is realize's rows.
  2026-09-26: created.
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, astuple, dataclass, fields
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context_v2.db import queries_share
from packs.ingestion.primitives.deep_context_v2.db.queries_share import Labels, Tags
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso
from packs.ingestion.primitives.deep_context_v2.realize.realize import Realize
from packs.ingestion.primitives.deep_context_v2.review import payloads
from packs.ingestion.primitives.deep_context_v2.review.queue import load_card
from packs.ingestion.primitives.share.labels import ACTIVE_P
from packs.ingestion.primitives.share.questions import CHOICE_LABELS, NOUL_LABELS
from packs.ingestion.primitives.share.store import split_tags
from packs.ingestion.schemas.people_schema import parse_interaction_counts

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
    """One list row per family: the export cells, the decision and the label cells a facet reads."""

    parent_id: str
    public_identifier: str
    in_progress: bool
    name: str
    avatar_url: str
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
    reason: str
    tags: tuple[str, ...]
    is_owner: bool
    linkedin_only: bool
    group_chat_only: bool
    shared_employer: bool
    shared_school: bool
    confidence: float | None


@dataclass(frozen=True)
class ShareCounts:
    people: int
    yes: int
    no: int

    @classmethod
    def of(cls, rows: tuple[SharePerson, ...]) -> "ShareCounts":
        return cls(
            people=len(rows),
            yes=sum(row.worth == "yes" for row in rows),
            no=sum(row.worth == "no" for row in rows),
        )


@dataclass(frozen=True)
class FactEvent:
    date: str
    summary: str


@dataclass(frozen=True)
class PersonDetail:
    """What the drawer shows beyond the list row."""

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
    relationship_kind_source: str
    events: tuple[FactEvent, ...]
    shared_context: tuple[str, ...]
    topics: tuple[str, ...]
    employers: tuple[str, ...]
    school: str
    location: str
    aliases: tuple[str, ...]
    emails: tuple[str, ...]
    phones: tuple[str, ...]
    interactions: dict[str, int]  # source family -> emails or messages, as the importer counted them


def _events(facts: dict[str, Any]) -> tuple[FactEvent, ...]:
    return tuple(FactEvent(str(row.get("date") or ""), str(row.get("summary") or ""))
                 for row in facts.get("notable_events") or [] if isinstance(row, dict))


def _shared_context(facts: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(row.get("detail") or "") for row in facts.get("shared_context") or []
                 if isinstance(row, dict) and row.get("detail"))


def _families(channels: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(CHANNEL_FAMILIES.get(channel, channel) for channel in channels if channel))


def _family_counts(counts: dict[str, int]) -> dict[str, int]:
    """The importer's per-channel counts added up per source family."""
    families: dict[str, int] = {}
    for channel, count in counts.items():
        family = CHANNEL_FAMILIES.get(channel, channel)
        families[family] = families.get(family, 0) + count
    return families


class SharePeople:
    """The families joined to the store: realize's `families` table (the roster: one row per family the owner has
    talked to, as the last realize wrote it) with `person_labels`, `current_tags` and `current_worth`. Construct
    once; `load()` re-reads the store, which changes as the human decides and tags."""

    def __init__(self, conn: sqlite3.Connection, data_root: Path) -> None:
        self.conn = conn
        self.data_root = data_root
        # A store from before the table (schema 8) has it empty: realize fills it once from what the store holds.
        if conn.execute("SELECT 1 FROM families LIMIT 1").fetchone() is None:
            Realize(conn, data_root).fill()

    def delete(self, parent_ids: tuple[str, ...]) -> None:
        """Hide each family from the page for good; `load` leaves deleted families out."""
        queries_share.delete_parents(self.conn, parent_ids, now_iso())

    def load(self, parents: tuple[str, ...] = ()) -> tuple[SharePerson, ...]:
        """Every family's row, or with `parents` only those families' rows: what a write answers with. A family
        realize wrote that the labels pass has not reached yet is not shown."""
        rows: dict[str, sqlite3.Row] = queries_share.families(self.conn, parents)
        labels: dict[str, Labels] = queries_share.labels_by_parent(self.conn, parents)
        tags: dict[str, Tags] = queries_share.current_tags(self.conn, parents)
        worth: dict[str, sqlite3.Row] = {}
        for row in self.conn.execute(
                "SELECT parent_id, worth, decided_by, reason FROM current_worth "
                + ("WHERE parent_id IN (" + ",".join("?" * len(parents)) + ")" if parents else ""), parents):
            worth[row["parent_id"]] = row
        deleted: set[str] = queries_share.deleted_parents(self.conn)
        people: list[SharePerson] = []
        for parent_id, row in rows.items():
            label = labels.get(parent_id)
            if label is None or parent_id in deleted:
                continue
            cells = parse_json_object(label.labels_json)
            held = tags.get(parent_id)
            decision = worth.get(parent_id)
            people.append(SharePerson(
                parent_id=parent_id,
                public_identifier=row["public_identifier"],
                in_progress=row["public_identifier"] != label.public_identifier,
                name=row["name"],
                avatar_url=row["profile_picture_url"],
                title=row["title"],
                company=row["company"],
                location=row["location"],
                channels=_families(tuple(row["source_channels"].split(","))),
                interactions=sum(parse_interaction_counts(row["interaction_counts"]).values()),
                last_interaction=row["last_interaction"],
                recency_days=cells.get("recency_days"),
                cadence=str(cells.get("cadence") or ""),
                direction=str(cells.get("direction") or ""),
                # A machine "maybe" from before the detectors (2026-10-28) is not a call: not assessed.
                worth=decision["worth"] if decision is not None and decision["worth"] != "maybe" else "",
                worth_source=decision["decided_by"] if decision is not None else "",
                relationship_kind=str(cells.get("relationship_kind") or ""),
                mode=str(cells.get("mode") or ""),
                hierarchy=str(cells.get("hierarchy") or ""),
                intro_source=str(cells.get("intro_source") or ""),
                seniority=str(cells.get("seniority") or ""),
                function=str(cells.get("function") or ""),
                warmth=cells.get("warmth"),
                labels=tuple(name for name in NOUL_LABELS if float(cells.get(name, 0.0)) >= ACTIVE_P),
                flag=label.flag,
                reason=_reason(bool(cells.get("is_owner")), decision, label.flag),
                tags=tuple(sorted(split_tags(held.tags))) if held else (),
                is_owner=bool(cells.get("is_owner")),
                linkedin_only=bool(cells.get("linkedin_only")),
                group_chat_only=bool(cells.get("group_chat_only")),
                shared_employer=bool(cells.get("shared_employer")),
                shared_school=bool(cells.get("shared_school")),
                confidence=None,
            ))
        return tuple(people)

    def detail(self, parent_id: str) -> PersonDetail | None:
        row = queries_share.families(self.conn, (parent_id,)).get(parent_id)
        if row is None or parent_id in queries_share.deleted_parents(self.conn):
            return None
        card = load_card(self.conn, self.data_root, parent_id)
        facts = card.facts.to_payload()
        label = queries_share.labels_by_parent(self.conn)[parent_id]
        cells = parse_json_object(label.labels_json)
        held = queries_share.current_tags(self.conn).get(parent_id)
        worth = self.conn.execute("SELECT reason FROM current_worth WHERE parent_id = ?", (parent_id,)).fetchone()
        name = row["name"]
        known_as = (str(facts.get("canonical_name") or ""), *(member.display_name for member in card.members),
                    *(str(alias) for alias in facts.get("aliases") or []))
        emails: list[str] = []
        phones: list[str] = []
        for identifier in card.identifiers:
            (emails if identifier.kind == "email" else phones).append(identifier.display_value)
        return PersonDetail(
            parent_id=parent_id,
            linkedin_url=row["linkedin_url"],
            headline=row["headline"],
            avatar_url=row["profile_picture_url"],
            worth_reason=str(worth["reason"] if worth else ""),
            note=held.note if held else "",
            dossier_html=payloads.dossier(card),
            probabilities={name: float(cells[name]) for name in NOUL_LABELS if name in cells},
            choice_p={name: float(cells[f"{name}_p"]) for name in CHOICE_LABELS if f"{name}_p" in cells},
            worth_note="",
            relationship_to_owner=str(facts.get("relationship_to_owner") or ""),
            relationship_kind_source=label.relationship_kind_source,
            events=_events(facts),
            shared_context=_shared_context(facts),
            topics=tuple(str(topic) for topic in facts.get("topics") or []),
            employers=tuple(
                " · ".join(part for part in (str(item.get("role") or ""), str(item.get("name") or "")) if part)
                for item in facts.get("employers") or [] if isinstance(item, dict)),
            school=str(facts.get("school") or ""),
            location=row["location"],
            aliases=tuple(dict.fromkeys(alias for alias in known_as if alias and alias != name)),
            emails=tuple(dict.fromkeys(emails)),
            phones=tuple(dict.fromkeys(phones)),
            interactions=_family_counts(parse_interaction_counts(row["interaction_counts"])),
        )


def _reason(is_owner: bool, decision: sqlite3.Row | None, flag: str) -> str:
    """The first rule that places the family, as the page's key: the owner, the human's call, a flag
    that keeps the family off the upload, else the machine's worth or none."""
    if is_owner:
        return "owner"
    if decision is not None and decision["decided_by"] == "human":
        return "human_worth"
    if flag:
        return flag
    return f"worth_{decision['worth']}" if decision is not None and decision["worth"] != "maybe" else "unjudged"


PEOPLE_COLUMNS = tuple(field.name for field in fields(SharePerson))


def people_payload(rows: tuple[SharePerson, ...]) -> dict[str, Any]:
    """The page's one payload: the header counts, the column names once, one array per person.
    Columnar because 28k rows of repeated keys is 22 MB; the same rows as arrays are under 9 MB."""
    return {
        "counts": asdict(ShareCounts.of(rows)),
        "columns": list(PEOPLE_COLUMNS),
        "rows": [list(astuple(row)) for row in rows],
    }

