"""The Check LinkedIn list and one family's card, as typed records.

The list is every worth-yes family on a p: id with no human LinkedIn row for any member that has
something to decide: a needs_review LinkedIn, or a synthetic card (a usable no_match research row at the
family's current evidence handle). A family research found nothing for is not listed: there is nothing to
review (decided 2026-10-07); it stays worth yes and is exported without a URL.

The card is the family: every member's name, identifiers, channels and message counts rolled up, the
members' facts collapsed into one, and what is pending with its profile.

Created: 2026-10-07
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context_v2.db import queries_review
from packs.ingestion.primitives.deep_context_v2.db.queries_review import (
    CurrentVerdict, IdentifierRow, Member, ResearchCard,
)
from packs.ingestion.primitives.deep_context_v2.db.queries_worth import MemberFacts
from packs.ingestion.primitives.deep_context_v2.db.schema import SYNTHETIC_PROFILE_PREFIX, Origin, Verdict
from packs.ingestion.primitives.deep_context_v2.enrich.profiles import Profile, Profiles, load_profiles
from packs.ingestion.primitives.deep_context_v2.enrich.research import handle as research_handle
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import SynthesizedFacts
from packs.ingestion.primitives.deep_context_v2.worth.evidence import family_facts


@dataclass(frozen=True)
class Pending:
    """One profile the human can say Yes to: a LinkedIn the judge left at needs_review, or a synthetic card."""

    key: str              # what the page posts back: the member id, or the synthetic key
    linkedin_url: str     # the URL, or the synthetic key
    member_id: str
    origin: str           # kept on the human row: where the URL came from
    fingerprint: str      # the judgment the human answers
    profile: Profile | None           # the cached LinkedIn profile; None when not cached, or a synthetic card
    research: dict[str, Any] | None   # the synthetic card's research content; None for a LinkedIn


@dataclass(frozen=True)
class Card:
    parent_id: str
    members: tuple[Member, ...]
    identifiers: tuple[IdentifierRow, ...]
    sources: tuple[str, ...]
    messages: dict[str, int]          # channel -> messages across the members
    facts: SynthesizedFacts
    labels_json: str | None           # the latest machine worth labels; None when worth never judged a member
    pending: tuple[Pending, ...]      # what the human can say Yes to; never empty for a listed family


def _synthetic(conn: sqlite3.Connection, facts: SynthesizedFacts) -> list[Pending]:
    """The usable research card at the family's current handle, whichever family's research wrote it; a
    card written at older evidence is not shown."""
    handle: str = research_handle(facts)
    found: list[Pending] = []
    card: ResearchCard | None = queries_review.research_card(conn, handle)
    if card is not None:
        key: str = SYNTHETIC_PROFILE_PREFIX + handle
        found.append(Pending(key, key, key, Origin.SYNTHETIC.value, handle, None, json.loads(card.result_json)["content"]))
    return found


def _needs_review(verdicts: list[CurrentVerdict], profiles: Profiles) -> list[Pending]:
    """Each LinkedIn a member's current verdict leaves at needs_review, once per member id."""
    found: list[Pending] = []
    seen: set[str] = set()
    for verdict in verdicts:
        if verdict.verdict != Verdict.NEEDS_REVIEW or verdict.member_id in seen:
            continue
        seen.add(verdict.member_id)
        found.append(Pending(verdict.member_id, verdict.linkedin_url, verdict.member_id, verdict.origin,
                             verdict.judgment_fingerprint, profiles.found.get(verdict.linkedin_url), None))
    return found


def load_card(conn: sqlite3.Connection, data_root: Path, parent_id: str) -> Card:
    members: list[Member] = queries_review.family_members(conn, parent_id)
    ids: list[str] = []
    for member in members:
        ids.append(member.candidate_id)
    facts_rows: list[MemberFacts] = queries_review.member_facts(conn, parent_id, ids)
    facts: SynthesizedFacts = family_facts(facts_rows)
    # What is pending: needs_review LinkedIns (profiles from the cache only) and the synthetic card.
    verdicts: list[CurrentVerdict] = queries_review.current_verdicts(conn, ids)
    urls: list[str] = []
    for verdict in verdicts:
        if verdict.verdict == Verdict.NEEDS_REVIEW:
            urls.append(verdict.linkedin_url)
    profiles: Profiles = load_profiles(data_root, urls, fetch=False)
    pending: list[Pending] = _needs_review(verdicts, profiles)
    pending.extend(_synthetic(conn, facts))
    return Card(parent_id, tuple(members), tuple(queries_review.identifiers(conn, ids)),
                tuple(queries_review.sources(conn, ids)), queries_review.message_counts(conn, ids), facts,
                queries_review.labels_json(conn, ids), tuple(pending))


def has_synthetic_card(conn: sqlite3.Connection, parent_id: str) -> bool:
    """A usable research row exists at the family's current handle."""
    ids: list[str] = []
    for member in queries_review.family_members(conn, parent_id):
        ids.append(member.candidate_id)
    facts: SynthesizedFacts = family_facts(queries_review.member_facts(conn, parent_id, ids))
    return bool(_synthetic(conn, facts))


def review_list(conn: sqlite3.Connection) -> list[str]:
    """The families with something to decide, in parent-id order."""
    listed: list[str] = []
    for row in queries_review.queue(conn):
        pending: bool = row.needs_review
        # A family with a usable research row under its own parent id is checked against its current
        # handle. (Two families with identical facts share one handle but only the researched one has the
        # row under its id; the other is missed here. None on Arthur's store.)
        if not pending and row.has_card:
            pending = has_synthetic_card(conn, row.parent_id)
        if pending:
            listed.append(row.parent_id)
    return listed
