"""The URLs proposed for each family: derived every run from the pre-match and the research rows, never stored.

Created: 2026-10-07
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from packs.ingestion.primitives.deep_context_v2.db import queries_enrich, queries_worth
from packs.ingestion.primitives.deep_context_v2.db.queries_enrich import Research
from packs.ingestion.primitives.deep_context_v2.db.schema import Origin, ResearchStatus
from packs.ingestion.primitives.deep_context_v2.enrich import research
from packs.ingestion.primitives.deep_context_v2.enrich.family import Family, load_families
from packs.ingestion.primitives.deep_context_v2.enrich.pre_match import PreMatch, pre_match_families


@dataclass(frozen=True)
class Proposal:
    linkedin_url: str  # normalized
    origin: str


@dataclass(frozen=True)
class Proposals:
    families: list[Family]
    by_email: dict[str, str]                # parent_id -> the pre-matched URL confirmed by a shared email
    proposed: dict[str, list[Proposal]]     # parent_id -> every URL for the judge, pre-match first
    research: dict[str, Research]           # handle -> research row, for the judge's citations and settle


def derive(conn: sqlite3.Connection) -> Proposals:
    families: list[Family] = load_families(conn)
    matches: PreMatch = pre_match_families(families, queries_worth.all_connections(conn),
                                           queries_enrich.connection_emails(conn))
    rows: dict[str, Research] = queries_enrich.research_by_handle(conn)
    proposed: dict[str, list[Proposal]] = {}
    for family in families:
        urls: list[Proposal] = []
        # The pre-matched connections, unless one is already confirmed by a shared email.
        if family.parent_id not in matches.by_email:
            for url in matches.matched.get(family.parent_id, []):
                urls.append(Proposal(url, Origin.LINKEDIN_NETWORK.value))
        # A complete research row at the family's current handle proposes its URL.
        row: Research | None = rows.get(research.handle(family))
        if row is not None and row.status == ResearchStatus.COMPLETE:
            url: str = research.research_url(row)
            seen: bool = False
            for proposal in urls:
                if proposal.linkedin_url == url:
                    seen = True
            if not seen:
                urls.append(Proposal(url, Origin.RESEARCH.value))
        if urls:
            proposed[family.parent_id] = urls
    return Proposals(families, matches.by_email, proposed, rows)


def all_urls(proposals: Proposals) -> list[str]:
    """Every URL a profile is needed for: the email-confirmed ones and every proposal."""
    urls: list[str] = []
    for url in proposals.by_email.values():
        urls.append(url)
    for family_urls in proposals.proposed.values():
        for proposal in family_urls:
            urls.append(proposal.linkedin_url)
    return urls
