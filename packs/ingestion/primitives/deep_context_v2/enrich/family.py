"""A family as enrich sees it, and what a confirmed LinkedIn writes for one.

A family is the candidates on one current parent. Enrich only looks at families with a worth row:
worth is written for families with facts, so every family here has facts.

Created: 2026-10-07
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from packs.ingestion.primitives.deep_context_v2.db import queries_dedupe, queries_enrich, queries_worth
from packs.ingestion.primitives.deep_context_v2.db.queries import display_names
from packs.ingestion.primitives.deep_context_v2.db.queries_dedupe import Identifier, ParentRow
from packs.ingestion.primitives.deep_context_v2.db.queries_enrich import FamilyWorth, LinkedinRow, LinkedinVerdict
from packs.ingestion.primitives.deep_context_v2.db.queries_worth import MemberFacts
from packs.ingestion.primitives.deep_context_v2.db.schema import (
    LINKEDIN_PARENT_PREFIX, MINTED_PARENT_PREFIX, DecidedBy, IdentifierKind, MergeReason, Verdict,
)
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import SynthesizedFacts
from packs.ingestion.primitives.deep_context_v2.worth import evidence


@dataclass(frozen=True)
class Family:
    parent_id: str
    candidates: tuple[str, ...]          # every candidate on the parent: every write goes to each
    members: tuple[MemberFacts, ...]     # the members with facts: every judge reads these
    facts_fingerprints: tuple[str, ...]  # the members' facts fingerprints, sorted
    worth: FamilyWorth
    names: tuple[str, ...]               # every member's written name, once each
    identifiers: tuple[Identifier, ...]  # every member's normalized emails and phones
    facts: SynthesizedFacts              # the members' facts collapsed into one
    messages: int                        # messages across the members' bundles
    verdicts: tuple[LinkedinVerdict, ...]  # the members' current LinkedIn verdicts

    @property
    def minted(self) -> bool:
        """Still on a p: id: no LinkedIn confirmed yet."""
        return self.parent_id.startswith(MINTED_PARENT_PREFIX)

    @property
    def human_linkedin(self) -> bool:
        """A human answered a LinkedIn question for a member: after review only review touches the family."""
        for verdict in self.verdicts:
            if verdict.decided_by == DecidedBy.HUMAN:
                return True
        return False

    def emails(self) -> set[str]:
        found: set[str] = set()
        for identifier in self.identifiers:
            if identifier.kind == IdentifierKind.EMAIL:
                found.add(identifier.normalized_value)
        return found



def load_families(conn: sqlite3.Connection) -> list[Family]:
    """Every family with a worth row, with its evidence read once for all steps."""
    # Group the candidates with facts by their current parent.
    by_family: dict[str, list[MemberFacts]] = {}
    for member in queries_worth.members_with_facts(conn):
        by_family.setdefault(member.family_key, []).append(member)
    worth: dict[str, FamilyWorth] = queries_enrich.family_worth(conn)
    names: dict[str, str] = display_names(conn)
    identifiers: dict[str, list[Identifier]] = queries_dedupe.candidate_identifiers(conn)
    fingerprints: dict[str, str] = queries_dedupe.facts_fingerprints(conn)
    # Every candidate on each parent, with facts or not.
    candidates: dict[str, list[str]] = {}
    for candidate_id, parent_id in sorted(queries_dedupe.current_parents(conn).items()):
        candidates.setdefault(parent_id, []).append(candidate_id)
    # Messages per candidate: the bundle counts added over channel and direction.
    messages: dict[str, int] = {}
    for count in queries_worth.bundle_counts(conn).channels:
        messages[count.candidate_id] = messages.get(count.candidate_id, 0) + count.messages
    verdicts: dict[str, list[LinkedinVerdict]] = {}
    for verdict in queries_enrich.current_linkedins(conn):
        verdicts.setdefault(verdict.candidate_id, []).append(verdict)

    families: list[Family] = []
    for parent_id, members in sorted(by_family.items()):
        if parent_id not in worth:
            continue  # worth has not judged this family: nothing for enrich to do
        family_identifiers: list[Identifier] = []
        family_messages: int = 0
        family_verdicts: list[LinkedinVerdict] = []
        family_fingerprints: list[str] = []
        for member in members:
            family_fingerprints.append(fingerprints[member.candidate_id])
        for candidate_id in candidates[parent_id]:
            family_identifiers.extend(identifiers.get(candidate_id, []))
            family_messages += messages.get(candidate_id, 0)
            family_verdicts.extend(verdicts.get(candidate_id, []))
        families.append(Family(
            parent_id, tuple(candidates[parent_id]), tuple(members), tuple(sorted(family_fingerprints)), worth[parent_id], tuple(evidence.family_names(members, names)),
            tuple(family_identifiers), evidence.family_facts(members), family_messages, tuple(family_verdicts),
        ))
    return families


def judgment_fingerprint(family: Family, profiles: list[tuple[str, str, str]], version: str) -> str:
    """What a LinkedIn judgment saw: the members' facts, every proposed URL with where it came from and the
    time its profile was fetched, and the judge. The URL set is every proposal with a profile, whatever was
    decided about any of them, so a family's own verdicts never change its key; a URL that was a research
    result and is now a connection is new evidence."""
    payload: dict[str, object] = {"facts": list(family.facts_fingerprints), "profiles": sorted(profiles), "version": version}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def confirm(conn: sqlite3.Connection, family: Family, url: str, member_id: str, origin: str,
            fingerprint: str, now: str) -> list[ParentRow]:
    """A confirmed LinkedIn: one confirmed machine verdict per member, and the parent rows that move every
    member onto li:<member id>, each naming its own member's verdict. Two families confirmed on one
    LinkedIn become one parent by this alone."""
    rows: list[ParentRow] = []
    for candidate_id in family.candidates:
        row: LinkedinRow = (candidate_id, url, member_id, origin, Verdict.CONFIRMED.value, DecidedBy.MACHINE.value,
                            fingerprint, now)
        seq: int = queries_enrich.insert_linkedin(conn, row)
        rows.append((candidate_id, LINKEDIN_PARENT_PREFIX + member_id, MergeReason.JUDGE_CONFIRMED.value,
                     f"candidate_linkedins:{seq}", now))
    return rows


def family_evidence(family: Family) -> dict[str, Any]:
    """The family's evidence: its written names, facts, emails, phones and message count."""
    emails: list[str] = []
    phones: list[str] = []
    for identifier in family.identifiers:
        if identifier.kind == IdentifierKind.EMAIL:
            emails.append(identifier.normalized_value)
        else:
            phones.append(identifier.normalized_value)
    return {"source_names": list(family.names), "facts": family.facts.to_payload(), "emails": sorted(emails),
            "phones": sorted(phones), "message_count": family.messages}
