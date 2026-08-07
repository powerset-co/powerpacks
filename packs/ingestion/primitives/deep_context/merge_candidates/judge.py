"""Prompt rendering and retrying OpenAI judge for ambiguous identity pairs."""
from __future__ import annotations

import asyncio
from typing import Any

from packs.indexing.lib.openai_responses import (
    is_retryable, make_async_client, parse_json_response, reasoning_effort,
    responses_kwargs, usage_tokens,
)
from packs.indexing.lib.openai_stream import drain_pool
from packs.indexing.lib.openai_usage_tiers import env_or_profile_int
from packs.ingestion.primitives.deep_context.common import load_env
from packs.ingestion.primitives.deep_context.merge_candidates.models import (
    MergeDecision,
    MergeJudgeResult,
    MergePairVerdict,
    MergePerson,
    MergeUsage,
    all_emails,
    all_phones,
    fmt_phone,
)
from packs.ingestion.primitives.deep_context.prompts.loader import load_prompt

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

    lines = [f"- phone {fmt_phone(digits)} is in BOTH records "
             f"(A: {phone_provenance(first, digits)}; B: {phone_provenance(second, digits)})"
             for digits in sorted(all_phones(first) & all_phones(second))]
    lines += [f"- email {email} is in BOTH records "
              f"(A: {email_provenance(first, email)}; B: {email_provenance(second, email)})"
              for email in sorted(all_emails(first) & all_emails(second))]
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


async def judge_pair(client: Any, first: MergePerson, second: MergePerson, *, model: str,
                     effort: str, semaphore: asyncio.Semaphore,
                     max_retries: int) -> MergeJudgeResult:
    kwargs = responses_kwargs(model, effort=effort, schema=JUDGE_SCHEMA, schema_name="same_person")
    async with semaphore:
        attempt = 0
        while True:
            try:
                response = await client.responses.create(
                    model=model,
                    input=[{"role": "system", "content": JUDGE_SYSTEM},
                           {"role": "user", "content": judge_prompt(first, second)}],
                    **kwargs,
                )
                return MergeJudgeResult(
                    MergeDecision.from_payload(
                        parse_json_response(response, "judge"), judge=JUDGE_LLM,
                    ),
                    MergeUsage.from_payload(usage_tokens(response)),
                )
            except Exception as exc:  # noqa: BLE001 - retry or report at paid boundary
                attempt += 1
                if is_retryable(exc) and attempt <= max_retries:
                    await asyncio.sleep(min(2 ** attempt, 30))
                    continue
                return MergeJudgeResult(
                    MergeDecision.from_payload({}, judge=JUDGE_LLM),
                    MergeUsage(),
                    f"{type(exc).__name__}: {exc}"[:200],
                )


def judge_pairs(people: list[MergePerson], pairs: list[tuple[int, int, str]], *, model: str,
                requested_effort: str, requested_concurrency: int | None, timeout: int,
                max_retries: int) -> tuple[list[MergePairVerdict], MergeUsage]:
    load_env()
    concurrency = requested_concurrency or env_or_profile_int(
        "POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency", fallback=64)
    effort = reasoning_effort(requested_effort)
    usage = MergeUsage()
    verdicts: list[MergePairVerdict] = []

    async def driver() -> None:
        nonlocal usage
        client = make_async_client(timeout=timeout)
        semaphore = asyncio.Semaphore(max(1, concurrency))
        results: dict[int, MergeJudgeResult] = {}

        def on_result(item: tuple[int, MergeJudgeResult]) -> None:
            results[item[0]] = item[1]

        async def one(
            index: int,
            left: int,
            right: int,
        ) -> tuple[int, MergeJudgeResult]:
            return index, await judge_pair(client, people[left], people[right], model=model,
                                           effort=effort, semaphore=semaphore,
                                           max_retries=max_retries)
        try:
            await drain_pool([one(i, a, b) for i, (a, b, _sig) in enumerate(pairs)], on_result)
        finally:
            await client.close()
        for index, (left, right, signature) in enumerate(pairs):
            result = results.get(index) or MergeJudgeResult(
                MergeDecision.from_payload({}, judge=JUDGE_LLM),
                MergeUsage(),
            )
            usage = usage + result.usage
            verdicts.append(MergePairVerdict(
                left,
                right,
                signature,
                result.decision,
            ))

    asyncio.run(driver())
    return verdicts, usage
