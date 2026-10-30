"""Judge LinkedIn and research identities; attach only an unopposed match at 0.90."""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import tiktoken

from packs.indexing.lib.openai_responses import estimate_cost_usd
from packs.ingestion.primitives.deep_context_v2.db import queries_enrich
from packs.ingestion.primitives.deep_context_v2.db.queries_dedupe import ParentRow, append_parent_rows
from packs.ingestion.primitives.deep_context_v2.db.queries_enrich import Research
from packs.ingestion.primitives.deep_context_v2.db.schema import SYNTHETIC_PROFILE_PREFIX, DecidedBy, MergeReason, Origin, Verdict, Worth
from packs.ingestion.primitives.deep_context_v2.db.owner import owner_background_block, read_owner
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.dedupe.dedupe import mint
from packs.ingestion.primitives.deep_context_v2.enrich import proposals, research, sol_identity
from packs.ingestion.primitives.deep_context_v2.enrich.sol_identity import SolVerdict
from packs.ingestion.primitives.deep_context_v2.enrich.family import Family, confirm, judgment_fingerprint
from packs.ingestion.primitives.deep_context_v2.enrich.profiles import Profile, Profiles, load_profiles
from packs.ingestion.primitives.deep_context_v2.enrich.proposals import Proposals
from packs.ingestion.primitives.deep_context_v2.openai import OpenAIResponsesCaller, OpenAIResponsesConfig
from packs.ingestion.schemas.people_schema import normalize_linkedin_url

VERSION = "enrich-judge-2026-10-20-match-probability"
MATCH_THRESHOLD = 0.90
OUTPUT_TOKENS_PER_CALL = 1500


@dataclass(frozen=True)
class ProposedProfile:
    origin: str
    profile: Profile
    research_result: dict[str, Any] | None = None

    @property
    def supported(self) -> bool:
        """A name and location alone are not a competing biography."""
        if self.origin == Origin.SYNTHETIC:
            assert self.research_result is not None
            content = self.research_result["content"]
            return bool(content.get("summary") or content.get("work_experience") or content.get("education"))
        return bool(self.profile.experiences or self.profile.education
                    or self.profile.headline.strip() not in ("", "-", "--"))


@dataclass(frozen=True)
class JudgeTask:
    family: Family
    candidates: tuple[ProposedProfile, ...]
    fingerprint: str
    citations: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class JudgePlan:
    tasks: list[JudgeTask]
    entering: int
    no_profile: int
    already_judged: int


def _citations(row: Research | None) -> tuple[dict[str, Any], ...]:
    found: list[dict[str, Any]] = []
    if row is None or row.result_json is None:
        return ()
    for basis in json.loads(row.result_json).get("basis", []):
        for citation in basis.get("citations") or []:
            kept = {key: citation[key] for key in ("url", "title", "excerpts") if key in citation}
            if kept not in found:
                found.append(kept)
    return tuple(found)


def plan(proposals: Proposals, profiles: dict[str, Profile], judged: set[tuple[str, str]]) -> JudgePlan:
    tasks: list[JudgeTask] = []
    entering = no_profile = already = 0
    for family in proposals.families:
        if not family.minted or family.worth.worth == Worth.NO or family.human_linkedin:
            continue
        row = proposals.research.get(research.handle(family.facts))
        candidates: list[ProposedProfile] = []
        seen: set[str] = set()
        profiled: list[tuple[str, str, str]] = []
        for proposal in proposals.proposed.get(family.parent_id, []):
            profile = profiles.get(proposal.linkedin_url)
            if profile is None:
                continue
            profiled.append((profile.linkedin_url, proposal.origin, profile.fetched_at))
            wrong = {v.candidate_id for v in family.verdicts
                     if v.member_id == profile.member_id and v.verdict == Verdict.WRONG_PERSON}
            if profile.member_id in seen or len(wrong) == len(family.candidates):
                continue
            seen.add(profile.member_id)
            context = None
            if row is not None and row.result_json is not None:
                result = json.loads(row.result_json)
                if _same_identity(ProposedProfile(proposal.origin, profile),
                                  ProposedProfile(Origin.SYNTHETIC.value, profile, result)):
                    context = result
            candidates.append(ProposedProfile(proposal.origin, profile, context))
        if row is not None and row.result_json is not None:
            result = json.loads(row.result_json)
            content = result["content"]
            represented = any(candidate.supported and candidate.research_result is not None for candidate in candidates)
            if not represented and content.get("real_name") and (content.get("summary") or content.get("work_experience") or content.get("education")):
                key = SYNTHETIC_PROFILE_PREFIX + research.handle(family.facts)
                profile = Profile(key, "", key, content["real_name"], content.get("summary") or "",
                                  ", ".join(v for v in (content.get("location_city"), content.get("location_country")) if v),
                                  tuple(f"{job.get('title') or ''} @ {job.get('company_name') or ''}"
                                        for job in content.get("work_experience") or []),
                                  tuple(school.get("school_name") or "" for school in content.get("education") or []), "")
                candidates.append(ProposedProfile(Origin.SYNTHETIC.value, profile, result))
                profiled.append((key, Origin.SYNTHETIC.value, row.result_json))
        if not candidates and family.parent_id not in proposals.proposed:
            continue
        entering += 1
        if not candidates:
            no_profile += 1
            continue
        fingerprint = judgment_fingerprint(family, profiled, VERSION)
        if all((candidate_id, fingerprint) in judged for candidate_id in family.candidates):
            already += 1
            continue
        tasks.append(JudgeTask(family, tuple(candidates), fingerprint, _citations(row)))
    return JudgePlan(tasks, entering, no_profile, already)


def _same_identity(linkedin: ProposedProfile, synthetic: ProposedProfile) -> bool:
    """Matching biography and the same research URL describe one identity."""
    if linkedin.origin == Origin.SYNTHETIC or synthetic.origin != Origin.SYNTHETIC or synthetic.research_result is None:
        return False
    content = synthetic.research_result["content"]
    if normalize_linkedin_url(content.get("linkedin_url") or "") == linkedin.profile.linkedin_url:
        return True
    urls: set[str] = set()
    for basis in synthetic.research_result.get("basis", []):
        urls.update(normalize_linkedin_url(c.get("url") or "") for c in basis.get("citations") or [])
    companies = [job["company_name"].casefold() for job in content.get("work_experience") or [] if job.get("company_name")]
    return (linkedin.profile.linkedin_url in urls
            and linkedin.profile.full_name.strip().casefold() == (content.get("real_name") or "").strip().casefold()
            and any(company in job.casefold() for company in companies for job in linkedin.profile.experiences))


async def sol(caller: OpenAIResponsesCaller, task: JudgeTask, owner_block: str) -> dict[str, SolVerdict]:
    answers: dict[str, SolVerdict] = {}
    for candidate in task.candidates:
        prompt = sol_identity.identity_prompt(task.family, candidate.profile, candidate.origin, task.citations,
                                               owner_block, candidate.research_result)
        answers[candidate.profile.linkedin_url] = await sol_identity.verdict(caller, prompt)
    # Group source representations before counting distinct plausible identities.
    groups: list[list[ProposedProfile]] = [[c] for c in task.candidates if c.origin != Origin.SYNTHETIC and c.supported
                                           and answers[c.profile.linkedin_url].verdict != Verdict.WRONG_PERSON]
    for candidate in task.candidates:
        if candidate.origin != Origin.SYNTHETIC or not candidate.supported or answers[candidate.profile.linkedin_url].verdict == Verdict.WRONG_PERSON:
            continue
        matching = [group for group in groups if _same_identity(group[0], candidate)]
        if len(matching) == 1:
            matching[0].append(candidate)
        else:
            groups.append([candidate])
    for candidate in task.candidates:
        url = candidate.profile.linkedin_url
        answer = answers[url]
        if answer.verdict == Verdict.WRONG_PERSON:
            continue
        group = next((g for g in groups if candidate in g), [])
        unopposed = len(groups) <= 1 and (not groups or bool(group))
        if unopposed and answer.match_probability >= MATCH_THRESHOLD:
            verdict = Verdict.CONFIRMED
        elif len(groups) > 1 and group:
            verdict = Verdict.NEEDS_REVIEW
        else:
            verdict = Verdict.INSUFFICIENT_EVIDENCE
        answers[url] = replace(answer, verdict=verdict.value)
    return answers


def write(conn: sqlite3.Connection, task: JudgeTask, verdicts: dict[str, SolVerdict], now: str) -> dict[str, int]:
    family = task.family
    parents: list[ParentRow] = []
    counts = {"confirmed": 0, "wrong_person": 0, "needs_review": 0, "insufficient_evidence": 0, "split": 0}
    for candidate in task.candidates:
        url = candidate.profile.linkedin_url
        answer = verdicts[url]
        counts[answer.verdict] += 1
        if answer.verdict != Verdict.CONFIRMED or candidate.origin == Origin.SYNTHETIC:
            for candidate_id in family.candidates:
                row = (candidate_id, url, candidate.profile.member_id, candidate.origin, answer.verdict,
                       DecidedBy.MACHINE.value, task.fingerprint, None, answer.reason, now)
                queries_enrich.insert_linkedin(conn, row, match_probability=answer.match_probability)
            continue
        wrong = {held.candidate_id: held.seq for held in family.verdicts
                 if held.member_id == candidate.profile.member_id and held.verdict == Verdict.WRONG_PERSON}
        staying: list[str] = []
        for candidate_id in family.candidates:
            if candidate_id in wrong:
                parents.append((candidate_id, mint(), MergeReason.JUDGE_WRONG_PERSON.value,
                                f"candidate_linkedins:{wrong[candidate_id]}", now))
                counts["split"] += 1
            else:
                staying.append(candidate_id)
        parents.extend(confirm(conn, replace(family, candidates=tuple(staying)), url, candidate.profile.member_id,
                               candidate.origin, task.fingerprint, None, answer.reason, now,
                               match_probability=answer.match_probability))
        if candidate.research_result is not None:
            key = SYNTHETIC_PROFILE_PREFIX + research.handle(family.facts)
            for candidate_id in staying:
                row = (candidate_id, key, key, Origin.SYNTHETIC.value, Verdict.CONFIRMED.value,
                       DecidedBy.MACHINE.value, task.fingerprint, None, answer.reason, now)
                queries_enrich.insert_linkedin(conn, row, match_probability=answer.match_probability)
    current_synthetic = SYNTHETIC_PROFILE_PREFIX + research.handle(family.facts)
    for held in family.verdicts:
        if (held.origin == Origin.SYNTHETIC and held.verdict == Verdict.CONFIRMED
                and held.member_id != current_synthetic and held.decided_by == DecidedBy.MACHINE):
            row = (held.candidate_id, held.linkedin_url, held.member_id, held.origin,
                   Verdict.INSUFFICIENT_EVIDENCE.value, DecidedBy.MACHINE.value, task.fingerprint, None,
                   "The contact evidence changed; this research profile has not been confirmed on it.", now)
            queries_enrich.insert_linkedin(conn, row)
        if held.verdict == Verdict.NEEDS_REVIEW and held.linkedin_url not in verdicts and held.decided_by == DecidedBy.MACHINE:
            row = (held.candidate_id, held.linkedin_url, held.member_id, held.origin,
                   Verdict.INSUFFICIENT_EVIDENCE.value, DecidedBy.MACHINE.value, task.fingerprint, None,
                   "The proposed LinkedIn has no usable cached profile to establish identity.", now)
            queries_enrich.insert_linkedin(conn, row)
    append_parent_rows(conn, parents)
    counts["parent_rows"] = len(parents)
    return counts


def estimate(tasks: list[JudgeTask], owner_block: str) -> dict[str, int | float]:
    encoder = tiktoken.get_encoding("o200k_base")
    tokens = calls = 0
    for task in tasks:
        for candidate in task.candidates:
            prompt = sol_identity.identity_prompt(task.family, candidate.profile, candidate.origin, task.citations,
                                                   owner_block, candidate.research_result)
            tokens += len(encoder.encode(sol_identity.SYSTEM_PROMPT + prompt))
            calls += 1
    output = calls * OUTPUT_TOKENS_PER_CALL
    cost = round(estimate_cost_usd(tokens, output, sol_identity.MODEL), 4)
    return {"sol_calls_at_most": calls, "sol_input_tokens": tokens, "sol_output_tokens_assumed": output,
            "sol_cost_usd_at_most": cost, "estimated_cost_usd_at_most": cost}


async def decide(conn: sqlite3.Connection, tasks: list[JudgeTask], owner_block: str, now: str,
                 on_progress: Callable[[str, int, int], None] | None = None) -> dict[str, int]:
    """Write and commit each family's paid answers immediately; reruns skip its fingerprint."""
    if not tasks:
        return {"judge_failed": 0}
    config = OpenAIResponsesConfig.resolve(model=sol_identity.MODEL, effort=sol_identity.REASONING_EFFORT, timeout=300, max_retries=2)
    counts: dict[str, int] = {"judge_failed": 0}
    async with OpenAIResponsesCaller(config) as caller:
        async def guarded(task: JudgeTask) -> tuple[JudgeTask, dict[str, SolVerdict] | None]:
            try:
                return task, await sol(caller, task, owner_block)
            except Exception as exc:
                print(f"failed one family: {type(exc).__name__}")
                return task, None

        if on_progress is not None:
            on_progress("judge", 0, len(tasks))
        calls = [asyncio.create_task(guarded(task)) for task in tasks]
        for done, call in enumerate(asyncio.as_completed(calls), 1):
            task, verdicts = await call
            if verdicts is None:
                counts["judge_failed"] += 1
            else:
                for key, value in write(conn, task, verdicts, now).items():
                    counts[key] = counts.get(key, 0) + value
                conn.commit()
            if on_progress is not None:
                on_progress("judge", done, len(tasks))
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="07 Enrich step 4, the Sol profile judge: counts and price "
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
    estimated: dict[str, int | float] = estimate(tasks, owner_background_block(read_owner(conn)))
    for key, value in estimated.items():
        counts[key] = value
    print(json.dumps(counts, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
