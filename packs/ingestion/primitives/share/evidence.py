"""The share stage's read boundary: one `PersonEvidence` per family, joined once from the v2 store.

A family is a parent id with the members `current_parent` puts under it. Its export row is what
realize writes (name, public identifier, channels, counts, last interaction); its worth and the
JEV labels worth saved come from `current_worth`; its facts are the members' facts collapsed as
every judge sees them; its message statistics come from the members' bundles, counts only.

Changelog:
  2026-10-07: v2. people.csv, dossier artifacts and the v1 parents table are gone; the roster is
    realize's rows and the facts are the collapsed family facts.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context_v2.db import queries_worth
from packs.ingestion.primitives.deep_context_v2.db.queries_worth import ChannelCount, MemberFacts
from packs.ingestion.primitives.deep_context_v2.realize.realize import Realize
from packs.ingestion.primitives.deep_context_v2.worth.evidence import family_facts
from packs.ingestion.primitives.share.models import NO_MESSAGES, MessageStats, PersonEvidence
from packs.ingestion.schemas.people_schema import parse_interaction_counts

FROM_ME = "from_me"


def _messages(counts: list[ChannelCount], groups: int) -> MessageStats:
    """The family's message statistics from its members' channel counts."""
    if not counts and not groups:
        return NO_MESSAGES
    first: list[str] = []
    last: list[str] = []
    from_me: int = 0
    from_them: int = 0
    channels: list[str] = []
    for count in counts:
        if count.first_at:
            first.append(count.first_at)
        if count.last_at:
            last.append(count.last_at)
        if count.direction == FROM_ME:
            from_me += count.messages
        else:
            from_them += count.messages
        if count.channel not in channels:
            channels.append(count.channel)
    return MessageStats(first_at=min(first) if first else None, last_at=max(last) if last else None,
                        from_me=from_me, from_them=from_them, group_count=groups, channels=tuple(sorted(channels)))


class ShareEvidence:
    """The families joined to the store, parsed once at the boundary."""

    def __init__(self, conn: sqlite3.Connection, data_root: Path) -> None:
        self.conn = conn
        self.data_root = data_root

    def load(self) -> list[PersonEvidence]:
        rows, _counts = Realize(self.conn, self.data_root).build()
        members: dict[str, list[MemberFacts]] = {}
        for member in queries_worth.members_with_facts(self.conn):
            members.setdefault(member.family_key, []).append(member)
        worth: dict[str, sqlite3.Row] = {}
        for row in self.conn.execute("SELECT parent_id, worth, decided_by, labels_json FROM current_worth"):
            worth[row["parent_id"]] = row
        counts: queries_worth.BundleCounts = queries_worth.bundle_counts(self.conn)
        by_candidate: dict[str, list[ChannelCount]] = {}
        for count in counts.channels:
            by_candidate.setdefault(count.candidate_id, []).append(count)

        people: list[PersonEvidence] = []
        for row in rows:
            parent_id: str = row["id"]
            family: list[MemberFacts] = members[parent_id]
            facts: dict[str, Any] = family_facts(family).to_payload()
            channel_counts: list[ChannelCount] = []
            groups: int = 0
            for member in family:
                channel_counts.extend(by_candidate.get(member.candidate_id, []))
                groups += counts.groups.get(member.candidate_id, 0)
            decided = worth[parent_id]
            labels: dict[str, Any] | None = json.loads(decided["labels_json"]) if decided["labels_json"] else None
            overlaps: set[str] = set()
            for item in facts["shared_context"]:
                overlaps.add(item["overlap"])
            people.append(PersonEvidence(
                person_id=parent_id,
                candidate_ids=tuple(member.candidate_id for member in family),
                public_identifier=row["public_identifier"],
                full_name=row["full_name"],
                source_channels=tuple(channel for channel in row["source_channels"].split(",") if channel),
                interaction_counts=parse_interaction_counts(row["interaction_counts"]),
                last_interaction=row["last_interaction"] or None,
                network_worth=decided["worth"],
                worth_labels=labels,
                facts=facts,
                shared_overlaps=frozenset(overlaps),
                messages=_messages(channel_counts, groups),
            ))
        return people
