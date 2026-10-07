"""Block 05 Dedupe: group candidates with similar names, let Sol decide each pair, give every candidate a parent.

In order:

1. Block: every candidate with facts goes into name buckets and an email-handle bucket (blocking.py).
2. Gate: a blocked pair goes on only when the two names could be one person's
   (names.written_names_can_match). A recall gate for the judge, not the identity decision.
3. Skip pairs already in one family, and pairs with any saved verdict: a pair is judged once, ever.
4. Judge: one gpt-6.1-sol call per remaining pair: same, different or uncertain, saved in
   pair_verdicts as it arrives. A failed pair is counted and the run fails before any parent row.
5. Merge: every same verdict is an edge between the two families its candidates stand for (a
   candidate with no parent stands for itself). Connected components over families, so a family is in
   exactly one component and is never split. Each component joins into one id: the li: among them, else
   the lowest existing p:, else a fresh p:; only the candidates moving onto it get a row (an absorb). A
   different verdict between two families in a component blocks the join; two li: ids write nothing;
   uncertain writes nothing.
6. Singletons, last: every candidate still without a parent gets a fresh p: id of its own.

The evidence signature saved with each verdict (facts fingerprints, written names, identifiers,
prompt version) records what Sol saw. It is never used to judge a pair again.

Created: 2026-10-06
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import tiktoken

from packs.indexing.lib.openai_responses import estimate_cost_usd
from packs.ingestion.primitives.deep_context_v2.components import connected_components
from packs.ingestion.primitives.deep_context_v2.db import queries_dedupe as queries
from packs.ingestion.primitives.deep_context_v2.db.queries import display_names
from packs.ingestion.primitives.deep_context_v2.db.owner import read_owner
from packs.ingestion.primitives.deep_context_v2.db.queries_dedupe import Identifier, ParentRow
from packs.ingestion.primitives.deep_context_v2.db.schema import LINKEDIN_PARENT_PREFIX, MINTED_PARENT_HEX, MINTED_PARENT_PREFIX, MergeReason
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso, open_store, store_path
from packs.ingestion.primitives.deep_context_v2.dedupe import judge
from packs.ingestion.primitives.deep_context_v2.dedupe.judge import SolDecision, SolSide
from packs.ingestion.primitives.deep_context_v2.dedupe.blocking import Blocking, block
from packs.ingestion.primitives.deep_context_v2.names import names_for_matching, written_names_can_match
from packs.ingestion.primitives.deep_context_v2.node import Node
from packs.ingestion.primitives.deep_context_v2.openai import OpenAIResponsesCaller, OpenAIResponsesConfig

OUTPUT_TOKENS_PER_PAIR = 1500  # assumed output+reasoning tokens per Sol call, estimate only
DEFAULT_LIMIT = 100_000        # more pairs than any store has; --limit N judges the first N


@dataclass(frozen=True)
class Plan:
    blocking: Blocking
    gated: int
    already_joined: int
    already_judged: int
    todo: list[tuple[str, str, str]]          # (a, b, signature) for Sol


class Dedupe(Node):
    name = "dedupe"
    reads = ("candidates", "candidate_identifiers", "facts", "bundles", "current_parent", "pair_verdicts", "owner")
    writes = ("pair_verdicts", "candidate_parent")

    def __init__(self, conn: sqlite3.Connection, data_root: Path, *, limit: int) -> None:
        super().__init__(conn, data_root)
        self.limit = limit
        self.config = OpenAIResponsesConfig.resolve(model=judge.MODEL, effort=judge.REASONING_EFFORT, timeout=600, max_retries=3)
        self.identifiers: dict[str, list[Identifier]] = queries.candidate_identifiers(conn)
        self.fingerprints: dict[str, str] = queries.facts_fingerprints(conn)
        self.parents: dict[str, str] = queries.current_parents(conn)
        self.written: dict[str, str] = display_names(conn)
        # The name each candidate is matched on: its written name, or the dossier's when that only
        # extends a one-word written name.
        self.names: dict[str, str] = {}
        dossier: dict[str, str] = queries.dossier_names(conn)
        for candidate_id, written in self.written.items():
            self.names[candidate_id] = names_for_matching(written, dossier.get(candidate_id, ""))
        # Every saved verdict, by pair: a pair is judged once, ever. Its signature records what Sol saw
        # and is the reference any parent row carries.
        self.current: dict[tuple[str, str], int | None] = {}
        self.signatures: dict[tuple[str, str], str] = {}
        for saved in queries.saved_verdicts(conn):
            self.current[(saved.candidate_a, saved.candidate_b)] = saved.same_person
            self.signatures[(saved.candidate_a, saved.candidate_b)] = saved.signature

    def signature(self, a: str, b: str) -> str:
        """The pair's evidence: facts fingerprints, written names, identifiers, prompt version."""
        payload: dict[str, object] = {"prompt_version": judge.PROMPT_VERSION, "sides": []}
        for candidate_id in (a, b):
            identifiers: list[str] = []
            for identifier in self.identifiers[candidate_id]:
                identifiers.append(identifier.kind + ":" + identifier.normalized_value)
            payload["sides"].append({"facts": self.fingerprints[candidate_id], "name": self.written[candidate_id],
                                     "identifiers": sorted(identifiers)})
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def plan(self) -> Plan:
        # Rule 1: block every candidate with facts.
        """Rules 1 to 3: block, gate, skip the joined and the judged; the pairs for Sol with their signatures."""
        blocking: Blocking = block(sorted(self.fingerprints), self.names, self.identifiers)
        gated: int = 0
        joined: int = 0
        skipped: int = 0
        todo: list[tuple[str, str, str]] = []
        for a, b in blocking.pairs:
            # Rule 2: the two names must be able to be one person's.
            if not written_names_can_match(self.names[a], self.names[b]):
                continue
            gated += 1
            # Rule 3: already one family, or already judged.
            if a in self.parents and self.parents.get(b) == self.parents[a]:
                joined += 1
                continue
            if (a, b) in self.current:
                skipped += 1
                continue
            signature: str = self.signature(a, b)
            self.signatures[(a, b)] = signature
            todo.append((a, b, signature))
        return Plan(blocking, gated, joined, skipped, todo[: self.limit])

    def prompts(self, todo: list[tuple[str, str, str]]) -> list[str]:
        """The user prompt of each pair going to Sol, in order."""
        ids: set[str] = set()
        for a, b, _ in todo:
            ids.add(a)
            ids.add(b)
        facts: dict[str, str] = queries.facts_json(self.conn, sorted(ids))
        bundles: dict[str, str] = queries.bundle_payloads(self.conn, sorted(ids))
        owner_name: str = read_owner(self.conn).name
        rendered: list[str] = []
        for a, b, _ in todo:
            first: SolSide = judge.side_of(self.written[a], self.identifiers[a], facts[a], bundles[a])
            second: SolSide = judge.side_of(self.written[b], self.identifiers[b], facts[b], bundles[b])
            rendered.append(judge.user_prompt(owner_name, first, second))
        return rendered

    def estimate(self) -> dict[str, object]:
        """The dry run: the bucket distribution, the pair funnel, and the Sol price. No call, no write."""
        plan: Plan = self.plan()
        encoder = tiktoken.get_encoding("o200k_base")
        tokens: int = 0
        for prompt in self.prompts(plan.todo):
            tokens += len(encoder.encode(judge.SYSTEM_PROMPT + prompt))
        buckets: dict[str, dict[str, float]] = {}
        for kind, stats in plan.blocking.stats.items():
            buckets[kind] = {"buckets_of_two_or_more": stats.buckets, "mean_size": stats.mean_size,
                             "max_size": stats.max_size, "members_in_skipped_buckets": stats.members_in_skipped}
        output: int = len(plan.todo) * OUTPUT_TOKENS_PER_PAIR
        return {
            "candidates_bucketed": plan.blocking.bucketed, "buckets": buckets,
            "blocked_pairs": len(plan.blocking.pairs), "gated_pairs": plan.gated,
            "already_joined": plan.already_joined, "already_judged": plan.already_judged,
            "pairs_for_sol": len(plan.todo), "input_tokens": tokens, "output_tokens_assumed": output,
            "model": judge.MODEL, "reasoning_effort": self.config.effort, "prompt_version": judge.PROMPT_VERSION,
            "estimated_cost_usd": estimate_cost_usd(tokens, output, judge.MODEL),
        }

    def execute(self) -> dict[str, int]:
        """Rules 4 to 7: judge the planned pairs, join families over the same verdicts, then a parent for everyone else."""
        plan: Plan = self.plan()
        # Rule 4: judge; every answer is saved as it arrives.
        answers: dict[tuple[str, str], int | None] = asyncio.run(self._judge_all(plan.todo, self.prompts(plan.todo)))
        failed: int = len(plan.todo) - len(answers)
        if failed:
            raise RuntimeError(f"{failed} of {len(plan.todo)} pairs failed; {len(answers)} verdicts saved, rerun to redo the rest")
        # Every verdict that stands: the ones saved before this run and the ones just paid for.
        verdicts: dict[tuple[str, str], int | None] = {}
        for pair, same_person in self.current.items():
            verdicts[pair] = same_person
        for pair, same_person in answers.items():
            verdicts[pair] = same_person
        # Rule 5: join families over the same verdicts; rule 6: a parent of their own for everyone else.
        rows: list[ParentRow]
        counts: dict[str, int]
        rows, counts = self.merge(verdicts)
        placed: set[str] = set(self.parents)
        for row in rows:
            placed.add(row[0])
        now: str = now_iso()
        singletons: int = 0
        for candidate_id in queries.all_candidate_ids(self.conn):
            if candidate_id not in placed:
                rows.append((candidate_id, mint(), MergeReason.SINGLETON.value, None, now))
                singletons += 1
        queries.append_parent_rows(self.conn, rows)
        counts["gated_pairs"] = plan.gated
        counts["judged"] = len(answers)
        counts["singletons"] = singletons
        return counts

    def family_of(self, candidate_id: str) -> str:
        """The id a candidate stands for: its current parent, or itself while it has none."""
        if candidate_id in self.parents:
            return self.parents[candidate_id]
        return candidate_id

    def merge(self, verdicts: dict[tuple[str, str], int | None]) -> tuple[list[ParentRow], dict[str, int]]:
        """Join families, never candidates. Every same verdict is an edge between the two families its
        candidates stand for, so a family sits in exactly one component and is never split. A component
        joins into one id: the li: among them, else the lowest existing p:, else a fresh p:. Only the
        candidates not already on that id get a row, so a join is an absorb and a rerun writes nothing.
        A different verdict between two families in a component blocks the whole join; two li: ids
        in one component write nothing."""
        same: list[tuple[str, str]] = []
        different: list[tuple[str, str]] = []
        for pair, verdict in sorted(verdicts.items()):
            if verdict == 1:
                same.append(pair)
            elif verdict == 0:
                different.append(pair)
        # Every member of every existing family, by family id.
        members: dict[str, list[str]] = {}
        for candidate_id, parent_id in sorted(self.parents.items()):
            members.setdefault(parent_id, []).append(candidate_id)
        # The edges, between families. A same verdict inside one family is nothing new.
        edges: list[tuple[str, str]] = []
        for a, b in same:
            if self.family_of(a) != self.family_of(b):
                edges.append((self.family_of(a), self.family_of(b)))
        rows: list[ParentRow] = []
        counts: dict[str, int] = {"components_merged": 0, "components_with_different": 0, "components_two_linkedins": 0}
        now: str = now_iso()
        for component in connected_components(edges):
            inside: set[str] = set(component)
            # A different verdict between any two families here blocks the join.
            blocked: bool = False
            for a, b in different:
                if self.family_of(a) in inside and self.family_of(b) in inside and self.family_of(a) != self.family_of(b):
                    blocked = True
            if blocked:
                counts["components_with_different"] += 1
                continue
            # The destination: the one li: id, else the lowest existing p: id, else a fresh one.
            linkedin: list[str] = []
            existing: list[str] = []
            for family_id in component:
                if family_id.startswith(LINKEDIN_PARENT_PREFIX):
                    linkedin.append(family_id)
                elif family_id.startswith(MINTED_PARENT_PREFIX):
                    existing.append(family_id)
            if len(linkedin) > 1:
                counts["components_two_linkedins"] += 1
                continue
            destination: str
            if linkedin:
                destination = linkedin[0]
            elif existing:
                destination = existing[0]
            else:
                destination = mint()
            # The verdict behind the join: the first same pair whose families are both in the component.
            ref: str = ""
            for a, b in same:
                if self.family_of(a) in inside and self.family_of(b) in inside:
                    ref = f"pair_verdicts:{a}|{b}|{self.signatures[(a, b)]}"
                    break
            # One row per candidate moving onto the destination; the destination's own members write nothing.
            for family_id in component:
                if family_id == destination:
                    continue
                moving: list[str] = [family_id]
                if family_id in members:
                    moving = members[family_id]
                for candidate_id in moving:
                    rows.append((candidate_id, destination, MergeReason.SOL_SAME.value, ref, now))
            counts["components_merged"] += 1
        counts["sol_same_rows"] = len(rows)
        return rows, counts

    async def _judge_all(self, todo: list[tuple[str, str, str]], prompts: list[str]) -> dict[tuple[str, str], int | None]:
        """Every pair in flight at once (the client holds the concurrency limit); each answer is committed
        as it arrives, so a stopped run keeps what it paid for. A failed pair is printed and left out."""
        answers: dict[tuple[str, str], int | None] = {}
        async with OpenAIResponsesCaller(self.config) as caller:
            tasks: list[asyncio.Task[tuple[int, SolDecision | None]]] = []
            for index, prompt in enumerate(prompts):
                tasks.append(asyncio.create_task(self._guarded(caller, index, prompt)))
            for task in asyncio.as_completed(tasks):
                index, decision = await task
                if decision is None:
                    continue
                a, b, signature = todo[index]
                queries.insert_pair_verdict(self.conn, (a, b, signature, decision.same_person, decision.confidence,
                                                        decision.reason, now_iso()))
                self.conn.commit()
                answers[(a, b)] = decision.same_person
        return answers

    async def _guarded(self, caller: OpenAIResponsesCaller, index: int, prompt: str) -> tuple[int, SolDecision | None]:
        try:
            return index, await judge.judge(caller, prompt)
        except Exception as exc:
            print(f"failed one pair: {type(exc).__name__}")  # the message may quote the prompt; never printed
            return index, None


def mint() -> str:
    """A fresh p: parent id."""
    return MINTED_PARENT_PREFIX + secrets.token_hex(MINTED_PARENT_HEX // 2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="05 Dedupe: name pairs, the Sol judge, one parent per candidate.")
    parser.add_argument("--data-root", type=Path, default=Path(".powerpacks"))
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="judge only the first N pending pairs")
    parser.add_argument("--dry-run", action="store_true", help="count and price only; no Sol call, no write, no manifest")
    args = parser.parse_args(argv)
    node = Dedupe(open_store(store_path(args.data_root)), args.data_root, limit=args.limit)
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
