"""Prompt rendering and OpenAI judging for ambiguous identity pairs."""
from __future__ import annotations

import asyncio
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
    try:
        response = await caller.call(
            system_prompt=JUDGE_SYSTEM,
            user_prompt=judge_prompt(first, second),
            schema=JUDGE_SCHEMA,
            schema_name="same_person",
            context="judge",
        )
        return MergeJudgeResult(
            MergeDecision.from_payload(response.payload, judge=JUDGE_LLM),
            MergeUsage.from_payload(response.usage.as_dict()),
        )
    except Exception as exc:  # noqa: BLE001 - SDK retries before result recording
        return MergeJudgeResult(
            MergeDecision.from_payload({}, judge=JUDGE_LLM),
            MergeUsage(),
            f"{type(exc).__name__}: {exc}"[:200],
        )


def judge_pairs(pairs: list[MergePairCandidate], *, model: str,
                requested_effort: str, requested_concurrency: int | None, timeout: int,
                max_retries: int) -> tuple[list[MergePairVerdict], MergeUsage]:
    config = OpenAIResponsesConfig.resolve(
        model=model,
        effort=requested_effort,
        concurrency=requested_concurrency,
        timeout=timeout,
        max_retries=max_retries,
    )
    usage = MergeUsage()
    verdicts: list[MergePairVerdict] = []

    async def driver() -> None:
        nonlocal usage
        async with OpenAIResponsesCaller(config) as caller:
            results = await asyncio.gather(
                *(judge_pair(caller, pair.first, pair.second) for pair in pairs)
            )
        for pair, result in zip(pairs, results, strict=True):
            usage = usage + result.usage
            verdicts.append(MergePairVerdict(
                pair.first,
                pair.second,
                pair.signature,
                result.decision,
            ))

    asyncio.run(driver())
    return verdicts, usage
