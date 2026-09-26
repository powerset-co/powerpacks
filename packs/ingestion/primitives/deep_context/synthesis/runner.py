"""OpenAI Responses runner: concurrent per-person batch fan-out, merge, and fixed fact writes.

Changelog:
  2026-09-25: the tagging pass reads each parent's imported LinkedIn headline from
      the roster (parent_headlines) and re-tags a saved non-yes verdict when the
      title is notable; the cached JEV answer makes that free.
"""

from __future__ import annotations

import asyncio
import json
import hashlib
import shutil
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tiktoken
from openai import APIStatusError

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.jev_worth import runner as jev_worth
from packs.ingestion.primitives.deep_context.jev_worth.questions import build_request
from packs.ingestion.primitives.deep_context.jev_worth.models import WorthFacts, WorthResult
from packs.ingestion.primitives.deep_context.shared.common import load_env
from packs.ingestion.primitives.deep_context.shared.openai_responses import (
    OpenAIResponsesCaller,
    estimate_cost_usd,
)
from packs.ingestion.primitives.deep_context.collection.models import CollectionBundle, MessageObservation
from packs.ingestion.primitives.deep_context.db.models import ArtifactKind, OwnerProfile
from packs.ingestion.primitives.deep_context.db.projectors import project_parent_fact
from packs.ingestion.primitives.deep_context.db.queries import artifacts, facts as stored_facts, people as person_rows
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.ensure_parents.imported_people import (
    ImportedPerson,
    read_imported_people,
)
from packs.ingestion.primitives.deep_context.synthesis import prompting, selection
from packs.ingestion.primitives.deep_context.synthesis.facts import collapse_fact_records
from packs.ingestion.primitives.deep_context.synthesis.history import FactHistory
from packs.ingestion.primitives.deep_context.db.context_queries import parent_histories
from packs.ingestion.primitives.deep_context.synthesis.models import (
    FactRecord,
    JevUsage,
    SynthesizedFacts,
    SynthesisCallResult,
    SynthesisConfig,
    SynthesisFailure,
    SynthesisPlan,
    SynthesisRecord,
    SynthesisResult,
    SynthesisTally,
    SynthesisUsage,
    TOKEN_KEYS,
)
from packs.search.primitives.llm_rerank_candidates.jev.client import (
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
        if facts is None:
            raise ValueError("empty synthesis facts")
        facts = replace(
            facts,
            relationship_category=coerce_relationship_category(facts.relationship_category),
        )
        return SynthesisCallResult(
            facts,
            SynthesisUsage.from_payload(response.usage.as_dict()),
        )
    except Exception as exc:  # noqa: BLE001 - the SDK owns retries
        # Provider messages may contain contact content; retain only type/status.
        error = type(exc).__name__
        if isinstance(exc, APIStatusError):
            error += f" HTTP {exc.status_code}"
        return SynthesisCallResult(None, SynthesisUsage(), error)


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
    failures: list[SynthesisFailure] = []
    for index, (batch, call) in enumerate(zip(person_batches, calls), start=1):
        for key, value in call.usage.as_dict().items():
            usage_total[key] += value
        messages_used += len(batch)
        errors += int(call.failed)
        if call.failed:
            failures.append(SynthesisFailure(person.person_id, index, call.error))
        if call.facts:
            chunks.append(FactRecord(call.facts))

    batches_used = len(person_batches)
    # A partial answer must never claim the entire input fingerprint.
    total_failure = bool(person_batches) and not chunks
    if errors:
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
        messages=tuple(MessageObservation.of(message) for batch in person_batches for message in batch) if not errors else (),
        groups=person.groups,
        source_channels=person.source_channels,
        model=config.responses.model,
        reasoning_effort=config.responses.effort,
        system_prompt_hash=hashlib.sha256(system_prompt.encode()).hexdigest(),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    return SynthesisResult(person.person_id, record, errors, total_failure=total_failure, failures=tuple(failures))


def estimate(db: Db, config: SynthesisConfig, plan: SynthesisPlan) -> dict[str, Any]:
    encoder = tiktoken.get_encoding("o200k_base")
    owner = plan.owner
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
            facts=WorthFacts.from_payload({"summary": "x " * 750}),
            bundle=bundle,
            owner=owner,
            reference_date=now_iso()[:10],
        )
        jev_cost += jev_worth.estimate(request).cost_usd
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
    headlines = parent_headlines(db, config.people_csv)
    for parent_id, path in _tagging_paths(db, config, bundles, owner, headlines=headlines):
        if parent_id in synthesized_ids:
            continue
        facts, bundle, timestamp, history = _tagging_inputs(bundles, parent_id, path)
        request = build_request(
            facts=facts,
            bundle=bundle,
            owner=owner,
            reference_date=timestamp[:10],
            history=history,
        )
        jev_cost += jev_worth.estimate(request, output_dir=config.facts_dir.parent).cost_usd
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
    histories = parent_histories(db)

    def on_result(result: SynthesisResult) -> None:
        tally.record(result)
        if result.errors:
            # Keep any prior completed facts; changed input remains pending.
            for failure in result.failures:
                print(f"[synthesize] {failure.person_id} batch {failure.batch}: {failure.error}",
                      file=sys.stderr, flush=True)
            return
        parent_id = result.person_id
        path = config.facts_dir / f"{parent_id}.jsonl"
        history = histories.get(parent_id, FactHistory())
        record = result.record.as_dict()
        records = (*(item.payload() for item in history.records), record)
        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bkup"))
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records),
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


def _tagging_inputs(
    bundles: dict[str, CollectionBundle], parent_id: str, path: Path,
) -> tuple[WorthFacts, CollectionBundle | None, str, FactHistory]:
    # Keep historical envelope fields and fact order at the file boundary.
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    history = FactHistory.from_records(records)
    record = history.payload()
    timestamp = str(record.get("updated_at") or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat())
    return WorthFacts.from_payload(record["facts"]), bundles.get(parent_id), timestamp, history


def _write_worth(path: Path, result: WorthResult, timestamp: str) -> None:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    record = json.loads(lines[-1])
    record["facts"]["network_worth"] = result.worth.to_payload()
    record["facts"]["labels"] = result.labels
    record["updated_at"] = timestamp
    record["jev_usage"] = result.usage_payload()
    lines[-1] = json.dumps(record, ensure_ascii=False)
    backup = path.with_suffix(path.suffix + ".bkup")
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parent_headlines(db: Db, people_csv: Path) -> dict[str, str]:
    """Each parent's imported LinkedIn headline, read once from the roster.

    A parent takes the first non-empty headline among its members, members with
    a public identifier first; a parent with no roster row or no headline is
    absent from the map.
    """
    roster = {person.person_id: person for person in read_imported_people(people_csv)}
    members: dict[str, list[ImportedPerson]] = {}
    for row in person_rows(db):
        person = roster.get(str(row.person_id))
        if person is not None:
            members.setdefault(str(row.parent_id), []).append(person)
    headlines: dict[str, str] = {}
    for parent_id, people in members.items():
        ordered = sorted(people, key=lambda person: (not person.public_identifier, person.person_id))
        headline = next((person.headline for person in ordered if person.headline), "")
        if headline:
            headlines[parent_id] = headline
    return headlines


def _needs_tagging(
    config: SynthesisConfig, facts: SynthesizedFacts, request: dict[str, Any], *, headline: str,
) -> bool:
    if not facts.labels:
        return True
    decision = facts.network_worth.decision if facts.network_worth else ""
    # A notable title decides worth in code; a saved non-yes verdict is redone
    # from the cached JEV answer, so the re-tag costs nothing.
    if jev_worth.notable_title(headline) and decision != "yes":
        return True
    return not jev_worth.estimate(request, output_dir=config.facts_dir.parent).cached


def _tagging_paths(
    db: Db,
    config: SynthesisConfig,
    bundles: dict[str, CollectionBundle],
    owner: OwnerProfile | None,
    *,
    headlines: dict[str, str],
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
        facts, bundle, timestamp, history = _tagging_inputs(bundles, fact.parent_id, path)
        if not facts.facts.present:
            continue
        request = build_request(facts=facts, bundle=bundle, owner=owner, reference_date=timestamp[:10], history=history)
        if _needs_tagging(config, facts.facts, request, headline=headlines.get(fact.parent_id, "")):
            paths.append((fact.parent_id, path))
    return paths


def tag_saved_facts(db: Db, config: SynthesisConfig, plan: SynthesisPlan) -> JevUsage:
    """Label the facts already on disk with JEV, rewriting each record in place.

    Runs after the GPT checkpoints are durable, so a failed label request never
    repeats extraction. Each tagged record is re-projected so SQLite's facts_json
    and machine_worth carry the new worth and labels.
    """
    owner = plan.owner
    bundles = selection.effective_parent_bundles(db)
    headlines = parent_headlines(db, config.people_csv)
    paths = _tagging_paths(db, config, bundles, owner, headlines=headlines)
    if not paths:
        return JevUsage()
    load_env()
    usage = JevUsage()

    async def tag_all() -> None:
        semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

        async def tag(parent_id: str, path: Path) -> None:
            nonlocal usage
            async with semaphore:
                facts, bundle, timestamp, history = _tagging_inputs(bundles, parent_id, path)
                result = await jev_worth.classify(
                    facts=facts,
                    bundle=bundle,
                    owner=owner,
                    reference_date=timestamp[:10],
                    output_dir=config.facts_dir.parent,
                    headline=headlines.get(parent_id, ""),
                    history=history,
                )
                _write_worth(path, result, timestamp)
                project_parent_fact(db, path, parent_id)
                usage = usage + result.usage

        for start in range(0, len(paths), TAG_CHUNK_PEOPLE):
            await asyncio.gather(*(tag(parent_id, path) for parent_id, path in paths[start:start + TAG_CHUNK_PEOPLE]))

    asyncio.run(tag_all())
    return usage
