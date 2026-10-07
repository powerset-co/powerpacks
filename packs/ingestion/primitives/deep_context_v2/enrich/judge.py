"""Step 4, judge: every undecided URL of a family that enters, JEV first, then Sol on what JEV left pending.

Who enters: a worth yes or maybe family on a p: id with at least one undecided URL, and no human LinkedIn
verdict on any member. A URL is undecided for the family unless every member already holds a wrong-person
verdict on its member id; a URL with no profile is not judged this run. Two URLs with one member id are
one profile. A family whose every member already holds a verdict under this judgment's fingerprint was
judged on this evidence and is skipped at $0.

JEV judges each profile alone and never says wrong person. Exactly one JEV-confirmed member id confirms
the family and its other URLs are never judged. Otherwise Sol sees every URL at once and says yes to at
most one: yes is confirmed, no is wrong person, review is the human queue.

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

import jsonschema
import tiktoken

from packs.indexing.lib.openai_responses import estimate_cost_usd

from packs.ingestion.primitives.deep_context_v2.db import queries_enrich
from packs.ingestion.primitives.deep_context_v2.db.queries_dedupe import ParentRow, append_parent_rows
from packs.ingestion.primitives.deep_context_v2.db.queries_enrich import LinkedinRow, Research
from packs.ingestion.primitives.deep_context_v2.db.schema import DecidedBy, IdentifierKind, MergeReason, Origin, Verdict, Worth
from packs.ingestion.primitives.deep_context_v2.db.owner import owner_background_block, read_owner
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.dedupe.dedupe import mint
from packs.ingestion.primitives.deep_context_v2.enrich import jev_identity, proposals, research
from packs.ingestion.primitives.deep_context_v2.enrich.family import Family, confirm, judgment_fingerprint
from packs.ingestion.primitives.deep_context_v2.enrich.profiles import Profile, Profiles, load_profiles
from packs.ingestion.primitives.deep_context_v2.enrich.proposals import Proposals
from packs.ingestion.primitives.deep_context_v2.openai import OpenAIResponsesCaller, OpenAIResponsesConfig, load_env
from packs.ingestion.schemas.people_schema import normalize_linkedin_url

_HERE = Path(__file__).parent
SYSTEM_PROMPT: str = (_HERE / "relationship_system.txt").read_text(encoding="utf-8").removesuffix("\n")
SCHEMA: dict[str, Any] = json.loads((_HERE / "relationship_schema.txt").read_text(encoding="utf-8"))
SCHEMA_NAME = "relationship"
MODEL = "gpt-6.1-sol"
REASONING_EFFORT = "medium"
VERSION = "enrich-judge-2026-10-07"  # in every judgment fingerprint: a new judge is a new judgment
OUTPUT_TOKENS_PER_CALL = 1500  # assumed Sol output+reasoning tokens per family, estimate only
JEV_CACHE_RELATIVE_DIR = Path("deep-context") / "identity"  # <data root>/deep-context/identity/<view>/jev/
SOL_VERDICTS: dict[str, str] = {"yes": Verdict.CONFIRMED.value, "no": Verdict.WRONG_PERSON.value,
                                "review": Verdict.NEEDS_REVIEW.value}


@dataclass(frozen=True)
class Candidate:
    """One profile proposed for a family."""

    origin: str
    profile: Profile


@dataclass(frozen=True)
class Task:
    family: Family
    candidates: tuple[Candidate, ...]  # one per member id, in proposal order
    fingerprint: str
    citations: tuple[dict[str, Any], ...]  # the research row's source URLs, titles and excerpts


@dataclass(frozen=True)
class Plan:
    tasks: list[Task]
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


def plan(proposals: Proposals, profiles: dict[str, Profile], judged: set[tuple[str, str]]) -> Plan:
    tasks: list[Task] = []
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
        candidates: list[Candidate] = []
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
            candidates.append(Candidate(proposal.origin, profile))
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
        tasks.append(Task(family, tuple(candidates), fingerprint,
                          _citations(proposals.research.get(research.handle(family)))))
    return Plan(tasks, entering, no_profile, already)


def contact(family: Family) -> dict[str, Any]:
    """The family's evidence: its written names, facts, emails, phones and message count."""
    emails: list[str] = []
    phones: list[str] = []
    for identifier in family.identifiers:
        if identifier.kind == IdentifierKind.EMAIL:
            emails.append(identifier.normalized_value)
        else:
            phones.append(identifier.normalized_value)
    return {"source_names": list(family.names), "facts": family.facts.to_payload(), "emails": sorted(emails),
            "phones": sorted(phones), "message_count": family.messages}


def jev_pairs(task: Task) -> list[dict[str, dict[str, Any]]]:
    """The two view requests for each of the task's profiles."""
    network_urls: list[str] = []
    for candidate in task.candidates:
        if candidate.origin == Origin.LINKEDIN_NETWORK:
            network_urls.append(candidate.profile.linkedin_url)
    pairs: list[dict[str, dict[str, Any]]] = []
    for candidate in task.candidates:
        pairs.append(jev_identity.requests(contact(task.family), candidate.profile.judge_view(),
                                           candidate.origin == Origin.LINKEDIN_NETWORK, network_urls))
    return pairs


def sol_prompt(task: Task, owner_block: str) -> str:
    """Every URL of the family at once, with the family's evidence and the research sources."""
    profiles: list[dict[str, Any]] = []
    for candidate in task.candidates:
        view: dict[str, Any] = candidate.profile.judge_view()
        profiles.append({"url": candidate.profile.linkedin_url, "origin": candidate.origin, "name": view["full_name"],
                         "headline": view["headline"], "location": view["location"],
                         "experiences": view["experiences"], "education": view["education"]})
    payload: dict[str, Any] = {"owner": owner_block, "contact": contact(task.family), "candidates": profiles,
                               "research": list(task.citations)}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def sol_verdicts(answer: dict[str, Any], task: Task) -> dict[str, str]:
    """URL -> verdict. The answer must name exactly the supplied URLs; more than one yes is not an answer,
    so every yes then goes to the human queue."""
    jsonschema.validate(answer, SCHEMA)
    expected: set[str] = set()
    for candidate in task.candidates:
        expected.add(candidate.profile.linkedin_url)
    verdicts: dict[str, str] = {}
    yes: int = 0
    for row in answer["candidates"]:
        verdicts[normalize_linkedin_url(row["url"])] = SOL_VERDICTS[row["verdict"]]
        if row["verdict"] == "yes":
            yes += 1
    if set(verdicts) != expected:
        raise ValueError("the judge must answer exactly the supplied URLs")
    if yes > 1:
        for url, verdict in verdicts.items():
            if verdict == Verdict.CONFIRMED:
                verdicts[url] = Verdict.NEEDS_REVIEW.value
    return verdicts


async def sol(caller: OpenAIResponsesCaller, task: Task, owner_block: str) -> dict[str, str]:
    answer: dict[str, Any] = await caller.call(system_prompt=SYSTEM_PROMPT, user_prompt=sol_prompt(task, owner_block),
                                               schema=SCHEMA, schema_name=SCHEMA_NAME, context="enrich-judge")
    return sol_verdicts(answer, task)


def write(conn: sqlite3.Connection, task: Task, verdicts: dict[str, str], now: str) -> dict[str, int]:
    """One verdict row per member per judged URL. A confirmed URL moves every member onto li:<member id>,
    except a member already holding a wrong-person verdict on it: that one is split onto a fresh p: id."""
    family: Family = task.family
    rows: list[LinkedinRow] = []
    parents: list[ParentRow] = []
    counts: dict[str, int] = {"confirmed": 0, "wrong_person": 0, "needs_review": 0, "split": 0}
    for candidate in task.candidates:
        url: str = candidate.profile.linkedin_url
        if url not in verdicts:
            continue  # not judged: another member id was confirmed by JEV alone
        verdict: str = verdicts[url]
        counts[verdict] += 1
        if verdict != Verdict.CONFIRMED:
            for candidate_id in family.candidates:
                rows.append((candidate_id, url, candidate.profile.member_id, candidate.origin, verdict,
                             DecidedBy.MACHINE.value, task.fingerprint, now))
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
        parents.extend(confirm(conn, moved, url, candidate.profile.member_id, candidate.origin, task.fingerprint, now))
    queries_enrich.append_linkedins(conn, rows)
    append_parent_rows(conn, parents)
    counts["parent_rows"] = len(parents)
    return counts


def estimate(tasks: list[Task], cache_dir: Path, owner_block: str) -> dict[str, object]:
    """The dry run: JEV requests and their price, and Sol at most once per family. No call."""
    requests: int = 0
    cached: int = 0
    jev_tokens: int = 0
    sol_tokens: int = 0
    encoder = tiktoken.get_encoding("o200k_base")
    for task in tasks:
        for pair in jev_pairs(task):
            for view, request in pair.items():
                requests += 1
                if jev_identity.is_cached(cache_dir, view, request):
                    cached += 1
                    continue
                jev_tokens += jev_identity.input_tokens(request)
        sol_tokens += len(encoder.encode(SYSTEM_PROMPT + sol_prompt(task, owner_block)))
    output: int = len(tasks) * OUTPUT_TOKENS_PER_CALL
    jev_usd: float = jev_identity.cost_usd(jev_tokens)
    sol_usd: float = estimate_cost_usd(sol_tokens, output, MODEL)
    return {"jev_requests": requests, "jev_cached": cached, "jev_input_tokens": jev_tokens,
            "jev_cost_usd": round(jev_usd, 4), "sol_calls_at_most": len(tasks), "sol_input_tokens": sol_tokens,
            "sol_output_tokens_assumed": output, "sol_cost_usd_at_most": round(sol_usd, 4),
            "estimated_cost_usd_at_most": round(jev_usd + sol_usd, 4)}


async def decide(conn: sqlite3.Connection, tasks: list[Task], cache_dir: Path, owner_block: str, now: str) -> dict[str, int]:
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
    for_sol: list[Task] = []
    position: int = 0
    for task in tasks:
        confirmed: list[str] = []
        for candidate in task.candidates:
            if jev[position] == Verdict.CONFIRMED:
                confirmed.append(candidate.profile.linkedin_url)
            position += 1
        if len(confirmed) == 1:
            _tally(counts, write(conn, task, {confirmed[0]: Verdict.CONFIRMED.value}, now))
            conn.commit()
            continue
        for_sol.append(task)
    config = OpenAIResponsesConfig.resolve(model=MODEL, effort=REASONING_EFFORT, timeout=300, max_retries=2)
    async with OpenAIResponsesCaller(config) as caller:
        calls: list[asyncio.Task[tuple[Task, dict[str, str] | None]]] = []
        for task in for_sol:
            calls.append(asyncio.create_task(_guarded(caller, task, owner_block)))
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


async def _guarded(caller: OpenAIResponsesCaller, task: Task, owner_block: str) -> tuple[Task, dict[str, str] | None]:
    try:
        return task, await sol(caller, task, owner_block)
    except Exception as exc:
        print(f"failed one family: {type(exc).__name__}")  # the message may quote the prompt; never printed
        return task, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="07 Enrich step 4, the JEV and Sol LinkedIn judge: counts and price "
                                                 "only. The judge runs through enrich.")
    parser.add_argument("--data-root", type=Path, default=Path(".powerpacks"))
    parser.add_argument("--limit", type=int, default=None, help="price only the first N families")
    args = parser.parse_args(argv)
    conn = open_store(store_path(args.data_root))
    found: Proposals = proposals.derive(conn)
    profiles: Profiles = load_profiles(args.data_root, proposals.all_urls(found), fetch=False)
    planned: Plan = plan(found, profiles.found, queries_enrich.machine_judgments(conn))
    tasks: list[Task] = planned.tasks
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
