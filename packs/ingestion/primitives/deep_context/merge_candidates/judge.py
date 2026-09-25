"""Prompt rendering and OpenAI judging for ambiguous identity pairs.

A judge call that still fails after one immediate retry yields no verdict, so
the pair is judged again on the next run; the failure is counted and reported
on stderr.

Changelog:
- 2026-09-25: a failed call no longer becomes a cached "not same person, 0"
  verdict; retry once, then leave the pair unjudged.
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any

from packs.ingestion.primitives.common.contact_fields import format_phone_digits
from packs.ingestion.primitives.deep_context.merge_candidates.models import (
    MergeDecision,
    MergeJudgeResult,
    MergePairCandidate,
    MergePairVerdict,
    MergePerson,
    MergeUsage,
)
from packs.ingestion.primitives.deep_context.prompts.loader import load_prompt
from packs.ingestion.primitives.deep_context.shared.openai_responses import (
    OpenAIResponsesCaller,
    OpenAIResponsesConfig,
)

JUDGE_SYSTEM = load_prompt("identity_merge_system")
JUDGE_LLM = "llm"
_JUDGE_RETRIES = 1
JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "same_person": {"type": "boolean"}, "confidence": {"type": "number"},
        "tone_toward_a": {"type": "string", "description": "How I address contact A (e.g. casual, formal)"},
        "tone_toward_b": {"type": "string"}, "tone_consistent": {"type": "boolean"},
        "reason": {"type": "string", "description": "One-line rationale, citing tone."},
    },
    "required": ["same_person", "confidence", "tone_toward_a", "tone_toward_b",
                 "tone_consistent", "reason"],
}


def shared_identifier_note(first: MergePerson, second: MergePerson) -> str:
    def phone_provenance(person: MergePerson, digits: str) -> str:
        return "contact record" if digits in set(person.phone_digits) else "owned message evidence"

    def email_provenance(person: MergePerson, email: str) -> str:
        return "contact record" if email in set(person.emails) else "owned message evidence"

    lines = [f"- phone {format_phone_digits(digits)} is in BOTH records "
             f"(A: {phone_provenance(first, digits)}; B: {phone_provenance(second, digits)})"
             for digits in sorted(first.all_phones & second.all_phones)]
    lines += [f"- email {email} is in BOTH records "
              f"(A: {email_provenance(first, email)}; B: {email_provenance(second, email)})"
              for email in sorted(first.all_emails & second.all_emails)]
    if not lines:
        return ""
    return ("SHARED IDENTIFIERS (computed by code from normalized values — literally identical "
            "on both sides; formatting differences were already resolved):\n" + "\n".join(lines))


def judge_prompt(first: MergePerson, second: MergePerson) -> str:
    shared = shared_identifier_note(first, second)
    shared_block = f"\n\n{shared}" if shared else ""
    left = first.evidence.render_identity_side(
        "A", first.name, first.emails, first.extra_emails,
    )
    right = second.evidence.render_identity_side(
        "B", second.name, second.emails, second.extra_emails,
    )
    return f"{left}\n\n{right}{shared_block}\n\nAre A and B the same person?"


async def judge_pair(
    caller: OpenAIResponsesCaller,
    first: MergePerson,
    second: MergePerson,
) -> MergeJudgeResult:
    error = ""
    for _attempt in range(1 + _JUDGE_RETRIES):
        try:
            response = await caller.call(
                system_prompt=JUDGE_SYSTEM,
                user_prompt=judge_prompt(first, second),
                schema=JUDGE_SCHEMA,
                schema_name="same_person",
                context="judge",
            )
        except Exception as exc:  # noqa: BLE001 - an unjudged pair is retried next run
            error = f"{type(exc).__name__}: {exc}"[:200]
            continue
        return MergeJudgeResult(
            MergeDecision.from_payload(response.payload, judge=JUDGE_LLM),
            MergeUsage.from_payload(response.usage.as_dict()),
        )
    return MergeJudgeResult(MergeDecision.from_payload({}, judge=JUDGE_LLM), MergeUsage(), error)


def judge_pairs(pairs: list[MergePairCandidate], *, model: str,
                requested_effort: str, requested_concurrency: int | None, timeout: int,
                max_retries: int) -> tuple[list[MergePairVerdict], MergeUsage, int]:
    """Judge pairs; return verdicts for the successful ones, usage, and the failure count."""
    config = OpenAIResponsesConfig.resolve(
        model=model,
        effort=requested_effort,
        concurrency=requested_concurrency,
        timeout=timeout,
        max_retries=max_retries,
    )
    usage = MergeUsage()
    verdicts: list[MergePairVerdict] = []
    errors: list[str] = []

    async def driver() -> None:
        nonlocal usage
        async with OpenAIResponsesCaller(config) as caller:
            results = await asyncio.gather(
                *(judge_pair(caller, pair.first, pair.second) for pair in pairs)
            )
        for pair, result in zip(pairs, results, strict=True):
            usage = usage + result.usage
            if result.error:
                errors.append(result.error)
                continue
            verdicts.append(MergePairVerdict(
                pair.first,
                pair.second,
                pair.signature,
                result.decision,
            ))

    asyncio.run(driver())
    if errors:
        print(
            f"[cluster] {len(errors)} judge call(s) failed; re-judged next run "
            f"(last: {errors[-1]})",
            file=sys.stderr,
        )
    return verdicts, usage, len(errors)
