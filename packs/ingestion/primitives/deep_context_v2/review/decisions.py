"""What a human answer writes: Yes, Retarget, Skip. Each writes its rows for every member of the family in
one transaction; nothing is written for one member and forgotten for the others.

  Yes on a LinkedIn   every member: a confirmed human verdict (origin kept from the pending row) and a
                      parent row onto li:<member id>, reason human. Worth is untouched.
  Yes on a card       every member: a confirmed human verdict on synthetic:<handle>, origin synthetic.
                      No parent change: the family keeps its p: id.
  Retarget            the pasted URL's profile (cache first, one RapidAPI call on a miss), then the same
                      rows as Yes with origin human_override. No judge. A failed fetch writes nothing.
  Skip                every member: a wrong_person human verdict on each pending URL or card, so it is
                      never proposed again, and a human worth no.

Created: 2026-10-07
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from packs.ingestion.primitives.deep_context_v2.db import queries_review
from packs.ingestion.primitives.deep_context_v2.db.schema import LINKEDIN_PARENT_PREFIX, MergeReason, Origin, Verdict
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso
from packs.ingestion.primitives.deep_context_v2.enrich.profiles import Profile, Profiles, load_profiles
from packs.ingestion.primitives.deep_context_v2.review.queue import Card, Pending
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url

SKIP_REASON = "review: skip"


class DecisionError(Exception):
    """A decision that writes nothing; the words are what the page shows."""


def _confirm(conn: sqlite3.Connection, card: Card, url: str, member_id: str, origin: str, fingerprint: str,
             now: str) -> None:
    """Every member onto li:<member id>, each parent row naming its own member's verdict row."""
    for member in card.members:
        seq: int = queries_review.insert_linkedin(conn, member.candidate_id, url, member_id, origin,
                                                  Verdict.CONFIRMED.value, fingerprint, now)
        queries_review.insert_parent(conn, member.candidate_id, LINKEDIN_PARENT_PREFIX + member_id,
                                     MergeReason.HUMAN.value, f"candidate_linkedins:{seq}", now)


def yes(conn: sqlite3.Connection, card: Card, key: str) -> str:
    """Use the pending profile the page posted. Returns what was confirmed: the URL or the synthetic key."""
    chosen: Pending | None = None
    for pending in card.pending:
        if pending.key == key:
            chosen = pending
    # The page draws "Use this profile" on a family with nothing pending too; there is nothing to confirm.
    if chosen is None:
        raise DecisionError("There is no LinkedIn to confirm here. Paste the right profile, or Skip.")
    now: str = now_iso()
    with conn:
        if chosen.origin == Origin.SYNTHETIC:
            for member in card.members:
                seq = queries_review.insert_linkedin(conn, member.candidate_id, chosen.linkedin_url, chosen.member_id,
                                               Origin.SYNTHETIC.value, Verdict.CONFIRMED.value, chosen.fingerprint, now)
                queries_review.insert_parent(conn, member.candidate_id, card.parent_id, MergeReason.HUMAN.value,
                                             f"candidate_linkedins:{seq}", now)
        else:
            _confirm(conn, card, chosen.linkedin_url, chosen.member_id, chosen.origin, chosen.fingerprint, now)
    return chosen.linkedin_url


def retarget(conn: sqlite3.Connection, data_root: Path, card: Card, pasted: str) -> str:
    """The pasted profile is assumed right. Returns the confirmed URL."""
    url: str = normalize_linkedin_url(pasted)
    # A text that names no /in/ profile would send an empty identifier to the paid fetch.
    if not extract_public_identifier(url):
        raise DecisionError("That is not a LinkedIn profile URL. Nothing was saved.")
    profiles: Profiles = load_profiles(data_root, [url], fetch=True)
    profile: Profile | None = profiles.found.get(url)
    if profile is None:
        raise DecisionError("Couldn't fetch that LinkedIn profile. Nothing was saved.")
    with conn:
        _confirm(conn, card, url, profile.member_id, Origin.HUMAN_OVERRIDE.value, profile.fetched_at, now_iso())
    return url


def skip(conn: sqlite3.Connection, card: Card) -> None:
    """This family is wrong: every pending URL or card is wrong_person for every member, and worth is no.
    A family with nothing pending has no URL to reject; it gets the worth rows alone."""
    now: str = now_iso()
    with conn:
        for member in card.members:
            for pending in card.pending:
                queries_review.insert_linkedin(conn, member.candidate_id, pending.linkedin_url, pending.member_id,
                                               pending.origin, Verdict.WRONG_PERSON.value, pending.fingerprint, now)
            queries_review.insert_parent(conn, member.candidate_id, card.parent_id, MergeReason.HUMAN.value, "", now)
            queries_review.insert_worth(conn, member.candidate_id, "no", SKIP_REASON, now)
