"""Step 5, settle: a family with no real profile and under 25 messages is worth no.

A real profile is a confirmed LinkedIn whose profile has a name and positions or a location, or a
synthetic card a human accepted. Only a family whose current worth is a machine yes or maybe is
settled: a human worth row wins in the view, so nothing is written when a member has one, and a family
already at no needs no row. The row carries the family's worth key, so worth does not judge it again.

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


def settle_rows(families: list[Family], profiles: Profiles, now: str) -> list[WorthRow]:
    """One worth-no row per member for every family with no real profile and under 25 messages, keyed so worth does not judge it again."""
    rows: list[WorthRow] = []
    for family in families:
        # Only a machine yes or maybe: a human row wins, and a no is already settled.
        if family.worth.decided_by == DecidedBy.HUMAN or family.worth.worth == Worth.NO:
            continue
        if family.messages >= MESSAGE_BAR or has_real_profile(family, profiles):
            continue
        key: str = evidence.family_key(list(family.members))
        for candidate_id in family.candidates:
            rows.append((candidate_id, Worth.NO.value, DecidedBy.MACHINE.value, REASON, "{}", key, now))
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
    settled: set[str] = set()
    for row in rows:
        settled.add(row[0])
    families_settled: int = 0
    for family in families:
        if family.candidates[0] in settled:
            families_settled += 1
    print(json.dumps({"families_to_settle": families_settled, "worth_rows": len(rows)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
