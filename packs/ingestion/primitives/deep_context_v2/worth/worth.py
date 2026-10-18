"""Worth and labels for each family, with one Luna request per family.

A family contains the candidates with a current parent, or one unparented candidate.
Only families with facts are judged. An exact LinkedIn pre-match is worth yes in code;
Luna supplies everyone's labels and everyone else's worth. One worth row is written
per member. Every family is asked every run, or the selected ones on a People refresh with a
selection: an unchanged request is answered from the exact-request cache for free, changed
facts or a question version bump are a new request.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from typing import Any

from packs.ingestion.primitives.deep_context_v2.collect.bundle import CollectionBundle
from packs.ingestion.primitives.deep_context_v2.db import queries_worth as queries
from packs.ingestion.primitives.deep_context_v2.db.queries import display_names
from packs.ingestion.primitives.deep_context_v2.db.owner import OwnerProfile, read_owner
from packs.ingestion.primitives.deep_context_v2.db.queries_worth import BundleCounts, ChannelCount, Connection, MemberFacts, WorthRow
from packs.ingestion.primitives.deep_context_v2.db import schema
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso, open_store, store_path
from packs.ingestion.primitives.deep_context_v2.decisions import (
    INPUT_PRICE_PER_MILLION, decide_all, input_tokens, request_digest, saved_answers,
)
from packs.ingestion.primitives.deep_context_v2.node import Node
from packs.ingestion.primitives.deep_context_v2.worth import evidence, luna
from packs.ingestion.primitives.deep_context_v2.worth.luna import ChannelSummary, Labels
from packs.ingestion.primitives.deep_context_v2.worth.pre_match import pre_match
from packs.ingestion.primitives.deep_context_v2.worth.reason import reason

DECISIONS_CACHE_DIR = Path("deep-context") / "decisions"  # answers by request digest
# Measured overhead per question beyond the request text (2026-10-13).
LABELS_TOKENS_PER_QUESTION = 107
LABELS_PHASE = "Reading signals"
PRE_MATCHED_REASON = "Already in the network: the name matches one LinkedIn connection."
NOTABLE_REASON_PREFIX = "Notable position: "
DEFAULT_LIMIT = 100_000  # more families than any store has; --limit N judges the first N
REQUEST_BATCH_SIZE = 64


@dataclass(frozen=True)
class Family:
    key: str
    members: tuple[str, ...]
    fingerprint: str
    match: Connection | None         # None = the pre-match did not tie the family to exactly one connection
    facts: tuple[MemberFacts, ...]
    channels: ChannelSummary


@dataclass(frozen=True)
class Verdict:
    """What worth decided for one family, written to every member."""

    worth: str
    reason: str
    labels_json: str


class Worth(Node):
    name = "worth"
    reads = ("current_parent", "candidates", "candidate_sources", "facts", "bundles", "connections", "worth", "owner")
    writes = ("worth",)

    def __init__(self, conn: sqlite3.Connection, data_root: Path, *, limit: int,
                 parent_ids: tuple[str, ...] = ()) -> None:
        super().__init__(conn, data_root)
        self.limit = limit
        self.parent_ids = parent_ids
        self.cache_dir = data_root / DECISIONS_CACHE_DIR
        self._families: list[Family] | None = None  # built once; estimate and execute both read it
        self.request_digests: dict[str, str] = {}

    def families(self) -> list[Family]:
        """Every family's facts, counts and pre-match, without raw message requests."""
        if self._families is None:
            self._families = self._build_families()
        return self._families

    def _build_families(self) -> list[Family]:
        # Group the candidates with facts by family; read the rest of the evidence once.
        by_family: dict[str, list[MemberFacts]] = {}
        for member in queries.members_with_facts(self.conn):
            by_family.setdefault(member.family_key, []).append(member)
        names: dict[str, str] = display_names(self.conn)
        sources: dict[str, list[str]] = queries.sources_by_candidate(self.conn)
        bundles: BundleCounts = queries.bundle_counts(self.conn)
        counts: dict[str, list[ChannelCount]] = {}
        for count in bundles.channels:
            counts.setdefault(count.candidate_id, []).append(count)
        groups: dict[str, int] = bundles.groups
        connections: list[Connection] = queries.all_connections(self.conn)
        self._owner: OwnerProfile = read_owner(self.conn)

        families: list[Family] = []
        for key, members in by_family.items():
            # The pre-match: every source name of every member must match the one connection.
            matches: list[Connection] = pre_match(evidence.family_names(members, names), connections)
            match: Connection | None = None
            if len(matches) == 1:
                match = matches[0]
            # The message counts: the members' bundles added together, counts only.
            summary: ChannelSummary = evidence.channel_summary(members, sources, counts, groups)
            fingerprint: str = evidence.version_key()
            member_ids: list[str] = []
            for member in members:
                member_ids.append(member.candidate_id)
            families.append(Family(key, tuple(member_ids), fingerprint, match, tuple(members), summary))
        return families

    def _request(self, family: Family) -> dict[str, Any]:
        """Build one family's exact request from its saved raw messages."""
        member_bundles: list[CollectionBundle] = []
        for candidate_id in family.members:
            row = self.conn.execute("SELECT payload_json FROM bundles WHERE candidate_id = ?", (candidate_id,)).fetchone()
            member_bundles.append(CollectionBundle.from_payload(json.loads(row["payload_json"])))
        members = list(family.facts)
        return luna.build_request(evidence.family_facts(members), member_bundles, family.channels, self._owner,
                                  evidence.reference_date(members))

    def pending(self, families: list[Family]) -> list[Family]:
        """The first `limit` families to ask: the selected ones (`parent_ids`), or every family."""
        asked: frozenset[str] = frozenset(self.parent_ids)
        todo: list[Family] = []
        for family in families:
            if not asked or family.key in asked:
                todo.append(family)
        return todo[: self.limit]

    def estimate(self) -> dict[str, int | float | str]:
        """Count families and price uncached Luna requests without making a call."""
        families: list[Family] = self.families()
        pending: list[Family] = self.pending(families)
        members: int = 0
        for family in families:
            members += len(family.members)
        pre_matched: int = 0
        notable: int = 0
        cached: int = 0
        tokens: int = 0
        for family in pending:
            request = self._request(family)
            digest = request_digest(luna.REQUEST_VERSION, request)
            self.request_digests[family.key] = digest
            if family.match is not None:
                pre_matched += 1
                if luna.NOTABLE_POSITION_RE.search(family.match.position):
                    notable += 1
            if saved_answers(self.cache_dir, digest, luna.refused) is not None:
                cached += 1
            else:
                tokens += input_tokens(request) + LABELS_TOKENS_PER_QUESTION * len(request["questions"])
            del request
        return {
            "families": len(families), "members": members,
            "pending": len(pending), "pre_matched_yes": pre_matched, "notable_positions": notable,
            "requests": len(pending), "cached": cached, "input_tokens": tokens,
            "estimated_cost_usd": round(tokens * INPUT_PRICE_PER_MILLION / 1_000_000, 4),
            "request_version": luna.REQUEST_VERSION,
        }

    def execute(self, *, cache_only: bool = False) -> dict[str, int]:
        """Ask Luna once per family to ask and write one worth row per member."""
        todo: list[Family] = self.pending(self.families())
        for family in todo:
            if family.key not in self.request_digests:
                request = self._request(family)
                self.request_digests[family.key] = request_digest(luna.REQUEST_VERSION, request)
                del request
        digests = {self.request_digests[family.key] for family in todo}
        missing = sum(saved_answers(self.cache_dir, digest, luna.refused) is None for digest in digests)
        if cache_only and missing:
            raise ValueError(f"{missing} answers are not saved yet. Check the cost again.")
        labels_by_digest: dict[str, Labels | None] = {}
        completed = 0
        for start in range(0, len(todo), REQUEST_BATCH_SIZE):
            requests: dict[str, dict[str, Any]] = {}
            for family in todo[start:start + REQUEST_BATCH_SIZE]:
                digest = self.request_digests[family.key]
                if digest not in labels_by_digest and digest not in requests:
                    requests[digest] = self._request(family)
            if not requests:
                continue
            batch_missing = sum(saved_answers(self.cache_dir, digest, luna.refused) is None for digest in requests)
            answers = asyncio.run(decide_all(requests, self.cache_dir, cache_only=cache_only, stage="worth_labels",
                                             on_progress=lambda done, total: self.progress(LABELS_PHASE, completed + done, missing),
                                             refused=luna.refused))
            for digest, answer in answers.items():
                labels_by_digest[digest] = None if luna.refused(answer) else {
                    **luna.labels(answer), "version": luna.REQUEST_VERSION}
            completed += batch_missing
            requests.clear()
            del answers
        verdicts: dict[str, Verdict] = {}  # family key -> its verdict
        pre_matched: int = 0
        for family in todo:
            labels = labels_by_digest[self.request_digests[family.key]]
            if labels is None:
                continue
            labels_json: str = json.dumps(labels, sort_keys=True)
            if family.match is not None:
                # Already in the network, so yes; the labels are Luna's all the same.
                pre_matched += 1
                text: str = PRE_MATCHED_REASON
                if luna.NOTABLE_POSITION_RE.search(family.match.position):
                    text = NOTABLE_REASON_PREFIX + family.match.position.strip()
                verdicts[family.key] = Verdict(schema.Worth.YES.value, text, labels_json)
            else:
                decision: str = str(labels["worth"])
                verdicts[family.key] = Verdict(decision, reason(labels, decision), labels_json)
        now: str = now_iso()
        rows: list[WorthRow] = []
        for family in todo:
            if family.key not in verdicts:
                continue
            verdict: Verdict = verdicts[family.key]
            for candidate_id in family.members:
                rows.append((candidate_id, verdict.worth, schema.DecidedBy.MACHINE.value, verdict.reason,
                             verdict.labels_json, family.fingerprint, now))
        queries.append_worth(self.conn, rows)
        return {"families": len(todo), "pre_matched_yes": pre_matched, "refused": len(todo) - len(verdicts),
                "requests": len(digests), "worth_rows": len(rows)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="06 Worth: yes, maybe or no per family (Luna Decisions).")
    parser.add_argument("--data-root", type=Path, default=Path(".powerpacks"))
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="judge only the first N families")
    parser.add_argument("--dry-run", action="store_true", help="count and price only; no call, no manifest")
    args = parser.parse_args(argv)
    node = Worth(open_store(store_path(args.data_root)), args.data_root, limit=args.limit)
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
