"""Step 4, judge: every undecided URL of a family that enters, JEV first, then Sol on what JEV left pending.

Who enters: a worth yes or maybe family on a p: id with at least one undecided URL, and no human LinkedIn
verdict on any member. A URL is undecided for the family unless every member already holds a wrong-person
verdict on its member id; a URL with no profile is not judged this run. Two URLs with one member id are
one profile. A family whose every member already holds a verdict under this judgment's fingerprint was
judged on this evidence and is skipped at $0.

JEV judges each profile alone and never says wrong person. Exactly one JEV-confirmed member id confirms
the family and its other URLs are never judged. Otherwise Sol judges each remaining profile against the
family with the identity prompt (the owner's own connection carries a note that being connected is strong
evidence); a family may end with at most one confirmed profile, two confirms both go to the human queue.

Created: 2026-10-07
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import tiktoken

from packs.indexing.lib.openai_responses import estimate_cost_usd
from packs.search.primitives.llm_rerank_candidates.jev.client import INPUT_PRICE_PER_MILLION, cache_path, request_digest

from packs.ingestion.primitives.deep_context_v2.db import queries_enrich
from packs.ingestion.primitives.deep_context_v2.db.queries_dedupe import ParentRow, append_parent_rows
from packs.ingestion.primitives.deep_context_v2.db.queries_enrich import LinkedinRow, Research
from packs.ingestion.primitives.deep_context_v2.db.schema import DecidedBy, MergeReason, Origin, Verdict, Worth
from packs.ingestion.primitives.deep_context_v2.db.owner import owner_background_block, read_owner
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.dedupe.dedupe import mint
from packs.ingestion.primitives.deep_context_v2.enrich import jev_identity, proposals, research
from packs.ingestion.primitives.deep_context_v2.enrich import sol_identity
from packs.ingestion.primitives.deep_context_v2.enrich.sol_identity import SolVerdict
from packs.ingestion.primitives.deep_context_v2.enrich.family import Family, confirm, family_evidence, judgment_fingerprint
from packs.ingestion.primitives.deep_context_v2.enrich.profiles import Profile, Profiles, load_profiles
from packs.ingestion.primitives.deep_context_v2.enrich.proposals import Proposals
from packs.ingestion.primitives.deep_context_v2.openai import OpenAIResponsesCaller, OpenAIResponsesConfig, load_env

VERSION = "enrich-judge-2026-10-07-owner-domains"  # in every judgment fingerprint: a new judge is a new judgment
# What a JEV-alone confirmation writes: JEV's two views agreeing carry no Sol confidence or reason (confirmed rows
# never reach the review card).
JEV_CONFIRMED = SolVerdict(Verdict.CONFIRMED.value, None, "")
OUTPUT_TOKENS_PER_CALL = 1500  # assumed Sol output+reasoning tokens per profile, estimate only
JEV_CACHE_RELATIVE_DIR = Path("deep-context") / "identity"  # <data root>/deep-context/identity/<view>/jev/
# The owner's own connection: being connected on LinkedIn is itself strong evidence (decided 2026-10-07).


@dataclass(frozen=True)
class ProposedProfile:
    """One profile proposed for a family."""

    origin: str
    profile: Profile


@dataclass(frozen=True)
class JudgeTask:
    family: Family
    candidates: tuple[ProposedProfile, ...]  # one per member id, in proposal order
    fingerprint: str
    citations: tuple[dict[str, Any], ...]  # the research row's source URLs, titles and excerpts


@dataclass(frozen=True)
class JudgePlan:
    tasks: list[JudgeTask]
    entering: int        # families that enter the judge
    no_profile: int      # families whose every undecided URL lacks a profile this run
    already_judged: int  # families judged on this evidence before


def _citations(row: Research | None) -> tuple[dict[str, Any], ...]:
    """The research row's sources, once each: the judge's research evidence."""
    found: list[dict[str, Any]] = []
    if row is None or row.result_json is None:
        return tuple(found)
    for basis in json.loads(row.result_json).get("basis", []):
        for citation in basis.get("citations") or []:
            kept: dict[str, Any] = {}
            for key in ("url", "title", "excerpts"):
                if key in citation:
                    kept[key] = citation[key]
            if kept not in found:
                found.append(kept)
    return tuple(found)


def plan(proposals: Proposals, profiles: dict[str, Profile], judged: set[tuple[str, str]]) -> JudgePlan:
    """Who enters the judge and with which profiles: the tasks, plus the counts of families entering, without a profile, and already judged on this evidence."""
    tasks: list[JudgeTask] = []
    entering: int = 0
    no_profile: int = 0
    already: int = 0
    for family in proposals.families:
        if not family.minted or family.worth.worth == Worth.NO or family.human_linkedin:
            continue
        if family.parent_id not in proposals.proposed:
            continue
        # The member ids every member already holds a wrong-person verdict on: never proposed again.
        wrong_by: dict[str, set[str]] = {}
        for verdict in family.verdicts:
            if verdict.verdict == Verdict.WRONG_PERSON:
                wrong_by.setdefault(verdict.member_id, set()).add(verdict.candidate_id)
        candidates: list[ProposedProfile] = []
        seen: set[str] = set()
        unfetched: int = 0  # undecided URLs with no profile this run
        profiled: list[tuple[str, str, str]] = []  # every proposal with a profile: (url, origin, fetched at), the key
        for proposal in proposals.proposed[family.parent_id]:
            profile: Profile | None = profiles.get(proposal.linkedin_url)
            if profile is None:
                unfetched += 1
                continue
            profiled.append((profile.linkedin_url, proposal.origin, profile.fetched_at))
            if profile.member_id in seen or len(wrong_by.get(profile.member_id, set())) == len(family.candidates):
                continue
            seen.add(profile.member_id)
            candidates.append(ProposedProfile(proposal.origin, profile))
        if not candidates and not unfetched:
            continue  # every URL is decided
        entering += 1
        if not candidates:
            no_profile += 1
            continue
        fingerprint: str = judgment_fingerprint(family, profiled, VERSION)
        done: bool = True
        for candidate_id in family.candidates:
            if (candidate_id, fingerprint) not in judged:
                done = False
        if done:
            already += 1
            continue
        tasks.append(JudgeTask(family, tuple(candidates), fingerprint,
                          _citations(proposals.research.get(research.handle(family.facts)))))
    return JudgePlan(tasks, entering, no_profile, already)


def jev_pairs(task: JudgeTask) -> list[dict[str, dict[str, Any]]]:
    """The two view requests for each of the task's profiles."""
    network_urls: list[str] = []
    for candidate in task.candidates:
        if candidate.origin == Origin.LINKEDIN_NETWORK:
            network_urls.append(candidate.profile.linkedin_url)
    pairs: list[dict[str, dict[str, Any]]] = []
    for candidate in task.candidates:
        pairs.append(jev_identity.requests(family_evidence(task.family), candidate.profile.judge_view(),
                                           candidate.origin == Origin.LINKEDIN_NETWORK, network_urls))
    return pairs


async def sol(caller: OpenAIResponsesCaller, task: JudgeTask, owner_block: str) -> dict[str, SolVerdict]:
    """URL -> Sol's answer, one call per profile (sol_identity). A family may end with at most one confirmed
    profile: when two are confirmed, both go to the human queue instead."""
    verdicts: dict[str, SolVerdict] = {}
    for candidate in task.candidates:
        prompt: str = sol_identity.identity_prompt(task.family, candidate.profile, candidate.origin, task.citations, owner_block)
        verdicts[candidate.profile.linkedin_url] = await sol_identity.verdict(caller, prompt)
    confirmed: int = 0
    for answer in verdicts.values():
        if answer.verdict == Verdict.CONFIRMED:
            confirmed += 1
    if confirmed > 1:
        for url, answer in verdicts.items():
            if answer.verdict == Verdict.CONFIRMED:
                verdicts[url] = replace(answer, verdict=Verdict.NEEDS_REVIEW.value)
    return verdicts


def write(conn: sqlite3.Connection, task: JudgeTask, verdicts: dict[str, SolVerdict], now: str) -> dict[str, int]:
    """One verdict row per member per judged URL, carrying Sol's confidence and reason. A confirmed URL moves
    every member onto li:<member id>, except a member already holding a wrong-person verdict on it: that one
    is split onto a fresh p: id."""
    family: Family = task.family
    rows: list[LinkedinRow] = []
    parents: list[ParentRow] = []
    counts: dict[str, int] = {"confirmed": 0, "wrong_person": 0, "needs_review": 0, "split": 0}
    for candidate in task.candidates:
        url: str = candidate.profile.linkedin_url
        if url not in verdicts:
            continue  # not judged: another member id was confirmed by JEV alone
        answer: SolVerdict = verdicts[url]
        counts[answer.verdict] += 1
        if answer.verdict != Verdict.CONFIRMED:
            for candidate_id in family.candidates:
                rows.append((candidate_id, url, candidate.profile.member_id, candidate.origin, answer.verdict,
                             DecidedBy.MACHINE.value, task.fingerprint, answer.confidence, answer.reason, now))
            continue
        # The split: a member already judged someone else on this member id leaves the family, its parent
        # row naming that wrong-person verdict.
        wrong: dict[str, int] = {}  # candidate_id -> the seq of its wrong-person verdict
        for held in family.verdicts:
            if held.member_id == candidate.profile.member_id and held.verdict == Verdict.WRONG_PERSON:
                wrong[held.candidate_id] = held.seq
        staying: list[str] = []
        for candidate_id in family.candidates:
            if candidate_id in wrong:
                parents.append((candidate_id, mint(), MergeReason.JUDGE_WRONG_PERSON.value,
                                f"candidate_linkedins:{wrong[candidate_id]}", now))
                counts["split"] += 1
            else:
                staying.append(candidate_id)
        moved: Family = replace(family, candidates=tuple(staying))
        parents.extend(confirm(conn, moved, url, candidate.profile.member_id, candidate.origin, task.fingerprint,
                               answer.confidence, answer.reason, now))
    queries_enrich.append_linkedins(conn, rows)
    append_parent_rows(conn, parents)
    counts["parent_rows"] = len(parents)
    return counts


def estimate(tasks: list[JudgeTask], cache_dir: Path, owner_block: str) -> dict[str, object]:
    """The dry run: JEV requests and their price, and Sol at most once per family. No call."""
    requests: int = 0
    cached: int = 0
    jev_tokens: int = 0
    sol_tokens: int = 0
    sol_calls: int = 0
    encoder = tiktoken.get_encoding("o200k_base")
    for task in tasks:
        # JEV: two view requests per profile, each cached on disk under <cache_dir>/<view>/ by its digest.
        for pair in jev_pairs(task):
            for view, request in pair.items():
                requests += 1
                if cache_path(cache_dir / view, request_digest(request)).exists():
                    cached += 1
                    continue
                jev_tokens += len(encoder.encode(json.dumps(request, ensure_ascii=False, sort_keys=True)))
        # Sol: one call per profile.
        for candidate in task.candidates:
            prompt: str = sol_identity.identity_prompt(task.family, candidate.profile, candidate.origin, task.citations, owner_block)
            sol_tokens += len(encoder.encode(sol_identity.SYSTEM_PROMPT + prompt))
            sol_calls += 1
    output: int = sol_calls * OUTPUT_TOKENS_PER_CALL
    jev_usd: float = jev_tokens * INPUT_PRICE_PER_MILLION / 1_000_000
    sol_usd: float = estimate_cost_usd(sol_tokens, output, sol_identity.MODEL)
    return {"jev_requests": requests, "jev_cached": cached, "jev_input_tokens": jev_tokens,
            "jev_cost_usd": round(jev_usd, 4), "sol_calls_at_most": sol_calls, "sol_input_tokens": sol_tokens,
            "sol_output_tokens_assumed": output, "sol_cost_usd_at_most": round(sol_usd, 4),
            "estimated_cost_usd_at_most": round(jev_usd + sol_usd, 4)}


async def decide(conn: sqlite3.Connection, tasks: list[JudgeTask], cache_dir: Path, owner_block: str, now: str) -> dict[str, int]:
    """Judge every task and write each family's rows the moment its verdicts are known, committing as it
    goes, so a stopped run keeps what it paid for. JEV answers every profile first (its own disk cache);
    Sol sees the families JEV did not settle on exactly one member id. A failed Sol call is counted and
    the family is judged next run."""
    load_env()  # the JEV key, read from the environment on a cache miss
    pairs: list[dict[str, dict[str, Any]]] = []
    for task in tasks:
        pairs.extend(jev_pairs(task))
    jev: list[str] = await jev_identity.answer_all(pairs, cache_dir)
    counts: dict[str, int] = {"judge_failed": 0}
    for_sol: list[JudgeTask] = []
    position: int = 0
    for task in tasks:
        confirmed: list[str] = []
        for candidate in task.candidates:
            if jev[position] == Verdict.CONFIRMED:
                confirmed.append(candidate.profile.linkedin_url)
            position += 1
        if len(confirmed) == 1:
            _tally(counts, write(conn, task, {confirmed[0]: JEV_CONFIRMED}, now))
            conn.commit()
            continue
        for_sol.append(task)
    config = OpenAIResponsesConfig.resolve(model=sol_identity.MODEL, effort=sol_identity.REASONING_EFFORT, timeout=300, max_retries=2)
    async with OpenAIResponsesCaller(config) as caller:

        async def guarded(task: JudgeTask) -> tuple[JudgeTask, dict[str, SolVerdict] | None]:
            """The family's verdicts, or None when its Sol call failed (printed without the prompt)."""
            try:
                return task, await sol(caller, task, owner_block)
            except Exception as exc:
                print(f"failed one family: {type(exc).__name__}")
                return task, None

        calls: list[asyncio.Task[tuple[JudgeTask, dict[str, SolVerdict] | None]]] = []
        for task in for_sol:
            calls.append(asyncio.create_task(guarded(task)))
        for call in asyncio.as_completed(calls):
            task, verdicts = await call
            if verdicts is None:
                counts["judge_failed"] += 1
                continue
            _tally(counts, write(conn, task, verdicts, now))
            conn.commit()
    return counts


def _tally(counts: dict[str, int], written: dict[str, int]) -> None:
    for key, value in written.items():
        counts[key] = counts.get(key, 0) + value



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="07 Enrich step 4, the JEV and Sol LinkedIn judge: counts and price "
                                                 "only. The judge runs through enrich.")
    parser.add_argument("--data-root", type=Path, default=Path(".powerpacks"))
    parser.add_argument("--limit", type=int, default=None, help="price only the first N families")
    args = parser.parse_args(argv)
    conn = open_store(store_path(args.data_root))
    found: Proposals = proposals.derive(conn)
    profiles: Profiles = load_profiles(args.data_root, proposals.all_urls(found), fetch=False)
    planned: JudgePlan = plan(found, profiles.found, queries_enrich.machine_judgments(conn))
    tasks: list[JudgeTask] = planned.tasks
    if args.limit is not None:
        tasks = tasks[: args.limit]
    counts: dict[str, object] = {"families_entering": planned.entering, "no_profile": planned.no_profile,
                                 "already_judged": planned.already_judged, "families_to_judge": len(tasks)}
    profiles_judged: int = 0
    for task in tasks:
        profiles_judged += len(task.candidates)
    counts["profiles_to_judge"] = profiles_judged
    estimated: dict[str, object] = estimate(tasks, args.data_root / JEV_CACHE_RELATIVE_DIR,
                                            owner_background_block(read_owner(conn)))
    for key, value in estimated.items():
        counts[key] = value
    print(json.dumps(counts, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
