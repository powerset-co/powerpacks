"""Decide empty-profile detachment and whether a parent can be known.

Worth settlement lives in parents.machine_worth / machine_worth_reason, below
human worth and above the JEV-owned facts. JEV rewrites cannot undo it. Each
settlement pass recomputes it from the fact verdict, profiles and messages;
clearing these parent columns lifts the No when evidence arrives.

Changelog:
- 2026-10-01: settle empty lookup profiles and parents with too little history;
  an own LinkedIn connection is always worth Yes until a human says No.
- 2026-10-02: two addresses of one LinkedIn profile (one member id) are one LinkedIn.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import replace
from typing import Iterable

from packs.ingestion.primitives.deep_context.db.models import (
    ApprovedState, HumanWorth, LinkSnapshotRow, MachineWorth, ReviewAction,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.settlement import MachineIdentitySettlement
from packs.ingestion.primitives.deep_context.enrich.profiles.models import ProfileResult
from packs.ingestion.primitives.deep_context.synthesis.models import NetworkWorthFact
from packs.ingestion.primitives.enrich.rapidapi_client import PROFILE_ERROR

REVIEW_MESSAGE_BAR = 25
EMPTY_PROFILE_REASON = 'LinkedIn profile is missing or empty'
SAME_PROFILE_REASON = 'same LinkedIn profile as '
UNKNOWN_PERSON_REASON = 'not enough to know who this is: no LinkedIn profile and '
OWN_CONNECTION_REASON = 'own LinkedIn connection'


def has_real_profile(profile: ProfileResult | None) -> bool:
    return profile is not None and profile.state != PROFILE_ERROR and profile.normalized_profile.present


def nothing_to_show(profile: ProfileResult | None) -> bool:
    """The profile was fetched and has nothing on it. One whose fetch failed, or that has not
    been fetched, may still have: the next run fetches it again."""
    return profile is not None and profile.state != PROFILE_ERROR and not profile.normalized_profile.present


def empty_profile_decision(
    link: LinkSnapshotRow, *, own_connection: bool, profile: ProfileResult | None,
) -> MachineIdentitySettlement | None:
    """For a LinkedIn the machine accepted or is unsure of; human decisions remain intact."""
    if own_connection or link.decision_action or has_real_profile(profile):
        return None
    return _machine_detach(link, EMPTY_PROFILE_REASON, profile.payload_json if profile else '')


def same_profile_decisions(
    accepted: Iterable[tuple[LinkSnapshotRow, ProfileResult | None]],
    unsure: Iterable[tuple[LinkSnapshotRow, ProfileResult | None]],
) -> list[MachineIdentitySettlement]:
    """Detach every unsure LinkedIn that is the same profile as one its person keeps.

    A person who renamed their LinkedIn has two addresses with one member id. An accepted
    address is kept; among unsure ones, the address LinkedIn itself answers to.
    """
    kept: dict[tuple[str, str], LinkSnapshotRow] = {}
    for link, profile in accepted:
        member = _member(link, profile)
        if member:
            kept.setdefault(member, link)
    groups: dict[tuple[str, str], list[tuple[bool, str, LinkSnapshotRow]]] = defaultdict(list)
    for link, profile in unsure:
        member = _member(link, profile)
        if member and not link.decision_action:
            # An address that answers as another one is the old address: it sorts last.
            renamed = profile.normalized_profile.echoed_public_identifier is not None
            groups[member].append((renamed, link.row_key, link))

    decisions = []
    for member, group in groups.items():
        keep = kept.get(member) or min(group)[2]
        decisions.extend(
            _machine_detach(link, f'{SAME_PROFILE_REASON}{keep.public_identifier}')
            for _, row_key, link in group if row_key != keep.row_key
        )
    return decisions


def _member(link: LinkSnapshotRow, profile: ProfileResult | None) -> tuple[str, str] | None:
    """The LinkedIn profile behind an address: its person and LinkedIn's own member id."""
    if not has_real_profile(profile) or not profile.normalized_profile.member_id:
        return None
    return link.parent_id, profile.normalized_profile.member_id


def _machine_detach(link: LinkSnapshotRow, reason: str, evidence: str = '') -> MachineIdentitySettlement:
    fingerprint = hashlib.sha256((link.row_key + reason + evidence).encode()).hexdigest()
    return replace(
        MachineIdentitySettlement.from_link(link),
        judgment_fingerprint=fingerprint,
        machine_action=ReviewAction.DETACH.value,
        machine_approved=ApprovedState.AUTO.value,
        machine_reason=reason,
        machine_proposed_url=None,
        machine_proposed_public_identifier=None,
    )


def worth_decision(
    *, human_worth: HumanWorth | None, worth: MachineWorth,
    own_connection: bool, real_profile: bool, messages: int,
) -> NetworkWorthFact | None:
    """The parent's settled worth, or None when the worth pass's own verdict stands."""
    if human_worth is not None:
        return None
    # The owner chose to connect with this person: always in, until a human says no.
    if own_connection:
        return None if worth == MachineWorth.YES else NetworkWorthFact(MachineWorth.YES.value, OWN_CONNECTION_REASON)
    if worth == MachineWorth.NO:
        return None
    if real_profile or messages >= REVIEW_MESSAGE_BAR:
        return None
    return NetworkWorthFact(MachineWorth.NO.value, f'{UNKNOWN_PERSON_REASON}{messages} messages')
