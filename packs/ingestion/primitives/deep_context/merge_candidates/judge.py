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
    MergePerson, all_emails, all_phones, fmt_phone,
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


def _render_side(label: str, person: MergePerson) -> str:
    profile = person.profile or {}
    facts = []
    if profile.get("relationship"):
        facts.append(f"relationship: {profile['relationship']}")
    if profile.get("title") or profile.get("employers"):
        facts.append(f"work: {profile.get('title', '')} "
                     f"{('@ ' + ', '.join(profile['employers'])) if profile.get('employers') else ''}".strip())
    if profile.get("school"):
        facts.append(f"school: {profile['school']}")
    if profile.get("location"):
        facts.append(f"location: {profile['location']}")
    if profile.get("topics"):
        facts.append(f"we discuss: {', '.join(profile['topics'])}")
    facts_block = "\n".join(f"  {fact}" for fact in facts) or "  (no extracted facts)"
    mine = "\n".join(f"  me→them: {text}" for text in person.from_me) or "  (no messages from me — tone unavailable)"
    theirs = "\n".join(f"  them→me: {text}" for text in person.from_them) or "  (no messages from them)"
    emails = ", ".join(person.emails) or "none"
    extra = ", ".join(person.extra_emails)
    extra_line = f"  [owned identifier seen in messages: {extra}]\n" if extra else ""
    return (f"CONTACT {label} — {person.name}  [emails: {emails}]\n{extra_line}"
            f"{facts_block}\nMessages:\n{mine}\n{theirs}")


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
    return f"{_render_side('A', first)}\n\n{_render_side('B', second)}{shared_block}\n\nAre A and B the same person?"


async def judge_pair(client: Any, first: MergePerson, second: MergePerson, *, model: str,
                     effort: str, semaphore: asyncio.Semaphore, max_retries: int) -> dict[str, Any]:
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
                return {"verdict": parse_json_response(response, "judge"),
                        "usage": usage_tokens(response), "error": ""}
            except Exception as exc:  # noqa: BLE001 - retry or report at paid boundary
                attempt += 1
                if is_retryable(exc) and attempt <= max_retries:
                    await asyncio.sleep(min(2 ** attempt, 30))
                    continue
                return {"verdict": {},
                        "usage": {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0},
                        "error": f"{type(exc).__name__}: {exc}"[:200]}


def judge_pairs(people: list[MergePerson], pairs: list[tuple[int, int, str]], *, model: str,
                requested_effort: str, requested_concurrency: int, timeout: int,
                max_retries: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    load_env()
    concurrency = requested_concurrency or env_or_profile_int(
        "POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency", fallback=64)
    effort = reasoning_effort(requested_effort)
    usage = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
    verdicts: list[dict[str, Any]] = []

    async def driver() -> None:
        client = make_async_client(timeout=timeout)
        semaphore = asyncio.Semaphore(max(1, concurrency))
        results: dict[int, dict[str, Any]] = {}

        def on_result(item: tuple[int, dict[str, Any]]) -> None:
            results[item[0]] = item[1]

        async def one(index: int, left: int, right: int) -> tuple[int, dict[str, Any]]:
            return index, await judge_pair(client, people[left], people[right], model=model,
                                           effort=effort, semaphore=semaphore,
                                           max_retries=max_retries)
        try:
            await drain_pool([one(i, a, b) for i, (a, b, _sig) in enumerate(pairs)], on_result)
        finally:
            await client.close()
        for index, (left, right, signature) in enumerate(pairs):
            result = results.get(index, {"verdict": {}, "usage": {}})
            for key in usage:
                usage[key] += result.get("usage", {}).get(key, 0)
            verdicts.append({"a": left, "b": right, "sig": signature, "judge": JUDGE_LLM,
                             **(result["verdict"] or {})})

    asyncio.run(driver())
    return verdicts, usage
