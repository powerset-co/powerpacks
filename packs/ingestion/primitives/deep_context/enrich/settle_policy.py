"""Decide empty-profile detachment and whether a parent can be known.

Worth settlement lives in parents.machine_worth / machine_worth_reason, below
human worth and above the JEV-owned facts. JEV rewrites cannot undo it. Each
settlement pass recomputes it from the fact verdict, profiles and messages;
clearing these parent columns lifts the No when evidence arrives.

Changelog:
- 2026-10-01: settle empty lookup profiles and parents with too little history;
  an own LinkedIn connection is always worth Yes until a human says No.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

from packs.ingestion.primitives.deep_context.db.models import (
    ApprovedState, HumanWorth, LinkSnapshotRow, MachineWorth, ReviewAction,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.settlement import MachineIdentitySettlement
from packs.ingestion.primitives.deep_context.enrich.profiles.models import ProfileResult
from packs.ingestion.primitives.deep_context.synthesis.models import NetworkWorthFact
from packs.ingestion.primitives.enrich.rapidapi_client import PROFILE_ERROR

REVIEW_MESSAGE_BAR = 25
EMPTY_PROFILE_REASON = 'LinkedIn profile is empty'
UNKNOWN_PERSON_REASON = 'not enough to know who this is: no LinkedIn profile and '
OWN_CONNECTION_REASON = 'own LinkedIn connection'


def has_real_profile(profile: ProfileResult | None) -> bool:
    return profile is not None and profile.state != PROFILE_ERROR and profile.normalized_profile.present


def empty_profile_decision(
    link: LinkSnapshotRow, *, own_connection: bool, profile: ProfileResult | None,
) -> MachineIdentitySettlement | None:
    """The caller supplies accepted identities; human decisions remain intact."""
    if own_connection or link.decision_action or has_real_profile(profile):
        return None
    fingerprint = hashlib.sha256(
        (link.row_key + EMPTY_PROFILE_REASON + (profile.payload_json if profile else '')).encode()
    ).hexdigest()
    return replace(
        MachineIdentitySettlement.from_link(link),
        judgment_fingerprint=fingerprint,
        machine_action=ReviewAction.DETACH.value,
        machine_approved=ApprovedState.AUTO.value,
        machine_reason=EMPTY_PROFILE_REASON,
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
