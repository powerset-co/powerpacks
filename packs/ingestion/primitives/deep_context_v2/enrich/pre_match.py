"""Step 1, pre-match: the LinkedIn connections whose name matches every written name of a worth-yes family.

Free and derived on every run; nothing is stored for a proposal. Exactly one match that also shares an
email with a member is confirmed without the judge (once its profile gives the member id); every other
match is a proposed URL, origin linkedin_network, for the judge. Two connections with the surname are two
proposals and the judge says yes to at most one.

Created: 2026-10-07
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from packs.ingestion.primitives.deep_context_v2.db import queries_enrich, queries_worth
from packs.ingestion.primitives.deep_context_v2.db.queries_worth import Connection
from packs.ingestion.primitives.deep_context_v2.db.schema import Worth
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.enrich.family import Family, load_families
from packs.ingestion.primitives.deep_context_v2.worth.pre_match import pre_match
from packs.ingestion.schemas.people_schema import normalize_linkedin_url


@dataclass(frozen=True)
class PreMatch:
    matched: dict[str, list[str]]  # parent_id -> the normalized URLs of every matching connection
    by_email: dict[str, str]       # parent_id -> its one match, when that connection shares a member's email


def enters(family: Family) -> bool:
    """A worth-yes family on a p: id with no human LinkedIn answer: one that may still need a LinkedIn found.
    After review only review touches a family."""
    return family.minted and family.worth.worth == Worth.YES and not family.human_linkedin


def pre_match_families(families: list[Family], connections: list[Connection], emails: dict[str, str]) -> PreMatch:
    """Every entering family's matching connections, and the one-match-sharing-an-email confirmations."""
    matched: dict[str, list[str]] = {}
    by_email: dict[str, str] = {}
    for family in families:
        if not enters(family):
            continue
        # Every connection whose name matches every written name of the family.
        matches: list[Connection] = pre_match(list(family.names), connections)
        if not matches:
            continue
        urls: list[str] = []
        for connection in matches:
            urls.append(normalize_linkedin_url(connection.linkedin_url))
        matched[family.parent_id] = urls
        # Exactly one match whose export email is one of the members' emails: confirmed like a slam dunk.
        if len(matches) == 1 and emails.get(matches[0].linkedin_url, "") in family.emails():
            by_email[family.parent_id] = urls[0]
    return PreMatch(matched, by_email)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="07 Enrich step 1, the pre-match against the LinkedIn connections: "
                                                 "counts only. Nothing is stored for a proposal.")
    parser.add_argument("--data-root", type=Path, default=Path(".powerpacks"))
    args = parser.parse_args(argv)
    conn = open_store(store_path(args.data_root))
    families: list[Family] = load_families(conn)
    result: PreMatch = pre_match_families(families, queries_worth.all_connections(conn), queries_enrich.connection_emails(conn))
    eligible: int = 0
    for family in families:
        if enters(family):
            eligible += 1
    one: int = 0
    several: int = 0
    proposed: int = 0
    for urls in result.matched.values():
        if len(urls) == 1:
            one += 1
        else:
            several += 1
    for parent_id, urls in result.matched.items():
        if parent_id not in result.by_email:
            proposed += len(urls)
    print(json.dumps({"families_worth_yes_on_p": eligible, "matched_families": len(result.matched),
                      "exactly_one_match": one, "several_matches": several, "email_confirmed": len(result.by_email),
                      "proposed_urls": proposed}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
