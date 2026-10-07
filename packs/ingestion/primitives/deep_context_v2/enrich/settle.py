"""Step 5, settle: an own LinkedIn connection is worth yes; any other family with no real profile and
under 25 messages is worth no. Both rules are v1's (`enrich/settle_policy.py: worth_decision`).

An own connection is a family one of the owner's first-degree connections was proposed for and not
judged wrong: the owner chose to connect with this person, so they are in until a human says no. A
real profile is a confirmed LinkedIn whose profile has a name and positions or a location, or a
synthetic card a human accepted. A human worth row wins in the view, so nothing is written when a
member has one, and a family already at the settled worth needs no row. The row carries the family's
worth key, so worth does not judge it again.

Changelog:
- 2026-10-07 (Arthur): the own-connection yes, dropped in the first port, restored.

Created: 2026-10-07
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from packs.ingestion.primitives.deep_context_v2.db.queries_worth import WorthRow
from packs.ingestion.primitives.deep_context_v2.db.schema import LINKEDIN_PARENT_PREFIX, DecidedBy, Origin, Verdict, Worth
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.enrich.family import Family, load_families
from packs.ingestion.primitives.deep_context_v2.enrich.profiles import Profiles, load_profiles
from packs.ingestion.primitives.deep_context_v2.worth import evidence

MESSAGE_BAR = 25
REASON = "settle: no profile, under 25 messages"
OWN_CONNECTION_REASON = "settle: own LinkedIn connection"


def confirmed_urls(families: list[Family]) -> list[str]:
    """The confirmed LinkedIn URL of every family on an li: id: the profiles settle reads."""
    urls: list[str] = []
    for family in families:
        for verdict in family.verdicts:
            if verdict.verdict == Verdict.CONFIRMED and family.parent_id == LINKEDIN_PARENT_PREFIX + verdict.member_id:
                urls.append(verdict.linkedin_url)
    return urls


def has_real_profile(family: Family, profiles: Profiles) -> bool:
    """A confirmed LinkedIn whose profile has a name and positions or a location, or a human-accepted synthetic card."""
    for verdict in family.verdicts:
        if verdict.verdict != Verdict.CONFIRMED:
            continue
        # A synthetic card a human accepted is the family's profile.
        if verdict.origin == Origin.SYNTHETIC:
            return True
        if family.parent_id == LINKEDIN_PARENT_PREFIX + verdict.member_id:
            profile = profiles.found.get(verdict.linkedin_url)
            if profile is not None and profile.real:
                return True
    return False


def own_connection(family: Family) -> bool:
    """One of the owner's first-degree connections was proposed for this family and not judged wrong."""
    for verdict in family.verdicts:
        if verdict.origin == Origin.LINKEDIN_NETWORK and verdict.verdict != Verdict.WRONG_PERSON:
            return True
    return False


def settle_rows(families: list[Family], profiles: Profiles, now: str) -> list[WorthRow]:
    """One worth row per member for every family settle changes: yes for an own connection not already
    at yes, no for any other family with no real profile and under 25 messages not already at no. Keyed
    so worth does not judge it again."""
    rows: list[WorthRow] = []
    for family in families:
        if family.worth.decided_by == DecidedBy.HUMAN:
            continue  # a human row wins in the view
        if own_connection(family):
            if family.worth.worth == Worth.YES:
                continue
            settled, reason = Worth.YES.value, OWN_CONNECTION_REASON
        else:
            if family.worth.worth == Worth.NO or family.messages >= MESSAGE_BAR or has_real_profile(family, profiles):
                continue
            settled, reason = Worth.NO.value, REASON
        key: str = evidence.family_key(list(family.members))
        for candidate_id in family.candidates:
            rows.append((candidate_id, settled, DecidedBy.MACHINE.value, reason, "{}", key, now))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="07 Enrich step 5, settle thin families to worth no: counts only. "
                                                 "Settle writes through enrich.")
    parser.add_argument("--data-root", type=Path, default=Path(".powerpacks"))
    args = parser.parse_args(argv)
    conn = open_store(store_path(args.data_root))
    families: list[Family] = load_families(conn)
    profiles: Profiles = load_profiles(args.data_root, confirmed_urls(families), fetch=False)
    rows: list[WorthRow] = settle_rows(families, profiles, "")
    settled: dict[str, str] = {}
    for row in rows:
        settled[row[0]] = row[1]
    counts: dict[str, int] = {"families_to_yes": 0, "families_to_no": 0, "worth_rows": len(rows)}
    for family in families:
        worth: str | None = settled.get(family.candidates[0])
        if worth is not None:
            counts["families_to_" + worth] += 1
    print(json.dumps(counts, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
