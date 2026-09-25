"""OpenAI Responses runner: concurrent per-person batch fan-out, merge, and fixed fact writes."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tiktoken

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.jev_worth import runner as jev_worth
from packs.ingestion.primitives.deep_context.jev_worth.questions import build_request
from packs.ingestion.primitives.deep_context.shared.common import load_env
from packs.ingestion.primitives.deep_context.shared.openai_responses import (
    OpenAIResponsesCaller,
    estimate_cost_usd,
)
from packs.ingestion.primitives.deep_context.collection.models import CollectionBundle
from packs.ingestion.primitives.deep_context.db.models import ArtifactKind
from packs.ingestion.primitives.deep_context.db.projectors import project_parent_fact
from packs.ingestion.primitives.deep_context.db.queries import artifacts, facts as stored_facts
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.synthesis import prompting, selection
from packs.ingestion.primitives.deep_context.synthesis.facts import collapse_fact_records
from packs.ingestion.primitives.deep_context.synthesis.models import (
    FactRecord,
    JevUsage,
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
from packs.search.primitives.llm_rerank_candidates.jev.client import (
    INPUT_PRICE_PER_MILLION,
    MAX_CONCURRENCY,
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
        # more than one goes through collapse_fact_records, the same-person batch
        # reduction — NOT merge_disjoint_fact_records, which is for blending several
        # different child identities and is wrong here (see facts.py).
        profile = chunks[0].facts if len(chunks) == 1 else collapse_fact_records(chunks)
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


def estimate(db: Db, config: SynthesisConfig, plan: SynthesisPlan) -> dict[str, Any]:
    encoder = tiktoken.get_encoding("o200k_base")
    owner = asdict(plan.owner) if plan.owner else {}
    bundles = selection.effective_parent_bundles(db)
    total_tokens = total_batches = people = 0
    jev_cost = 0.0
    jev_people = 0
    synthesized_ids = {bundle.person_id for bundle in plan.bundles}
    for bundle in plan.bundles:
        if not bundle.messages:
            continue
        people += 1
        # The JEV request needs facts that don't exist yet, so a placeholder
        # profile of the expected output size stands in for the token count only.
        request = build_request(
            facts={"summary": "x " * 750},
            bundle=bundle.to_payload(),
            owner=owner,
            reference_date=now_iso()[:10],
        )
        jev_cost += jev_worth.estimate(request)["cost_usd"]
        jev_people += 1
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
    for parent_id, path in _tagging_paths(db, config, bundles, owner):
        if parent_id in synthesized_ids:
            continue
        record, bundle_payload, timestamp = _tagging_inputs(bundles, parent_id, path)
        request = build_request(
            facts=record["facts"],
            bundle=bundle_payload,
            owner=owner,
            reference_date=timestamp[:10],
        )
        jev_cost += jev_worth.estimate(request, output_dir=config.facts_dir.parent)["cost_usd"]
        jev_people += 1
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
        "max_batches": config.max_batches,
        "estimated_cost_floor_usd": estimated_cost_usd + jev_cost,
        "estimated_cost_ceiling_usd": estimated_cost_usd + jev_cost,
        "jev_people": jev_people,
        "jev_estimated_cost_usd": jev_cost,
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


# People held in memory per JEV chunk; API concurrency is the MAX_CONCURRENCY
# semaphore, not this bound.
TAG_CHUNK_PEOPLE = 200


def _load_facts_record(path: Path) -> dict[str, Any]:
    try:
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except json.JSONDecodeError:
        return {}
    return records[-1] if records else {}


def _backup_facts(path: Path) -> None:
    backup = path.with_suffix(path.suffix + ".bkup")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)


def _chunked(seq: list[Any], size: int) -> Any:
    for index in range(0, len(seq), max(1, size)):
        yield seq[index:index + size]


def _bundle_payload(bundles: dict[str, CollectionBundle], parent_id: str) -> dict[str, Any]:
    bundle = bundles.get(parent_id)
    return bundle.to_payload() if bundle else {}


def _tagging_inputs(
    bundles: dict[str, CollectionBundle],
    parent_id: str,
    path: Path,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    record = _load_facts_record(path)
    bundle = _bundle_payload(bundles, parent_id)
    # A tagged record carries its own updated_at; an untagged one uses the file's mtime.
    timestamp = str(
        record.get("updated_at")
        or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    )
    return record, bundle, timestamp


def _needs_tagging(config: SynthesisConfig, facts: dict[str, Any], request: dict[str, Any]) -> bool:
    if not facts.get("labels"):
        return True
    return not jev_worth.estimate(request, output_dir=config.facts_dir.parent)["cached"]


def _tagging_paths(
    db: Db,
    config: SynthesisConfig,
    bundles: dict[str, CollectionBundle],
    owner: dict[str, Any],
) -> list[tuple[str, Path]]:
    paths: list[tuple[str, Path]] = []
    projected = {
        row.artifact_key: row
        for row in artifacts(db, kind=ArtifactKind.FACTS.value, status="projected", parent_owned=True)
    }
    for fact in stored_facts(db, parent_owned=True):
        artifact = projected.get(fact.artifact_key)
        if artifact is None:
            continue
        path = Path(artifact.path)
        if not path.is_file():
            continue
        record, bundle, timestamp = _tagging_inputs(bundles, fact.parent_id, path)
        facts = record.get("facts") or {}
        if not facts:
            continue
        request = build_request(facts=facts, bundle=bundle, owner=owner, reference_date=timestamp[:10])
        if _needs_tagging(config, facts, request):
            paths.append((fact.parent_id, path))
    return paths


def _tally_jev(total: dict[str, Any], usage: dict[str, Any]) -> None:
    total["people"] += 1
    total["cached"] += int(usage["cached"])
    if usage["cached"]:
        return
    total["input_tokens"] += usage["input_tokens"]
    total["output_tokens"] += usage["output_tokens"]
    total["cost_usd"] += usage["input_tokens"] * INPUT_PRICE_PER_MILLION / 1_000_000


def tag_saved_facts(db: Db, config: SynthesisConfig, plan: SynthesisPlan) -> JevUsage:
    """Label the facts already on disk with JEV, rewriting each record in place.

    Runs after the GPT checkpoints are durable, so a failed label request never
    repeats extraction. Each tagged record is re-projected so SQLite's facts_json
    and machine_worth carry the new worth and labels.
    """
    owner = asdict(plan.owner) if plan.owner else {}
    bundles = selection.effective_parent_bundles(db)
    paths = _tagging_paths(db, config, bundles, owner)
    if not paths:
        return JevUsage()
    load_env()
    usage: dict[str, Any] = {"people": 0, "cached": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    async def tag_all() -> None:
        semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

        async def tag(parent_id: str, path: Path) -> None:
            async with semaphore:
                record, bundle, timestamp = _tagging_inputs(bundles, parent_id, path)
                result = await jev_worth.classify(
                    facts=record.get("facts") or {},
                    bundle=bundle,
                    owner=owner,
                    reference_date=timestamp[:10],
                    output_dir=config.facts_dir.parent,
                )
                facts = record.setdefault("facts", {})
                facts["network_worth"] = result["network_worth"]
                facts["labels"] = result["labels"]
                record["updated_at"] = timestamp
                record["jev_usage"] = result["usage"]
                lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
                lines[-1] = json.dumps(record, ensure_ascii=False)
                _backup_facts(path)
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                project_parent_fact(db, path, parent_id)
                _tally_jev(usage, result["usage"])

        for chunk in _chunked(paths, TAG_CHUNK_PEOPLE):
            await asyncio.gather(*(tag(parent_id, path) for parent_id, path in chunk))

    asyncio.run(tag_all())
    return JevUsage(**usage)
