"""The People page's read boundary: one typed row per family, joined once from the v2 store.

Flow: the store's `families` table (realize's row per family: name, profile fields, channels, counts) +
its `person_labels`, `current_tags` and `current_worth` -> `SharePerson` rows -> one JSON payload for the page. `detail(id)` adds what only the drawer
shows: the dossier (the review card's fragment), every label cell, the worth reason, the contact
identifiers.

The list row is stored on the families table (schema 12) and the page reads it a slice at a time with its
filters, search and sort in SQL; the row carries only what a facet or a column reads; the 27 probabilities
and the dossier stay per-person.

Changelog:
  2026-11-03: the row is derived once per change and stored on families; `page` filters, sorts and slices in SQL.
  2026-10-23: the roster is the families table realize writes; no in-memory rows, no rebuild per request.
  2026-10-07: v2. A row is a family on its parent id; the roster is realize's rows.
  2026-09-26: created.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, astuple, dataclass, fields
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context_v2.db import queries_review, queries_share
from packs.ingestion.primitives.deep_context_v2.db.queries_share import Labels, Tags
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso
from packs.ingestion.primitives.deep_context_v2.enrich.profiles import profile_from_record
from packs.ingestion.primitives.deep_context_v2.realize.realize import Realize
from packs.ingestion.primitives.deep_context_v2.review import payloads
from packs.ingestion.primitives.deep_context_v2.review.queue import load_card
from packs.ingestion.primitives.share.labels import ACTIVE_P
from packs.ingestion.primitives.share.questions import CHOICE_LABELS, NOUL_LABELS
from packs.ingestion.primitives.share.store import split_tags
from packs.ingestion.schemas.linkedin_profile_normalizer import normalize_linkedin_profile
from packs.ingestion.schemas.people_schema import parse_interaction_counts

# A people.csv channel name -> the channel the page shows (one icon per family).
CHANNEL_FAMILIES = {
    "gmail_msgvault": "gmail",
    "gmail": "gmail",
    "imessage": "imessage",
    "imessage_group": "imessage",
    "whatsapp": "whatsapp",
    "whatsapp_group": "whatsapp",
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
    # The imported LinkedIn profile, in the review card's lines; empty for a family without one.
    summary: str
    experiences: tuple[str, ...]
    education: tuple[str, ...]


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


# The People row as the families table stores it (schema.py PEOPLE_ROW_COLUMNS): lists as JSON arrays.
STORED_COLUMNS = ("in_progress", "channels", "interactions", "recency_days", "cadence", "direction", "worth",
                  "worth_source", "relationship_kind", "mode", "hierarchy", "intro_source", "seniority", "function",
                  "warmth", "labels", "flag", "reason", "tags", "is_owner", "linkedin_only", "group_chat_only",
                  "shared_employer", "shared_school")
_LISTS = ("channels", "labels", "tags")
_FLAGS = ("in_progress", "is_owner", "linkedin_only", "group_chat_only", "shared_employer", "shared_school")


def _stored(value: Any) -> Any:
    return json.dumps(list(value)) if isinstance(value, tuple) else value


def _person(row: sqlite3.Row) -> SharePerson:
    """A families row as the page's row."""
    cells = {column: row[column] for column in STORED_COLUMNS}
    cells.update({column: tuple(json.loads(row[column])) for column in _LISTS})
    cells.update({column: bool(row[column]) for column in _FLAGS})
    return SharePerson(parent_id=row["parent_id"], public_identifier=row["public_identifier"], name=row["name"],
                       avatar_url=row["profile_picture_url"], title=row["title"], company=row["company"],
                       location=row["location"], last_interaction=row["last_interaction"], confidence=None, **cells)


# The page's buckets (web/src/types/people.ts LAST and WARMTH), as SQL over the stored row.
LAST_BUCKET = ("CASE WHEN recency_days IS NULL THEN 'Never' WHEN recency_days < 365 THEN '< 1 year' "
               "WHEN recency_days <= 730 THEN '1–2 years' ELSE '> 2 years' END")
# Warmth: the model level nearest the score.
WARMTH_BUCKET = ("CASE WHEN warmth IS NULL THEN '' WHEN warmth < 0.5 THEN 'No relationship' WHEN warmth < 1.5 THEN "
                 "'Distant' WHEN warmth < 2.5 THEN 'Friendly' WHEN warmth < 3.5 THEN 'Close' ELSE 'Inner circle' END")
EVIDENCE = {"No relationship labels": "linkedin_only", "Group chats only": "group_chat_only",
            "Shared employer": "shared_employer", "Shared school": "shared_school"}

# The family's decision as the page counts and lists it: a LinkedIn-only family nobody decided is a Keep (there is
# nothing to assess it on), so Not assessed counts only people with more than a LinkedIn.
DECISION = "CASE WHEN worth != '' THEN worth WHEN linkedin_only THEN 'yes' ELSE 'unjudged' END"

# A facet (web/src/lib/people/facets.ts FACETS) -> the SQL value a held value is matched against; lists match
# when any of their items is held.
FACET_VALUES = {
    "reason": "reason",
    "worth": DECISION,
    "relationship_kind": "relationship_kind",
    "last": LAST_BUCKET,
    "linkedin": "CASE WHEN public_identifier != '' THEN 'Has LinkedIn' ELSE 'No LinkedIn' END",
    "worth_source": "worth_source",
    "warmth": WARMTH_BUCKET,
    "direction": "direction",
}
FACET_LISTS = ("channels", "tags", "labels")
HIDES = {"linkedin_only": "linkedin_only"}
SORTS = {
    "name": "name COLLATE NOCASE",
    "relationship": "CASE relationship_kind WHEN '' THEN '~' ELSE relationship_kind END",
    "worth": "CASE worth WHEN 'yes' THEN 0 WHEN 'no' THEN 1 ELSE 2 END",
    "warmth": "-COALESCE(warmth, -1)",
    "last": "COALESCE(recency_days, 1000000000)",
    "messages": "-interactions",
}
TABS = ("all", "yes", "no", "unjudged")


def _tab(tab: str) -> tuple[str, tuple[str, ...]]:
    if tab == "all":
        return "1", ()
    return f"{DECISION} = ?", (tab,)


def _held(values: tuple[str, ...]) -> str:
    return "(" + ",".join("?" * len(values)) + ")"


@dataclass(frozen=True)
class PeopleQuery:
    """The page's view as the query string writes it (web/src/lib/people/view.ts): tab, q, sort, dir,
    filter.<facet> (repeated), show / hide (a hide the owner turned off / on), and the slice. A hide left
    alone is on while anyone else would show, so a LinkedIn-only network does not open to an empty list."""

    tab: str
    text: str
    sort: str
    desc: bool
    filters: tuple[tuple[str, tuple[str, ...]], ...]
    hide: tuple[str, ...]
    forced: tuple[str, ...]  # the hides the owner turned on: they apply even when nobody is left
    offset: int
    limit: int
    ids: bool
    person: str  # `id`: that one family, for a drawer opened on a row not in a loaded slice

    @classmethod
    def parse(cls, query: dict[str, list[str]]) -> "PeopleQuery":
        one = lambda key, default: (query.get(key) or [default])[0]  # noqa: E731
        facets = (*FACET_VALUES, *FACET_LISTS, "logbook", "evidence")
        return cls(
            tab=one("tab", "all") if one("tab", "all") in TABS else "all",
            text=one("q", "").strip().lower(),
            sort=one("sort", "warmth") if one("sort", "warmth") in SORTS else "warmth",
            desc=one("dir", "") == "desc",
            filters=tuple((key, tuple(query[f"filter.{key}"])) for key in facets if query.get(f"filter.{key}")),
            hide=tuple(key for key in HIDES if key not in query.get("show", [])),
            forced=tuple(key for key in HIDES if key in query.get("hide", []) and key not in query.get("show", [])),
            offset=max(0, int(one("offset", "0"))),
            limit=min(500, max(1, int(one("limit", "200")))),
            ids=one("ids", "") == "1",
            person=one("id", ""),
        )

    def where(self, logbooks: frozenset[str]) -> tuple[list[str], list[Any]]:
        """The search text and the held facets as SQL conditions and their arguments."""
        where: list[str] = []
        args: list[Any] = []
        if self.person:
            where.append("parent_id = ?")
            args.append(self.person)
        if self.text:
            where.append("instr(lower(name || ' ' || title || ' ' || company || ' ' || location), ?) > 0")
            args.append(self.text)
        for key, values in self.filters:
            if key in FACET_VALUES:
                where.append(f"{FACET_VALUES[key]} IN {_held(values)}")
                args.extend(values)
            elif key in FACET_LISTS:
                where.append(f"EXISTS (SELECT 1 FROM json_each({key}) WHERE value IN {_held(values)})")
                args.extend(values)
            elif key == "evidence":
                where.append("(" + " OR ".join(f"{EVIDENCE[value]} = 1" for value in values if value in EVIDENCE)
                             + " OR 0)")
            else:  # logbook: the families with a saved logbook, from the logbook catalog
                held = [f"parent_id {'' if value == 'Has logbook' else 'NOT '}IN {_held(tuple(logbooks))}"
                        for value in values]
                where.append("(" + " OR ".join(held) + ")")
                args.extend([parent for _ in values for parent in logbooks])
        return where, args


@dataclass(frozen=True)
class PeoplePage:
    """GET /api/people/page: how many match, the tab totals and relationship counts, and the slice (or every
    matching id)."""

    matching: int
    totals: dict[str, int]
    kinds: dict[str, int]
    rows: tuple[SharePerson, ...]
    ids: tuple[str, ...]
    hidden: tuple[str, ...] = ()  # the hides applied: the Filters menu shows these as on


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

    def _derive(self, parents: tuple[str, ...]) -> tuple[SharePerson, ...]:
        """The People row of every family, or with `parents` only those: realize's columns joined with the
        family's labels, tags and worth. A family realize wrote is shown even with no message context or labels."""
        rows: dict[str, sqlite3.Row] = queries_share.families(self.conn, parents)
        labels: dict[str, Labels] = queries_share.labels_by_parent(self.conn, parents)
        tags: dict[str, Tags] = queries_share.current_tags(self.conn, parents)
        relationships = queries_share.relationships_by_parent(self.conn, parents)
        worth: dict[str, sqlite3.Row] = {}
        for row in self.conn.execute(
                "SELECT parent_id, worth, decided_by, reason FROM current_worth "
                + ("WHERE parent_id IN (" + ",".join("?" * len(parents)) + ")" if parents else ""), parents):
            worth[row["parent_id"]] = row
        deleted: set[str] = queries_share.deleted_parents(self.conn)
        people: list[SharePerson] = []
        for parent_id, row in rows.items():
            label = labels.get(parent_id)
            if parent_id in deleted:
                continue
            cells = parse_json_object(label.labels_json) if label else {}
            kind = relationships.get(parent_id, str(cells.get("relationship_kind") or ""))
            flag = label.flag if label else kind if kind in ("family", "romantic_partner") else ""
            held = tags.get(parent_id)
            decision = worth.get(parent_id)
            people.append(SharePerson(
                parent_id=parent_id,
                public_identifier=row["public_identifier"],
                in_progress=label is not None and row["public_identifier"] != label.public_identifier,
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
                relationship_kind=kind,
                mode=str(cells.get("mode") or ""),
                hierarchy=str(cells.get("hierarchy") or ""),
                intro_source=str(cells.get("intro_source") or ""),
                seniority=str(cells.get("seniority") or ""),
                function=str(cells.get("function") or ""),
                warmth=cells.get("warmth"),
                labels=tuple(name for name in NOUL_LABELS if float(cells.get(name, 0.0)) >= ACTIVE_P),
                flag=flag,
                reason=_reason(bool(cells.get("is_owner")), decision, flag),
                tags=tuple(sorted(split_tags(held.tags))) if held else (),
                is_owner=bool(cells.get("is_owner")),
                linkedin_only=_families(tuple(row["source_channels"].split(","))) == ("linkedin",),
                group_chat_only=bool(cells.get("group_chat_only")),
                shared_employer=bool(cells.get("shared_employer")),
                shared_school=bool(cells.get("shared_school")),
                confidence=None,
            ))
        return tuple(people)

    def refresh(self) -> None:
        """Derive the People row of every family a write marked (`derived = 0`, schema.py's triggers) and store it
        on the families table, so a read is a SELECT."""
        stale = tuple(row[0] for row in self.conn.execute("SELECT parent_id FROM families WHERE derived = 0"))
        if not stale:
            return
        total = self.conn.execute("SELECT COUNT(*) FROM families").fetchone()[0]
        # Every family at once reads each input table once; a few read only theirs.
        people = self._derive(() if len(stale) * 4 > total else stale)
        derived = {person.parent_id for person in people}
        with self.conn:
            # A family a merge emptied (or deleted) is no longer one: realize would not write it again.
            self.conn.executemany("DELETE FROM families WHERE parent_id = ?",
                                  [(parent_id,) for parent_id in stale if parent_id not in derived])
            self.conn.executemany(
                f"UPDATE families SET derived = 1, {', '.join(f'{column} = ?' for column in STORED_COLUMNS)} "
                "WHERE parent_id = ?",
                [(*(_stored(getattr(person, column)) for column in STORED_COLUMNS), person.parent_id)
                 for person in people])

    def load(self, parents: tuple[str, ...] = ()) -> tuple[SharePerson, ...]:
        """Every family's row, or with `parents` only those families' rows: what a write answers with."""
        self.refresh()
        return tuple(_person(row) for row in self.conn.execute(
            "SELECT * FROM families"
            + (" WHERE parent_id IN (" + ",".join("?" * len(parents)) + ")" if parents else "")
            + " ORDER BY parent_id", parents))

    def page(self, query: PeopleQuery, logbooks: frozenset[str]) -> PeoplePage:
        """One page of the list for the page's view: its filters, search and sort in SQL; the tab totals and the
        relationship counts over the tab; `ids` alone when asked (select-all)."""
        self.refresh()
        tab_where, tab_args = _tab(query.tab)
        where, args = query.where(logbooks)
        hides = tuple(hide for hide in query.hide if hide in query.forced
                      or self.conn.execute(f"SELECT 1 FROM families WHERE NOT {HIDES[hide]} LIMIT 1").fetchone())
        clause = " AND ".join([tab_where, *where, *(f"NOT {HIDES[hide]}" for hide in hides)])
        params = (*tab_args, *args)
        totals = dict(self.conn.execute(
            f"SELECT {DECISION}, COUNT(*) FROM families GROUP BY 1").fetchall())
        kinds = dict(self.conn.execute(
            f"SELECT relationship_kind, COUNT(*) FROM families WHERE {tab_where} AND relationship_kind != '' "
            "GROUP BY 1", tab_args).fetchall())
        matching = self.conn.execute(f"SELECT COUNT(*) FROM families WHERE {clause}", params).fetchone()[0]
        order = f"{SORTS[query.sort]} {'DESC' if query.desc else 'ASC'}, name COLLATE NOCASE, parent_id"
        if query.ids:
            ids = tuple(row[0] for row in self.conn.execute(
                f"SELECT parent_id FROM families WHERE {clause} ORDER BY {order}", params))
            return PeoplePage(matching, totals, kinds, (), ids, hides)
        rows = tuple(_person(row) for row in self.conn.execute(
            f"SELECT * FROM families WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, query.limit, query.offset)))
        return PeoplePage(matching, totals, kinds, rows, (), hides)

    def detail(self, parent_id: str) -> PersonDetail | None:
        row = queries_share.families(self.conn, (parent_id,)).get(parent_id)
        if row is None or parent_id in queries_share.deleted_parents(self.conn):
            return None
        members = queries_review.family_members(self.conn, parent_id)
        ids = [member.candidate_id for member in members]
        has_facts = bool(queries_review.member_facts(self.conn, parent_id, ids))
        card = load_card(self.conn, self.data_root, parent_id) if has_facts else None
        facts = card.facts.to_payload() if card else {}
        label = queries_share.labels_by_parent(self.conn, (parent_id,)).get(parent_id)
        cells = parse_json_object(label.labels_json) if label else {}
        relationships = queries_share.relationships_by_parent(self.conn, (parent_id,))
        held = queries_share.current_tags(self.conn, (parent_id,)).get(parent_id)
        worth = self.conn.execute("SELECT reason FROM current_worth WHERE parent_id = ?", (parent_id,)).fetchone()
        imported = self._imported_profile(parent_id, row["linkedin_url"])
        name = row["name"]
        known_as = (str(facts.get("canonical_name") or ""), *(member.display_name for member in members),
                    *(str(alias) for alias in facts.get("aliases") or []))
        emails: list[str] = []
        phones: list[str] = []
        for identifier in card.identifiers if card else queries_review.identifiers(self.conn, ids):
            (emails if identifier.kind == "email" else phones).append(identifier.display_value)
        return PersonDetail(
            parent_id=parent_id,
            linkedin_url=row["linkedin_url"],
            headline=row["headline"],
            avatar_url=row["profile_picture_url"],
            worth_reason=str(worth["reason"] if worth else ""),
            note=held.note if held else "",
            dossier_html=payloads.dossier(card) if card else "",
            probabilities={name: float(cells[name]) for name in NOUL_LABELS if name in cells},
            choice_p={name: float(cells[f"{name}_p"]) for name in CHOICE_LABELS if f"{name}_p" in cells},
            worth_note="",
            relationship_to_owner=str(facts.get("relationship_to_owner") or ""),
            relationship_kind_source="human" if parent_id in relationships else label.relationship_kind_source if label else "",
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
            summary=imported.get("summary", ""),
            experiences=imported.get("experiences", ()),
            education=imported.get("education", ()),
        )

    def _imported_profile(self, parent_id: str, url: str) -> dict[str, Any]:
        """The family's imported LinkedIn profile as the review card draws it (review/decisions.py
        `fetch_profile` builds the same lines): the About text, the work lines, the education lines."""
        if not parent_id.startswith("li:"):
            return {}
        row = self.conn.execute("SELECT import_json FROM candidates WHERE candidate_id = ?",
                                ("candidate:linkedin:" + parent_id[3:],)).fetchone()
        if row is None:
            return {}
        normalized = normalize_linkedin_profile(json.loads(json.loads(row["import_json"])["rapidapi_response"]))
        record = {"normalized_profile": normalized, "fetched_at": hashlib.sha256(row["import_json"].encode()).hexdigest()}
        profile = profile_from_record(url, record)
        return {"summary": str(normalized.get("summary") or "").strip(), "experiences": profile.experiences,
                "education": profile.education}


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


def page_payload(page: PeoplePage) -> dict[str, Any]:
    """GET /api/people/page: the slice as columnar rows, as `people_payload` sends them."""
    return {
        "matching": page.matching,
        "totals": page.totals,
        "kinds": page.kinds,
        "columns": list(PEOPLE_COLUMNS),
        "rows": [list(astuple(row)) for row in page.rows],
        "ids": list(page.ids),
        "hidden": list(page.hidden),
    }


def people_payload(rows: tuple[SharePerson, ...]) -> dict[str, Any]:
    """The page's one payload: the header counts, the column names once, one array per person.
    Columnar because 28k rows of repeated keys is 22 MB; the same rows as arrays are under 9 MB."""
    return {
        "counts": asdict(ShareCounts.of(rows)),
        "columns": list(PEOPLE_COLUMNS),
        "rows": [list(astuple(row)) for row in rows],
    }

