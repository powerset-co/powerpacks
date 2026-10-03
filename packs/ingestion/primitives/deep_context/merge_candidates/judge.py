"""One structured Sol judgment per ambiguous identity pair, checkpointed in SQLite.

Changelog:
- 2026-10-03: same, different, and uncertain replace binary JEV and names calls.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Callable

import jsonschema

from packs.ingestion.primitives.common.contact_fields import format_phone_digits
from packs.ingestion.primitives.deep_context.merge_candidates.models import (
    MergeDecision, MergePairCandidate, MergePairVerdict, MergePerson,
)
from packs.ingestion.primitives.deep_context.prompts.loader import load_prompt
from packs.ingestion.primitives.deep_context.shared.openai_responses import (
    DEFAULT_MAX_OUTPUT_TOKENS, OpenAIResponsesCaller, OpenAIResponsesConfig, OpenAIUsage,
)

JUDGE_SYSTEM = load_prompt("identity_merge_system")
SCHEMA = json.loads(load_prompt("identity_merge_schema"))
SCHEMA_NAME = "identity_merge"
JUDGE_LLM = "sol"
MODEL_ID = "gpt-6.1-sol"
REASONING_EFFORT = "high"
MERGE_JUDGE_CHUNK = 500
ESTIMATED_OUTPUT_TOKENS = 1500


def shared_identifier_note(first: MergePerson, second: MergePerson) -> str:
    def phone_provenance(person: MergePerson, digits: str) -> str:
        return "contact record" if digits in set(person.phone_digits) else "attributed by message extraction"

    def email_provenance(person: MergePerson, email: str) -> str:
        return "contact record" if email in set(person.emails) else "attributed by message extraction"

    emails = sorted(first.all_emails & second.all_emails)
    lines = [f"- phone {format_phone_digits(digits)} is in BOTH records "
             f"(A: {phone_provenance(first, digits)}; B: {phone_provenance(second, digits)})"
             for digits in sorted(first.all_phones & second.all_phones)]
    lines += [f"- email {email} is in BOTH records "
              f"(A: {email_provenance(first, email)}; B: {email_provenance(second, email)})"
              for email in emails]
    if not lines:
        return ""
    return ("SHARED IDENTIFIERS (computed by code from normalized values — literally identical "
            "on both sides; formatting differences were already resolved):\n" + "\n".join(lines))


def pair_evidence(first: MergePerson, second: MergePerson) -> str:
    """The rendered A and B records and the identifiers they share."""
    shared = shared_identifier_note(first, second)
    shared_block = f"\n\n{shared}" if shared else ""
    left = first.evidence.render_identity_side(
        "A", first.name, first.emails, first.extra_emails,
    )
    right = second.evidence.render_identity_side(
        "B", second.name, second.emails, second.extra_emails,
    )
    names = (f"ORIGINAL SOURCE CONTACT NAMES:\nA: {json.dumps(first.source_names, ensure_ascii=False)}"
             f"\nB: {json.dumps(second.source_names, ensure_ascii=False)}")
    phones = (f"SOURCE CONTACT PHONES:\nA: {json.dumps(first.phone_digits)}"
              f"\nB: {json.dumps(second.phone_digits)}")
    return f"{left}\n\n{right}\n\n{names}\n\n{phones}{shared_block}"


def judge_prompt(first: MergePerson, second: MergePerson) -> str:
    return f"{pair_evidence(first, second)}\n\nAre A and B the same person, different people, or uncertain?"


def judge_request(first: MergePerson, second: MergePerson, *, owner_name: str = "") -> dict:
    """The full semantic request, including response contract and model settings."""
    return {
        "model": MODEL_ID,
        "reasoning_effort": REASONING_EFFORT,
        "max_output_tokens": int(os.getenv("POWERPACKS_DEEP_CONTEXT_MAX_OUTPUT_TOKENS", str(DEFAULT_MAX_OUTPUT_TOKENS))),
        "system_prompt": JUDGE_SYSTEM,
        "user_prompt": f"Network owner: {owner_name}\n\n{judge_prompt(first, second)}",
        "schema_name": SCHEMA_NAME,
        "schema": SCHEMA,
    }


def request_signature(request: dict) -> str:
    return hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def decision_from_answers(answers: dict) -> MergeDecision:
    """Validate the response at the provider boundary; uncertainty is not rejection."""
    jsonschema.validate(answers, SCHEMA)
    same = {"same": True, "different": False, "uncertain": None}[answers["decision"]]
    evidence = answers["identity_evidence"].strip()
    if same is not None and not evidence:
        raise ValueError("same or different judgment requires affirmative individual identity evidence")
    reason = answers["reason"] + (f" Individual identity evidence: {evidence}" if evidence else "")
    return MergeDecision(same, answers["confidence"], answers["tone_consistent"], reason, JUDGE_LLM)


def judge_pairs(
    pairs: list[MergePairCandidate], *, owner_name: str,
    config: OpenAIResponsesConfig, on_verdict: Callable[[MergePairVerdict], None],
) -> tuple[list[MergePairVerdict], OpenAIUsage, int]:
    """Save each completed decision before waiting for other calls or applying edges."""
    verdicts = []
    errors = []

    async def driver() -> OpenAIUsage:
        async with OpenAIResponsesCaller(config) as caller:
            async def one(pair: MergePairCandidate) -> None:
                request = judge_request(pair.first, pair.second, owner_name=owner_name)
                try:
                    result = await caller.call(
                        system_prompt=request["system_prompt"], user_prompt=request["user_prompt"],
                        schema=request["schema"], schema_name=request["schema_name"],
                        context=f"merge {pair.first.person_id}/{pair.second.person_id}",
                    )
                    decision = decision_from_answers(result.payload)
                except Exception as exc:
                    errors.append(f"{type(exc).__name__}: {exc}"[:200])
                    return
                verdict = MergePairVerdict(pair.first, pair.second, pair.signature, decision)
                on_verdict(verdict)
                verdicts.append(verdict)
            for start in range(0, len(pairs), MERGE_JUDGE_CHUNK):
                await asyncio.gather(*(one(pair) for pair in pairs[start:start + MERGE_JUDGE_CHUNK]))
            return caller.usage

    usage = asyncio.run(driver())
    if errors:
        print(f"[cluster] {len(errors)} judge request(s) failed; retried next run (last: {errors[-1]})", file=sys.stderr)
    return verdicts, usage, len(errors)
