"""OpenAI Responses runner: concurrent per-person batch fan-out, merge, and fixed fact writes."""

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
from packs.ingestion.primitives.deep_context.synthesis.facts import merge_batch_facts
from packs.ingestion.primitives.deep_context.synthesis.models import (
    FactRecord,
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

# Empirical batches/sec for the dry-run wall-clock estimate only; real
# throughput is bounded by config.responses.concurrency, not this constant.
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
        # Zero usage: a failed call adds nothing to the run's token tally, but it
        # still consumes a batch slot toward total_failure below.
        return SynthesisCallResult(None, SynthesisUsage(), True)


async def synthesize_person(
    caller: OpenAIResponsesCaller,
    person: CollectionBundle,
    *,
    config: SynthesisConfig,
    system_prompt: str,
) -> SynthesisResult:
    """Fan every batch out concurrently (prior=None each) and merge the results.

    No iteration: on the real install 86.5% of people have exactly one batch,
    so the old confidence/saturation loop bought nothing for them, and for the
    rest it traded per-batch attention (a model asked to refine tends to
    condense) for an early stop that rarely mattered.
    """
    person_batches = prompting.batches(
        person.messages,
        chunk_chars=config.chunk_chars,
        max_batches=config.max_batches,
    )
    # Concurrency is bounded by caller's own semaphore (config.responses.concurrency),
    # same as run_paid's per-person fan-out below — not by anything here.
    calls = await asyncio.gather(*(
        call_one(caller, prompting.render_batch(person, batch, None), system_prompt=system_prompt)
        for batch in person_batches
    ))
    usage_total = dict.fromkeys(TOKEN_KEYS, 0)
    messages_used = errors = 0
    chunks: list[FactRecord] = []
    for batch, call in zip(person_batches, calls):
        for key, value in call.usage.as_dict().items():
            usage_total[key] += value
        messages_used += len(batch)
        errors += int(call.failed)
        if call.facts:
            chunks.append(FactRecord(call.facts))

    batches_used = len(person_batches)
    # Every batch called, none usable: persisting this as a completed record
    # would let selection.pending_target_bundles match its fingerprint and
    # skip the person forever. run_paid must not write/project this result.
    total_failure = bool(person_batches) and not chunks
    if total_failure:
        profile, stop_reason, fingerprint = None, "failed", ""
    else:
        # One batch means no merge at all (the single result IS the profile);
        # more than one goes through merge_batch_facts, the same-person batch
        # reduction — NOT merge_fact_records, which is for blending several
        # different child identities and is wrong here (see facts.py).
        profile = chunks[0].facts if len(chunks) == 1 else merge_batch_facts(chunks)
        # prompting.batches() already truncates its return to max_batches, so
        # reaching that count IS the ceiling, not a coincidence.
        stop_reason = "max_batches" if batches_used >= config.max_batches else "completed"
        fingerprint = prompting.input_evidence_fingerprint(
            person,
            system_prompt=system_prompt,
            chunk_chars=config.chunk_chars,
            max_batches=config.max_batches,
        )
    record = SynthesisRecord(
        synthesis_version=prompting.SYNTHESIS_VERSION,
        input_evidence_fingerprint=fingerprint,
        facts=profile,
        usage=SynthesisUsage.from_payload(usage_total),
        batches_used=batches_used,
        batches_total=len(person_batches),
        messages_used=messages_used,
        messages_available=person.messages_available,
        # `confidence` (model self-report, fact_schema.json) stays a real field
        # for display and validate_dossiers.py's completeness scoring — it just
        # no longer picks the stop_reason or gates a merge. Do not wire it back
        # into control flow here: that reintroduces the loop this change removed.
        final_confidence=round(profile.confidence if profile else 0.0, 2),
        stop_reason=stop_reason,
    )
    return SynthesisResult(person.person_id, record, errors, total_failure=total_failure)


def estimate(config: SynthesisConfig, plan: SynthesisPlan) -> dict[str, Any]:
    encoder = tiktoken.get_encoding("o200k_base")
    total_tokens = total_batches = people = 0
    for bundle in plan.bundles:
        if not bundle.messages:
            continue
        people += 1
        person_batches = prompting.batches(
            bundle.messages,
            chunk_chars=config.chunk_chars,
            max_batches=config.max_batches,
        )
        # No early stop: every one of a person's batches always runs, so there
        # is one real cost per person, not a floor/ceiling range — and no
        # 350-token "prior profile" fudge, since no batch ever renders a prior
        # (rendered with prior=None here, same as input_evidence_fingerprint).
        total_tokens += sum(
            len(encoder.encode(plan.system_prompt + prompting.render_batch(bundle, batch, None)))
            for batch in person_batches
        )
        total_batches += len(person_batches)
    # Still called floor/ceiling for output-shape stability: the two numbers
    # are now the same value because both scenarios ARE the same scenario.
    estimated_cost_usd = estimate_cost_usd(
        total_tokens,
        # 750 = an assumed output+reasoning tokens/call; real output size isn't
        # knowable before the call runs.
        total_batches * 750,
        config.responses.model,
    )
    return {
        "source": "synthesize_person_context",
        "status": "dry_run",
        "people": people,
        "batches_ceiling": total_batches,
        "model": config.responses.model,
        "synthesis_version": prompting.SYNTHESIS_VERSION,
        "reasoning_effort": config.responses.effort,
        "owner_context": True,
        "orphan_facts_removed": 0,
        "rejudge": config.rejudge,
        "max_batches": config.max_batches,
        "estimated_cost_floor_usd": estimated_cost_usd,
        "estimated_cost_ceiling_usd": estimated_cost_usd,
        "estimated_wall_seconds_ceiling": round(total_batches / CHUNKS_PER_SEC, 1),
        "note": "approximate (output/reasoning tokens vary with --reasoning-effort); every person's batches all run now (no adaptive stop), so floor and ceiling are the same number.",
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
        tally.record(result)
        if result.total_failure:
            # Every batch errored or came back empty: skip the write+project so
            # selection.pending_target_bundles retries this person next run
            # instead of matching a fingerprint on a false "done".
            print(f"[synthesize] total failure, retrying next run: {result.person_id}", file=sys.stderr, flush=True)
            return
        parent_id = result.person_id
        path = config.facts_dir / f"{parent_id}.jsonl"
        # One line, full overwrite despite the .jsonl name — not an append log.
        # project_parent_fact reads records[-1] defensively for older multi-line files.
        path.write_text(
            json.dumps(result.record.as_dict(), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        projection = project_parent_fact(db, path, parent_id)
        tally.projected_rows += projection.synced_rows
        if tally.people_done % 25 == 0:
            print(f"[synthesize] {tally.people_done}/{total} people", file=sys.stderr, flush=True)

    async def driver() -> None:
        async with OpenAIResponsesCaller(config.responses) as caller:
            # Every pending person's task starts immediately; actual concurrent
            # OpenAI calls are throttled by caller's semaphore
            # (config.responses.concurrency), not by how many tasks exist here.
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
                # Reached only on interruption (on_result raising, signal, etc).
                # Any person not yet reported to on_result has no facts.jsonl —
                # the next run redoes exactly those via selection's fingerprint
                # miss; already-written people are skipped as usual.
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(driver())
    return tally
