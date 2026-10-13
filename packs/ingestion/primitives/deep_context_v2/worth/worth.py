"""Block 06 Worth: does each family belong in the network? Yes, maybe or no, with a reason, the share labels
and the share call.

A family is the candidates that share a current parent; a candidate with no parent yet is a family of
one, keyed by its own id. Only candidates with facts are judged: without facts there is no evidence.
In order:

1. Pre-matched connection is yes in code: a family whose source names all match exactly one LinkedIn
   connection is already in the network. Its labels and share call still come from Luna.
2. Notable position is yes in code: the same yes, with that connection's position as the reason when
   it names a chief officer, founder, president, chair, partner or managing director.
3. Every family gets Luna's two requests (luna.py): the labels and worth over its members' facts
   (collapsed into one) and message counts, then the share call over those labels. Worth is yes,
   maybe or no with a reason, for every family not settled by rules 1 and 2.
4. A family is judged once per question version, as soon as any member is (evidence.version_key):
   new messages and merges never ask again, and a version bump asks everyone once. People's explicit
   refresh selects its families again, reusing the exact-request cache. A family Luna refuses to
   answer gets no row and stays pending.
5. One worth row per member candidate, the same verdict and labels to each.

Created: 2026-10-06
Changelog:
  2026-10-13: Luna Decisions replace JEV and its frozen worth model; request 2, the share call. A
    family is judged once any member is under the current version: a merge no longer asks again.
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
from packs.ingestion.primitives.deep_context_v2.db.queries import display_names
from packs.ingestion.primitives.deep_context_v2.db.owner import OwnerProfile, read_owner
from packs.ingestion.primitives.deep_context_v2.db.queries_worth import BundleCounts, ChannelCount, Connection, MemberFacts, WorthRow
from packs.ingestion.primitives.deep_context_v2.db import schema
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso, open_store, store_path
from packs.ingestion.primitives.deep_context_v2.decisions import (
    INPUT_PRICE_PER_MILLION, Answer, cache_path, decide_all, input_tokens, request_digest,
)
from packs.ingestion.primitives.deep_context_v2.node import Node
from packs.ingestion.primitives.deep_context_v2.worth import evidence, luna
from packs.ingestion.primitives.deep_context_v2.worth.luna import ChannelSummary, Labels
from packs.ingestion.primitives.deep_context_v2.worth.pre_match import pre_match
from packs.ingestion.primitives.deep_context_v2.worth.reason import reason

DECISIONS_CACHE_DIR = Path("deep-context") / "decisions"  # both requests' answers, by request digest
# What OpenAI bills beyond a request's own text, measured on 510 families (2026-10-13): 103-108
# tokens per question of request 1, and 33 on request 2, which before request 1 is answered is
# priced at its measured size (617 tokens, median).
LABELS_TOKENS_PER_QUESTION = 107
SHARE_EXTRA_TOKENS = 33
SHARE_REQUEST_TOKENS = 650
LABELS_PHASE = "Reading signals"
SHARE_PHASE = "Deciding who to share"
PRE_MATCHED_REASON = "Already in the network: the name matches one LinkedIn connection."
NOTABLE_REASON_PREFIX = "Notable position: "
DEFAULT_LIMIT = 100_000  # more families than any store has; --limit N judges the first N pending


@dataclass(frozen=True)
class Family:
    key: str
    members: tuple[str, ...]
    fingerprint: str
    judged: bool                     # a member's latest machine worth row carries the version key
    match: Connection | None         # None = the pre-match did not tie the family to exactly one connection
    request: dict[str, Any]          # request 1: every family is labelled, pre-matched or not
    digest: str                      # its cache key


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

    def families(self) -> list[Family]:
        """Every family with its evidence fingerprint, its pre-match, and its request 1."""
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
            summary: ChannelSummary = evidence.channel_summary(members, sources, counts, groups)
            fingerprint: str = evidence.version_key()
            # Judged once any member was, under these questions: a merge, or a new identity joining a
            # judged person, asks nothing.
            is_judged: bool = False
            for member in members:
                if judged.get(member.candidate_id) == fingerprint:
                    is_judged = True
            # Every family gets Luna's labels, a pre-matched one included: its worth is yes by the match, its
            # labels and share call come from the same requests as everyone else's.
            request: dict[str, Any] = luna.build_request(evidence.family_facts(members), summary, owner,
                                                         evidence.reference_date(members))
            digest: str = request_digest(luna.REQUEST_VERSION, request)
            member_ids: list[str] = []
            for member in members:
                member_ids.append(member.candidate_id)
            families.append(Family(key, tuple(member_ids), fingerprint, is_judged, match, request, digest))
        return families

    def pending(self, families: list[Family]) -> list[Family]:
        """The first `limit` families no member of which has a machine worth row under this version, and
        every family People asked for again (`parent_ids`), judged or not. New facts never make a family
        pending. Asking for a few people still brings everyone else up to the current version, so the
        share step after it never meets labels from older questions."""
        asked: frozenset[str] = frozenset(self.parent_ids)
        todo: list[Family] = []
        for family in families:
            if not family.judged or family.key in asked:
                todo.append(family)
        return todo[: self.limit]

    def estimate(self) -> dict[str, int | float | str]:
        """The dry run: count families, the free yeses and Luna's requests, and price the requests not
        yet cached as OpenAI bills them. Request 2 is known only once request 1 is answered; before
        that it is priced at its measured size. No call."""
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
        cached: int = 0
        tokens: int = 0
        for family in pending:
            if family.match is not None:
                pre_matched += 1
                if luna.NOTABLE_POSITION_RE.search(family.match.position):
                    notable += 1
            first = cache_path(self.cache_dir, family.digest)
            if not first.exists():
                tokens += (input_tokens(family.request) + LABELS_TOKENS_PER_QUESTION * len(family.request["questions"])
                           + SHARE_REQUEST_TOKENS)
                continue
            cached += 1
            answers: list[Answer] = json.loads(first.read_text(encoding="utf-8"))["answers"]
            if luna.refused(answers):
                continue
            share: dict[str, Any] = luna.share_request(luna.labels(answers))
            if cache_path(self.cache_dir, request_digest(luna.REQUEST_VERSION, share)).exists():
                cached += 1
            else:
                tokens += input_tokens(share) + SHARE_EXTRA_TOKENS
        return {
            "families": len(families), "members": members, "fresh": fresh,
            "pending": len(pending), "pre_matched_yes": pre_matched, "notable_positions": notable,
            "requests": 2 * len(pending), "cached": cached, "input_tokens": tokens,
            "estimated_cost_usd": round(tokens * INPUT_PRICE_PER_MILLION / 1_000_000, 4),
            "request_version": luna.REQUEST_VERSION,
        }

    def execute(self, *, cache_only: bool = False) -> dict[str, int]:
        """Rules 1 to 5: Luna's two requests for every pending family; a pre-matched family is yes by its
        match, the rest by Luna's worth answer; one worth row per member."""
        todo: list[Family] = self.pending(self.families())
        first: dict[str, dict[str, Any]] = {}  # digest -> request 1
        for family in todo:
            first[family.digest] = family.request
        answers = asyncio.run(decide_all(first, self.cache_dir, cache_only=cache_only, stage="worth_labels",
                                         on_progress=lambda done, total: self.progress(LABELS_PHASE, done, total)))
        labelled: dict[str, Labels] = {}  # family key -> request 1's labels
        second: dict[str, dict[str, Any]] = {}  # digest -> request 2
        share_digest: dict[str, str] = {}  # family key -> its request 2's digest
        for family in todo:
            if luna.refused(answers[family.digest]):
                continue
            labelled[family.key] = luna.labels(answers[family.digest])
            request: dict[str, Any] = luna.share_request(labelled[family.key])
            share_digest[family.key] = request_digest(luna.REQUEST_VERSION, request)
            second[share_digest[family.key]] = request
        calls = asyncio.run(decide_all(second, self.cache_dir, cache_only=cache_only, stage="share_call",
                                       on_progress=lambda done, total: self.progress(SHARE_PHASE, done, total)))
        verdicts: dict[str, Verdict] = {}  # family key -> its verdict
        pre_matched: int = 0
        for family in todo:
            if family.key not in labelled or luna.refused(calls[share_digest[family.key]]):
                continue
            labels: Labels = {**labelled[family.key], **luna.labels(calls[share_digest[family.key]]),
                              "version": luna.REQUEST_VERSION}
            labels_json: str = json.dumps(labels, sort_keys=True)
            if family.match is not None:
                # Rules 1 and 2: already in the network, so yes; the labels are Luna's all the same.
                pre_matched += 1
                text: str = PRE_MATCHED_REASON
                if luna.NOTABLE_POSITION_RE.search(family.match.position):
                    text = NOTABLE_REASON_PREFIX + family.match.position.strip()
                verdicts[family.key] = Verdict(schema.Worth.YES.value, text, labels_json)
            else:
                decision: str = str(labels["worth"])
                verdicts[family.key] = Verdict(decision, reason(labels, decision), labels_json)
        # Rule 5: one row per member, the same verdict to each.
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
                "requests": len(first) + len(second), "worth_rows": len(rows)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="06 Worth: yes, maybe or no per family (Luna Decisions).")
    parser.add_argument("--data-root", type=Path, default=Path(".powerpacks"))
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="judge only the first N pending families")
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
