"""OpenAI Responses runner, retries, adaptive stopping, and fixed fact writes."""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from typing import Any

import tiktoken

from packs.indexing.lib.openai_responses import (
    estimate_cost_usd,
    is_retryable,
    make_async_client,
    parse_json_response,
    reasoning_effort,
    responses_kwargs,
    usage_tokens,
)
from packs.indexing.lib.openai_stream import drain_pool
from packs.indexing.lib.openai_usage_tiers import env_or_profile_int
from packs.ingestion.primitives.deep_context.common import load_env
from packs.ingestion.primitives.deep_context.synthesis import prompting, selection

CHUNKS_PER_SEC = 10.0
_CATEGORY_VALUES = ("work", "personal", "family", "service", "mixed", "unknown")
_CATEGORY_SYNONYMS = {
    "professional": "work", "colleague": "work", "business": "work", "coworker": "work",
    "friend": "personal", "social": "personal", "relative": "family",
    "vendor": "service", "transactional": "service", "support": "service",
    "both": "mixed", "personal+work": "mixed", "work+personal": "mixed",
}


@dataclass
class SynthesisTally:
    people_done: int = 0
    errors: int = 0
    batches: int = 0
    stop_reasons: dict[str, int] = field(default_factory=dict)
    tokens: dict[str, int] = field(default_factory=lambda: {
        "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
    })

    def record(self, result: dict[str, Any]) -> None:
        for key in self.tokens:
            self.tokens[key] += result["usage"].get(key, 0)
        self.people_done += 1
        self.errors += result["errors"]
        self.batches += result["batches_used"]
        reason = result["stop_reason"]
        self.stop_reasons[reason] = self.stop_reasons.get(reason, 0) + 1


def fact_keys(facts: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for employer in facts.get("employers") or []:
        if employer.get("name"):
            keys.add(f"emp:{employer['name'].lower()}")
    for field_name in ("title", "school", "location", "field_of_study"):
        if facts.get(field_name):
            keys.add(f"{field_name}:{str(facts[field_name]).lower()}")
    for topic in facts.get("topics") or []:
        keys.add(f"topic:{str(topic).lower()}")
    for identifier in facts.get("identifiers") or []:
        keys.add(f"id:{str(identifier).lower()}")
    for kind in ("emails", "phones", "urls"):
        for identifier in (facts.get("owned_identifiers") or {}).get(kind) or []:
            keys.add(f"owned:{kind}:{str(identifier).lower()}")
    return keys


def coerce_relationship_category(value: Any) -> str:
    label = str(value or "").strip().lower()
    if label in _CATEGORY_VALUES:
        return label
    return _CATEGORY_SYNONYMS.get(label, "unknown")


async def call_one(
    client: Any,
    prompt: str,
    *,
    model: str,
    effort: str,
    semaphore: asyncio.Semaphore,
    max_retries: int,
    system_prompt: str,
) -> tuple[dict[str, Any], dict[str, int], str]:
    kwargs = responses_kwargs(
        model, effort=effort, schema=prompting.FACT_SCHEMA, schema_name="person_facts",
    )
    async with semaphore:
        attempt = 0
        while True:
            try:
                response = await client.responses.create(
                    model=model,
                    input=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    **kwargs,
                )
                facts = parse_json_response(response, "synthesize")
                if facts:
                    facts["relationship_category"] = coerce_relationship_category(
                        facts.get("relationship_category")
                    )
                return facts, usage_tokens(response), ""
            except Exception as exc:  # noqa: BLE001 - classify then retry/record
                attempt += 1
                if is_retryable(exc) and attempt <= max_retries:
                    await asyncio.sleep(min(2 ** attempt, 30))
                    continue
                return {}, {
                    "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
                }, f"{type(exc).__name__}: {exc}"[:300]


async def synthesize_person(
    client: Any,
    person: dict[str, Any],
    batches: list[list[dict[str, Any]]],
    *,
    model: str,
    effort: str,
    semaphore: asyncio.Semaphore,
    max_retries: int,
    system_prompt: str,
    target_confidence: float,
    saturation_rounds: int,
    max_batches: int,
) -> dict[str, Any]:
    profile: dict[str, Any] = {}
    seen: set[str] = set()
    stale = batches_used = messages_used = errors = 0
    usage_total = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
    stop_reason = "exhausted"
    for index, batch in enumerate(batches):
        if index >= max_batches:
            stop_reason = "max_batches"
            break
        facts, usage, error = await call_one(
            client,
            prompting.render_batch(person, batch, profile or None),
            model=model,
            effort=effort,
            semaphore=semaphore,
            max_retries=max_retries,
            system_prompt=system_prompt,
        )
        for key in usage_total:
            usage_total[key] += usage.get(key, 0)
        batches_used += 1
        messages_used += len(batch)
        errors += int(bool(error))
        if facts:
            profile = facts
        new_keys = fact_keys(facts) - seen
        seen |= fact_keys(facts)
        stale = stale + 1 if not new_keys else 0
        confidence = float(facts.get("confidence") or 0.0)
        if confidence >= target_confidence:
            stop_reason = "confident"
            break
        if stale >= saturation_rounds:
            stop_reason = "saturated"
            break
    return {
        "person_id": person.get("person_id"), "facts": profile, "usage": usage_total,
        "batches_used": batches_used, "batches_total": len(batches),
        "messages_used": messages_used,
        "messages_available": person.get("messages_available", len(person.get("messages") or [])),
        "final_confidence": round(float(profile.get("confidence") or 0.0), 2),
        "stop_reason": stop_reason, "errors": errors,
    }


def estimate(stage: Any) -> dict[str, Any]:
    encoder = tiktoken.get_encoding("o200k_base")
    plan = stage._plan()
    floor_tokens = ceiling_tokens = ceiling_batches = people = 0
    for path in plan.paths:
        bundle = selection.load_bundle(path)
        if not bundle.get("messages"):
            continue
        people += 1
        person_batches = prompting.batches(
            bundle["messages"], chunk_chars=stage.chunk_chars, max_batches=stage.max_batches,
        )
        if person_batches:
            floor_tokens += len(encoder.encode(
                plan.system_prompt + prompting.render_batch(bundle, person_batches[0], None)
            ))
        for index, batch in enumerate(person_batches):
            ceiling_tokens += len(encoder.encode(
                plan.system_prompt + prompting.render_batch(bundle, batch, None)
            ))
            ceiling_tokens += 350 if index > 0 else 0
            ceiling_batches += 1
    return {
        "source": "synthesize_person_context", "status": "dry_run", "people": people,
        "batches_ceiling": ceiling_batches, "model": stage.model,
        "synthesis_version": prompting.SYNTHESIS_VERSION,
        "reasoning_effort": reasoning_effort(stage.reasoning_effort),
        "owner_context": bool(plan.owner), "orphan_facts_removed": 0,
        "rejudge": bool(stage.rejudge), "target_confidence": stage.target_confidence,
        "max_batches": stage.max_batches,
        "estimated_cost_floor_usd": estimate_cost_usd(floor_tokens, people * 750, stage.model),
        "estimated_cost_ceiling_usd": estimate_cost_usd(
            ceiling_tokens, ceiling_batches * 750, stage.model,
        ),
        "estimated_wall_seconds_ceiling": round(ceiling_batches / CHUNKS_PER_SEC, 1),
        "note": "approximate (output/reasoning tokens vary with --reasoning-effort); floor=1 batch each, ceiling=all batches. Confidence/saturation usually stops near the floor.",
    }


def run_paid(stage: Any, plan: selection.SynthesisPlan, tally: SynthesisTally) -> tuple[int, str]:
    concurrency = 0
    effort = reasoning_effort(stage.reasoning_effort)
    if not plan.paths:
        return concurrency, effort
    load_env()
    effort = reasoning_effort(stage.reasoning_effort)
    concurrency = stage.concurrency or env_or_profile_int(
        "POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency", fallback=16,
    )
    total = len(plan.paths)

    def on_result(result: dict[str, Any]) -> None:
        record = {
            "chunk_index": 0, "synthesis_version": prompting.SYNTHESIS_VERSION,
            "facts": result["facts"], "usage": result["usage"],
            "batches_used": result["batches_used"], "batches_total": result["batches_total"],
            "messages_used": result["messages_used"],
            "messages_available": result["messages_available"],
            "final_confidence": result["final_confidence"], "stop_reason": result["stop_reason"],
        }
        (stage.facts_dir / f"{result['person_id']}.jsonl").write_text(
            json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        tally.record(result)
        if tally.people_done % 25 == 0:
            print(f"[synthesize] {tally.people_done}/{total} people", file=sys.stderr, flush=True)

    async def driver() -> None:
        client = make_async_client(timeout=stage.timeout)
        semaphore = asyncio.Semaphore(max(1, concurrency))
        try:
            for paths in selection.chunked(plan.paths, stage.chunk_people):
                bundles = [
                    bundle for bundle in (selection.load_bundle(path) for path in paths)
                    if bundle.get("messages")
                ]
                person_batches = {
                    bundle["person_id"]: prompting.batches(
                        bundle["messages"],
                        chunk_chars=stage.chunk_chars,
                        max_batches=stage.max_batches,
                    )
                    for bundle in bundles
                }
                coroutines = [
                    synthesize_person(
                        client, bundle, person_batches[bundle["person_id"]],
                        model=stage.model, effort=effort, semaphore=semaphore,
                        max_retries=stage.max_retries, system_prompt=plan.system_prompt,
                        target_confidence=stage.target_confidence,
                        saturation_rounds=stage.saturation_rounds,
                        max_batches=stage.max_batches,
                    )
                    for bundle in bundles
                ]
                await drain_pool(coroutines, on_result)
        finally:
            await client.close()

    asyncio.run(driver())
    return concurrency, effort
