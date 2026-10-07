"""A family's evidence for worth: its facts as one, its message counts, and the key that says it is unchanged.

Created: 2026-10-06
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from packs.ingestion.primitives.deep_context_v2.collect.bundle import MessageDirection
from packs.ingestion.primitives.deep_context_v2.db.queries_worth import ChannelCount, MemberFacts
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import SynthesizedFacts, collapse
from packs.ingestion.primitives.deep_context_v2.worth import jev


def family_facts(members: list[MemberFacts]) -> SynthesizedFacts:
    """The family's facts: one member's as they are, several members' collapsed into one."""
    facts: list[SynthesizedFacts] = []
    for member in members:
        facts.append(SynthesizedFacts.from_payload(json.loads(member.facts_json)))
    if len(facts) == 1:
        return facts[0]
    return collapse(facts)


def reference_date(members: list[MemberFacts]) -> str:
    """When the family's newest facts were synthesized: the request changes with its evidence, not the day."""
    latest: str = ""
    for member in members:
        latest = max(latest, member.synthesized_at)
    return latest[:10]


def channel_summary(members: list[MemberFacts], sources: dict[str, list[str]],
                     counts: dict[str, list[ChannelCount]], groups: dict[str, int]) -> jev.ChannelSummary:
    """The members' message counts added together: per channel, from me and from them, first and last."""
    family_sources: set[str] = set()
    per_channel: dict[str, int] = {}
    timestamps: list[str] = []
    from_me: int = 0
    from_them: int = 0
    group_count: int = 0
    for member in members:
        family_sources.update(sources[member.candidate_id])
        group_count += groups[member.candidate_id]
        for count in counts.get(member.candidate_id, []):
            per_channel[count.channel] = per_channel.get(count.channel, 0) + count.messages
            if count.direction == MessageDirection.FROM_ME:
                from_me += count.messages
            elif count.direction == MessageDirection.FROM_THEM:
                from_them += count.messages
            if count.first_at is not None:
                timestamps.append(count.first_at)
                timestamps.append(count.last_at)
    return jev.ChannelSummary(tuple(sorted(family_sources)), per_channel, min(timestamps, default=None),
                              max(timestamps, default=None), from_me, from_them, group_count)


def family_key(members: list[MemberFacts]) -> str:
    """The reuse key: the request version and the family's member ids. A family is judged once; only a
    new member (a merge) makes a new family and a new judgment. New messages never do."""
    member_ids: list[str] = []
    for member in members:
        member_ids.append(member.candidate_id)
    payload: dict[str, Any] = {"version": jev.REQUEST_VERSION, "members": sorted(member_ids)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def family_names(members: list[MemberFacts], names: dict[str, str]) -> list[str]:
    """Every member's written name, once each. A member with an empty name adds none."""
    family: list[str] = []
    for member in members:
        name: str = names.get(member.candidate_id, "")
        if name and name not in family:
            family.append(name)
    return family
