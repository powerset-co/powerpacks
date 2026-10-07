"""Block 06 Worth: does each family belong in the network? Yes, maybe or no, with a reason and the share labels.

A family is the candidates that share a current parent; a candidate with no parent yet is a family of
one, keyed by its own id. Only candidates with facts are judged: without facts there is no evidence.
In order:

1. Pre-matched connection is yes in code: a family whose source names all match exactly one LinkedIn
   connection is already in the network. No JEV call.
2. Notable position is yes in code: the same yes, with that connection's position as the reason when
   it names a chief officer, founder, president, chair, partner or managing director.
3. Every other family gets one JEV pass over its members' facts (collapsed into one) and message
   counts: yes, maybe or no, a reason, and the share labels.
4. A family is judged once: its key is the request version and its member ids. Only a merge, which
   makes a new family, brings a new judgment; a family whose members' latest machine worth rows all
   carry its key is skipped at $0.
5. One worth row per member candidate, the same verdict and labels to each.

Created: 2026-10-06
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context_v2.db import queries_worth as queries
from packs.ingestion.primitives.deep_context_v2.db.owner import OwnerProfile, read_owner
from packs.ingestion.primitives.deep_context_v2.db.queries_worth import Connection, MemberFacts, WorthRow
from packs.ingestion.primitives.deep_context_v2.db import schema
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso, open_store, store_path
from packs.ingestion.primitives.deep_context_v2.node import Node
from packs.ingestion.primitives.deep_context_v2.openai import load_env
from packs.ingestion.primitives.deep_context_v2.worth import evidence, jev
from packs.ingestion.primitives.deep_context_v2.worth.pre_match import pre_match
from packs.ingestion.primitives.deep_context_v2.worth.reason import reason

JEV_CACHE_DIR = Path("deep-context")  # answers are cached under <data root>/deep-context/jev/
PRE_MATCHED_REASON = "Already in the network: the name matches one LinkedIn connection."
NOTABLE_REASON_PREFIX = "Notable position: "
DEFAULT_LIMIT = 100_000  # more families than any store has; --limit N judges the first N pending


@dataclass(frozen=True)
class Family:
    key: str
    members: tuple[str, ...]
    fingerprint: str
    judged: bool                     # every member's latest machine worth row carries the fingerprint
    match: Connection | None         # None = the pre-match did not tie the family to exactly one connection
    request: dict[str, Any] | None   # the JEV request; None for a pre-matched family, which needs none


class Worth(Node):
    name = "worth"
    reads = ("current_parent", "candidates", "candidate_sources", "facts", "bundles", "connections", "worth", "owner")
    writes = ("worth",)

    def __init__(self, conn: sqlite3.Connection, data_root: Path, *, limit: int) -> None:
        super().__init__(conn, data_root)
        self.limit = limit
        self.cache_dir = data_root / JEV_CACHE_DIR

    def families(self) -> list[Family]:
        """Every family with its evidence fingerprint, its pre-match, and the request JEV would answer."""
        # Group the candidates with facts by family; read the rest of the evidence once.
        by_family: dict[str, list[MemberFacts]] = {}
        for member in queries.members_with_facts(self.conn):
            by_family.setdefault(member.family_key, []).append(member)
        names: dict[str, str] = queries.names_by_candidate(self.conn)
        sources: dict[str, list[str]] = queries.sources_by_candidate(self.conn)
        counts: dict[str, list[queries.ChannelCount]] = {}
        for count in queries.channel_counts(self.conn):
            counts.setdefault(count.candidate_id, []).append(count)
        groups: dict[str, int] = queries.group_counts(self.conn)
        connections: list[Connection] = queries.all_connections(self.conn)
        judged: dict[str, str] = queries.latest_machine_worth_fingerprints(self.conn)
        owner: OwnerProfile = read_owner(self.conn)

        families: list[Family] = []
        for key, members in by_family.items():
            # The pre-match: every source name of every member must match the one connection.
            matches: list[Connection] = pre_match(evidence.family_names(members, names), connections)
            match: Connection | None = None
            if len(matches) == 1:
                match = matches[0]
            # The message counts: the members' bundles added together, counts only.
            summary = evidence.channel_summary(members, sources, counts, groups)
            fingerprint: str = evidence.family_key(members)
            is_judged: bool = True
            for member in members:
                if judged.get(member.candidate_id) != fingerprint:
                    is_judged = False
            request: dict[str, Any] | None = None
            if match is None:
                request = jev.build_request(evidence.family_facts(members), summary, owner, evidence.reference_date(members))
            member_ids: list[str] = []
            for member in members:
                member_ids.append(member.candidate_id)
            families.append(Family(key, tuple(member_ids), fingerprint, is_judged, match, request))
        return families

    def pending(self, families: list[Family]) -> list[Family]:
        """The families not yet judged on their current evidence, the first `limit` of them."""
        todo: list[Family] = []
        for family in families:
            if not family.judged:
                todo.append(family)
        return todo[: self.limit]

    def estimate(self) -> dict[str, object]:
        """The dry run: count families, the free yeses and the JEV calls, and price the calls. No call."""
        families: list[Family] = self.families()
        pending: list[Family] = self.pending(families)
        fresh: int = 0
        members: int = 0
        for family in families:
            members += len(family.members)
            if family.judged:
                fresh += 1
        pre_matched: int = 0
        notable: int = 0
        calls: int = 0
        cached: int = 0
        tokens: int = 0
        for family in pending:
            if family.match is not None:
                pre_matched += 1
                if jev.NOTABLE_POSITION_RE.search(family.match.position):
                    notable += 1
                continue
            calls += 1
            if jev.is_cached(family.request, self.cache_dir):
                cached += 1
                continue
            tokens += jev.input_tokens(family.request)
        return {
            "families": len(families), "members": members, "fresh": fresh,
            "pending": len(pending), "pre_matched_yes": pre_matched, "notable_positions": notable,
            "jev_calls": calls, "jev_cached": cached, "jev_input_tokens": tokens,
            "estimated_cost_usd": round(jev.cost_usd(tokens), 4), "request_version": jev.REQUEST_VERSION,
        }

    def execute(self) -> dict[str, int]:
        todo: list[Family] = self.pending(self.families())
        # Rules 1 and 2: a pre-matched family is yes, no call.
        verdicts: dict[str, tuple[str, str, str]] = {}  # family key -> (worth, reason, labels_json)
        requests: dict[str, dict[str, Any]] = {}
        pre_matched: int = 0
        for family in todo:
            if family.match is None:
                requests[jev.request_digest(family.request)] = family.request
                continue
            pre_matched += 1
            text: str = PRE_MATCHED_REASON
            if jev.NOTABLE_POSITION_RE.search(family.match.position):
                text = NOTABLE_REASON_PREFIX + family.match.position.strip()
            verdicts[family.key] = (schema.Worth.YES.value, text, "{}")
        # Rule 3: one JEV pass per remaining family, read from the disk cache where it can be.
        load_env()
        answers = asyncio.run(jev.answer_all(requests, self.cache_dir))
        for family in todo:
            if family.match is None:
                answer = answers[jev.request_digest(family.request)]
                decision: str = jev.predict(answer)
                verdicts[family.key] = (decision, reason(answer, decision), json.dumps(jev.labels(answer), sort_keys=True))
        # Rule 5: one row per member, the same verdict to each.
        now: str = now_iso()
        rows: list[WorthRow] = []
        for family in todo:
            worth, text, labels_json = verdicts[family.key]
            for candidate_id in family.members:
                rows.append((candidate_id, worth, schema.DecidedBy.MACHINE.value, text, labels_json, family.fingerprint, now))
        queries.append_worth(self.conn, rows)
        return {"families": len(todo), "pre_matched_yes": pre_matched, "jev_requests": len(requests),
                "worth_rows": len(rows)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="06 Worth: yes, maybe or no per family (JEV).")
    parser.add_argument("--data-root", type=Path, default=Path(".powerpacks"))
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="judge only the first N pending families")
    parser.add_argument("--dry-run", action="store_true", help="count and price only; no JEV call, no manifest")
    args = parser.parse_args(argv)
    node = Worth(open_store(store_path(args.data_root)), args.data_root, limit=args.limit)
    if args.dry_run:
        print(json.dumps(node.estimate(), indent=2))
        return 0
    manifest = node.run()
    print(manifest.status, " ".join(f"{key}={value}" for key, value in manifest.counts.items()), manifest.error or "")
    return 0 if manifest.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
