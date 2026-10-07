"""Block 07 Enrich: find and judge the LinkedIn of every family worth keeping.

In order:

1. Pre-match: the LinkedIn connections whose name matches every written name of a worth-yes family on
   a p: id. Exactly one match sharing a member's email is confirmed without the judge; every other match
   is a proposed URL (pre_match.py). Nothing is stored for a proposal.
2. Research: Parallel, for the worth-yes p: families with no pre-match, no research row at their evidence
   handle and no human LinkedIn verdict (research.py). The dry run prices it first.
3. Profiles: the profile behind every URL, cache first, RapidAPI once per miss (profiles.py). A URL whose
   fetch failed is not judged this run.
4. Confirm the email pre-matches, then judge: JEV on every undecided profile, Sol on the families JEV
   did not settle on exactly one member id (judge.py). A confirmed LinkedIn moves every member onto
   li:<member id>; a member already wrong on it splits onto a fresh p: id.
5. Settle: a family with no real profile and under 25 messages is worth no (settle.py).

Created: 2026-10-07
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from pathlib import Path

from packs.ingestion.primitives.deep_context_v2.db import queries_enrich, queries_worth
from packs.ingestion.primitives.deep_context_v2.db.owner import owner_background_block, read_owner
from packs.ingestion.primitives.deep_context_v2.db.queries_dedupe import ParentRow, append_parent_rows
from packs.ingestion.primitives.deep_context_v2.db.queries_worth import WorthRow
from packs.ingestion.primitives.deep_context_v2.db.schema import Origin
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso, open_store, store_path
from packs.ingestion.primitives.deep_context_v2.enrich import judge, proposals, research, settle
from packs.ingestion.primitives.deep_context_v2.enrich.judge import JudgePlan, JudgeTask
from packs.ingestion.primitives.deep_context_v2.enrich.research import ResearchSubject
from packs.ingestion.primitives.deep_context_v2.enrich.pre_match import PreMatch, pre_match_families
from packs.ingestion.primitives.deep_context_v2.enrich.family import Family, confirm, judgment_fingerprint, load_families
from packs.ingestion.primitives.deep_context_v2.enrich.profiles import Profile, Profiles, load_profiles
from packs.ingestion.primitives.deep_context_v2.enrich.proposals import Proposals
from packs.ingestion.primitives.deep_context_v2.node import Node

PRE_MATCH_VERSION = "pre-match-email-2026-10-07"  # the fingerprint of an email-confirmed pre-match


class Enrich(Node):
    name = "enrich"
    reads = ("current_parent", "current_worth", "current_linkedins", "candidates", "candidate_identifiers", "facts",
             "bundles", "connections", "candidate_linkedins", "research", "owner")
    writes = ("candidate_linkedins", "research", "candidate_parent", "worth")

    def __init__(self, conn: sqlite3.Connection, data_root: Path, *, limit: int | None) -> None:
        super().__init__(conn, data_root)
        self.limit = limit
        self.jev_cache = data_root / judge.JEV_CACHE_RELATIVE_DIR

    def execute(self) -> dict[str, int]:
        """The five steps in the page's order; every count comes from what each step built."""
        counts: dict[str, int] = {}
        # Steps 1 and 2: the pre-match, then research for the families it does not name.
        found: Proposals = proposals.derive(self.conn)
        matches: PreMatch = pre_match_families(found.families, queries_worth.all_connections(self.conn),
                                               queries_enrich.connection_emails(self.conn))
        todo: list[ResearchSubject] = research.subjects(found.families, matches, found.research)
        if todo:
            for key, value in research.submit(self.conn, todo).items():
                counts[key] = value
            found = proposals.derive(self.conn)
        # Step 3: a profile for every URL a judge or a confirmation will read.
        profiles: Profiles = load_profiles(self.data_root, proposals.all_urls(found), fetch=True)
        counts["profiles_found"] = len(profiles.found)
        counts["profiles_missing"] = profiles.missing
        counts["profiles_fetched"] = profiles.fetched
        # Step 4a: an email-confirmed pre-match is confirmed without the judge, once its profile names the member.
        now: str = now_iso()
        parents: list[ParentRow] = []
        counts["email_confirmed"] = 0
        counts["email_confirm_no_profile"] = 0
        by_parent: dict[str, Family] = {}
        for family in found.families:
            by_parent[family.parent_id] = family
        for parent_id, url in sorted(found.by_email.items()):
            profile: Profile | None = profiles.found.get(url)
            if profile is None:
                counts["email_confirm_no_profile"] += 1
                continue
            family: Family = by_parent[parent_id]
            fingerprint: str = judgment_fingerprint(family, [(url, Origin.LINKEDIN_NETWORK.value, profile.fetched_at)],
                                                    PRE_MATCH_VERSION)
            parents.extend(confirm(self.conn, family, url, profile.member_id, Origin.LINKEDIN_NETWORK.value,
                                   fingerprint, now))
            counts["email_confirmed"] += 1
        append_parent_rows(self.conn, parents)
        if parents:
            self.conn.commit()
            found = proposals.derive(self.conn)  # the confirmed families are on li: now and do not enter the judge
        # Step 4b: the judge, on every family with an undecided URL; each family's rows are written as it is decided.
        planned: JudgePlan = judge.plan(found, profiles.found, queries_enrich.machine_judgments(self.conn))
        tasks: list[JudgeTask] = planned.tasks
        if self.limit is not None:
            tasks = tasks[: self.limit]
        counts["judge_entering"] = planned.entering
        counts["judge_no_profile"] = planned.no_profile
        counts["judge_already_judged"] = planned.already_judged
        counts["judge_families"] = len(tasks)
        owner_block: str = owner_background_block(read_owner(self.conn))
        for key, value in asyncio.run(judge.decide(self.conn, tasks, self.jev_cache, owner_block, now)).items():
            counts[key] = value
        # Step 5: settle, on the families as the judge left them.
        families: list[Family] = load_families(self.conn)
        confirmed: Profiles = load_profiles(self.data_root, settle.confirmed_urls(families), fetch=False)
        rows: list[WorthRow] = settle.settle_rows(families, confirmed, now)
        queries_worth.append_worth(self.conn, rows)
        counts["settle_worth_rows"] = len(rows)
        return counts

    def estimate(self) -> dict[str, object]:
        """The dry run of the whole block: every step's counts and the judge's price. No call, no write."""
        found: Proposals = proposals.derive(self.conn)
        matches = pre_match_families(found.families, queries_worth.all_connections(self.conn),
                                              queries_enrich.connection_emails(self.conn))
        todo: list[ResearchSubject] = research.subjects(found.families, matches, found.research)
        profiles: Profiles = load_profiles(self.data_root, proposals.all_urls(found), fetch=False)
        planned: JudgePlan = judge.plan(found, profiles.found, queries_enrich.machine_judgments(self.conn))
        tasks: list[JudgeTask] = planned.tasks
        if self.limit is not None:
            tasks = tasks[: self.limit]
        result: dict[str, object] = {
            "families": len(found.families), "pre_matched_families": len(matches.matched),
            "email_confirmed": len(found.by_email), "research_families": len(todo),
            "research_cost_usd": round(len(todo) * research.PRICE_PER_RUN_USD, 2),
            "profiles_cached": len(profiles.found), "profiles_missing": profiles.missing,
            "judge_entering": planned.entering, "judge_no_profile": planned.no_profile,
            "judge_already_judged": planned.already_judged, "judge_families": len(tasks),
        }
        priced: dict[str, object] = judge.estimate(tasks, self.jev_cache, owner_background_block(read_owner(self.conn)))
        for key, value in priced.items():
            result[key] = value
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="07 Enrich: pre-match, research, profiles, judge, settle.")
    parser.add_argument("--data-root", type=Path, default=Path(".powerpacks"))
    parser.add_argument("--limit", type=int, default=None, help="judge only the first N families")
    parser.add_argument("--dry-run", action="store_true", help="count and price only; no call, no write, no manifest")
    args = parser.parse_args(argv)
    node = Enrich(open_store(store_path(args.data_root)), args.data_root, limit=args.limit)
    if args.dry_run:
        print(json.dumps(node.estimate(), indent=2))
        return 0
    manifest = node.run()
    print(manifest.status, manifest.counts, manifest.error or "")
    if manifest.status == "completed":
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
