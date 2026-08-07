"""OpenAI Responses runner, adaptive stopping, and fixed fact writes."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from typing import Any

import tiktoken

from packs.ingestion.primitives.deep_context.shared.openai_responses import (
    OpenAIResponsesCaller,
    estimate_cost_usd,
)
from packs.ingestion.primitives.deep_context.collection.models import CollectionBundle
from packs.ingestion.primitives.deep_context.db.projectors import project_parent_fact
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.synthesis import prompting
from packs.ingestion.primitives.deep_context.synthesis.models import (
    SynthesizedFacts,
    SynthesisCallResult,
    SynthesisConfig,
    SynthesisPlan,
    SynthesisRecord,
    SynthesisResult,
    SynthesisTally,
    SynthesisUsage,
    TOKEN_KEYS,
)

CHUNKS_PER_SEC = 10.0
_CATEGORY_VALUES = ("work", "personal", "family", "service", "mixed", "unknown")
_CATEGORY_SYNONYMS = {
    "professional": "work",
    "colleague": "work",
    "business": "work",
    "coworker": "work",
    "friend": "personal",
    "social": "personal",
    "relative": "family",
    "vendor": "service",
    "transactional": "service",
    "support": "service",
    "both": "mixed",
    "personal+work": "mixed",
    "work+personal": "mixed",
}


def fact_keys(facts: SynthesizedFacts | None) -> set[str]:
    if facts is None:
        return set()
    keys: set[str] = set()
    for employer in facts.employers:
        if employer.name:
            keys.add(f"emp:{employer.name.lower()}")
    for field_name in ("title", "school", "location", "field_of_study"):
        if value := getattr(facts, field_name):
            keys.add(f"{field_name}:{value.lower()}")
    for topic in facts.topics:
        keys.add(f"topic:{str(topic).lower()}")
    for identifier in facts.identifiers:
        keys.add(f"id:{str(identifier).lower()}")
    for kind in ("emails", "phones", "urls"):
        for identifier in getattr(facts.owned_identifiers, kind):
            keys.add(f"owned:{kind}:{str(identifier).lower()}")
    return keys


def coerce_relationship_category(value: object) -> str:
    label = str(value or "").strip().lower()
    if label in _CATEGORY_VALUES:
        return label
    return _CATEGORY_SYNONYMS.get(label, "unknown")


async def call_one(
    caller: OpenAIResponsesCaller,
    prompt: str,
    *,
    system_prompt: str,
) -> SynthesisCallResult:
    try:
        response = await caller.call(
            system_prompt=system_prompt,
            user_prompt=prompt,
            schema=prompting.FACT_SCHEMA,
            schema_name="person_facts",
            context="synthesize",
        )
        facts: SynthesizedFacts | None = SynthesizedFacts.from_payload(
            response.payload
        )
        if facts:
            facts = replace(
                facts,
                relationship_category=coerce_relationship_category(
                    facts.relationship_category
                ),
            )
        return SynthesisCallResult(
            facts,
            SynthesisUsage.from_payload(response.usage.as_dict()),
            False,
        )
    except Exception:  # noqa: BLE001 - SDK retries before this paid-boundary result
        return SynthesisCallResult(None, SynthesisUsage(), True)


async def synthesize_person(
    caller: OpenAIResponsesCaller,
    person: CollectionBundle,
    *,
    config: SynthesisConfig,
    system_prompt: str,
) -> SynthesisResult:
    person_batches = prompting.batches(
        person.messages,
        chunk_chars=config.chunk_chars,
        max_batches=config.max_batches,
    )
    profile: SynthesizedFacts | None = None
    seen: set[str] = set()
    stale = batches_used = messages_used = errors = 0
    usage_total = dict.fromkeys(TOKEN_KEYS, 0)
    stop_reason = "exhausted"
    for index, batch in enumerate(person_batches):
        if index >= config.max_batches:
            stop_reason = "max_batches"
            break
        call = await call_one(
            caller,
            prompting.render_batch(person, batch, profile),
            system_prompt=system_prompt,
        )
        for key, value in call.usage.as_dict().items():
            usage_total[key] += value
        batches_used += 1
        messages_used += len(batch)
        errors += int(call.failed)
        if call.facts:
            profile = call.facts
        current_keys = fact_keys(call.facts)
        new_keys = current_keys - seen
        seen |= current_keys
        stale = stale + 1 if not new_keys else 0
        confidence = call.facts.confidence if call.facts else 0.0
        if confidence >= config.target_confidence:
            stop_reason = "confident"
            break
        if stale >= config.saturation_rounds:
            stop_reason = "saturated"
            break
    record = SynthesisRecord(
        synthesis_version=prompting.SYNTHESIS_VERSION,
        input_evidence_fingerprint=prompting.input_evidence_fingerprint(
            person,
            system_prompt=system_prompt,
            chunk_chars=config.chunk_chars,
            max_batches=config.max_batches,
        ),
        facts=profile,
        usage=SynthesisUsage.from_payload(usage_total),
        batches_used=batches_used,
        batches_total=len(person_batches),
        messages_used=messages_used,
        messages_available=person.messages_available,
        final_confidence=round(profile.confidence if profile else 0.0, 2),
        stop_reason=stop_reason,
    )
    return SynthesisResult(person.person_id, record, errors)


def estimate(config: SynthesisConfig, plan: SynthesisPlan) -> dict[str, Any]:
    encoder = tiktoken.get_encoding("o200k_base")
    floor_tokens = ceiling_tokens = ceiling_batches = people = 0
    for bundle in plan.bundles:
        if not bundle.messages:
            continue
        people += 1
        person_batches = prompting.batches(
            bundle.messages,
            chunk_chars=config.chunk_chars,
            max_batches=config.max_batches,
        )
        token_counts = [
            len(encoder.encode(plan.system_prompt + prompting.render_batch(bundle, batch, None)))
            for batch in person_batches
        ]
        floor_tokens += token_counts[0] if token_counts else 0
        ceiling_tokens += sum(token_counts) + 350 * max(0, len(token_counts) - 1)
        ceiling_batches += len(token_counts)
    return {
        "source": "synthesize_person_context",
        "status": "dry_run",
        "people": people,
        "batches_ceiling": ceiling_batches,
        "model": config.responses.model,
        "synthesis_version": prompting.SYNTHESIS_VERSION,
        "reasoning_effort": config.responses.effort,
        "owner_context": True,
        "orphan_facts_removed": 0,
        "rejudge": config.rejudge,
        "target_confidence": config.target_confidence,
        "max_batches": config.max_batches,
        "estimated_cost_floor_usd": estimate_cost_usd(
            floor_tokens,
            people * 750,
            config.responses.model,
        ),
        "estimated_cost_ceiling_usd": estimate_cost_usd(
            ceiling_tokens,
            ceiling_batches * 750,
            config.responses.model,
        ),
        "estimated_wall_seconds_ceiling": round(ceiling_batches / CHUNKS_PER_SEC, 1),
        "note": "approximate (output/reasoning tokens vary with --reasoning-effort); floor=1 batch each, ceiling=all batches. Confidence/saturation usually stops near the floor.",
    }


def run_paid(
    db: Db,
    config: SynthesisConfig,
    plan: SynthesisPlan,
) -> SynthesisTally:
    tally = SynthesisTally()
    if not plan.bundles:
        return tally
    total = len(plan.bundles)

    def on_result(result: SynthesisResult) -> None:
        parent_id = result.person_id
        path = config.facts_dir / f"{parent_id}.jsonl"
        path.write_text(
            json.dumps(result.record.as_dict(), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        projection = project_parent_fact(db, path, parent_id)
        tally.projected_rows += projection.synced_rows
        tally.record(result)
        if tally.people_done % 25 == 0:
            print(f"[synthesize] {tally.people_done}/{total} people", file=sys.stderr, flush=True)

    async def driver() -> None:
        async with OpenAIResponsesCaller(config.responses) as caller:
            tasks = [
                asyncio.create_task(
                    synthesize_person(
                        caller,
                        bundle,
                        config=config,
                        system_prompt=plan.system_prompt,
                    )
                )
                for bundle in plan.bundles
                if bundle.messages
            ]
            try:
                for task in asyncio.as_completed(tasks):
                    on_result(await task)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(driver())
    return tally
