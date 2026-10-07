"""03 Synthesize: facts per candidate from its own bundle. Worth is not here (stage 06).

Rules (spec, stage 03):
- The unit of work is the candidate; never a parent.
- Strict output: the 16 fields of v1's fact_schema.json, nothing added. gpt-6-luna,
  9,000-character batches, at most 20 per candidate. Several batches collapse into one object.
- Reuse: a candidate whose facts.input_fingerprint equals the fingerprint of what would be
  sent (system prompt, rendered batches, model, effort) is skipped ($0).
- The paid path commits one facts row per finished candidate, so a stopped run resumes
  from its own rows. A candidate whose call fails writes nothing; the run finishes the
  others, then fails with the count, and a rerun redoes the failed ones.

From v1:
- deep_context/synthesis/prompting.py: SYSTEM_PROMPT, OWNER_PROMPT_SUFFIX, owner_identity_block,
  SYNTHESIS_VERSION, FACT_SCHEMA, batches, render_batch
- deep_context/synthesis/facts.py: collapse_fact_records
- deep_context/synthesis/models.py: SynthesizedFacts, FactRecord
- deep_context/collection/models.py: CollectionBundle
- deep_context/shared/openai_responses.py: OpenAIResponsesCaller, OpenAIResponsesConfig, estimate_cost_usd
- packs/indexing/lib/llm_config.py: DEFAULT_SYNTHESIS_MODEL
Copied (the v1 module imports the v1 store):
- synthesis/selection.py:151-153 system prompt assembly
- synthesis/runner.py:142-152,177 batch fan-out, one-or-collapse
- synthesis/runner.py:213-247,266-272 token estimate (o200k_base, 750 output tokens per call)

Changelog:
- 2026-10-06 (Astra review): effort resolved once and used in request, fingerprint and row;
  fingerprint over the rendered prompts; failed candidates no longer drop in-flight results;


Created: 2026-10-06
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import tiktoken

from packs.indexing.lib.llm_config import DEFAULT_SYNTHESIS_MODEL
from packs.ingestion.primitives.deep_context.collection.models import CollectionBundle
from packs.ingestion.primitives.deep_context.shared.openai_responses import (
    OpenAIResponsesCaller,
    OpenAIResponsesConfig,
    estimate_cost_usd,
)
from packs.ingestion.primitives.deep_context.synthesis import prompting
from packs.ingestion.primitives.deep_context.synthesis.facts import collapse_fact_records
from packs.ingestion.primitives.deep_context.synthesis.models import FactRecord, SynthesizedFacts
from packs.ingestion.primitives.deep_context_v2.db.owner import OwnerProfile, owner_background_block, read_owner
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso, open_store, store_path
from packs.ingestion.primitives.deep_context_v2.node import Node

MODEL = DEFAULT_SYNTHESIS_MODEL
REASONING_EFFORT = "medium"  # the pinned default; the resolved config may carry the one env override
CHUNK_CHARS = 9000
MAX_BATCHES = 20
OUTPUT_TOKENS_PER_CALL = 750  # runner.py:268-270: assumed output+reasoning tokens per call, estimate only
FACT_FIELDS = tuple(prompting.FACT_SCHEMA["required"])


@dataclass(frozen=True)
class Work:
    candidate_id: str
    prompts: tuple[str, ...]
    fingerprint: str


def system_prompt(owner: OwnerProfile) -> str:
    """selection.py:151-153. owner_identity_block reads only .name and .emails."""
    return (prompting.SYSTEM_PROMPT + prompting.owner_identity_block(owner)
            + prompting.OWNER_PROMPT_SUFFIX + owner_background_block(owner))


def batch_prompts(bundle: CollectionBundle) -> tuple[str, ...]:
    return tuple(prompting.render_batch(bundle, batch, None)
                 for batch in prompting.batches(bundle.messages, chunk_chars=CHUNK_CHARS, max_batches=MAX_BATCHES))


def input_fingerprint(prompts: tuple[str, ...], system: str, config: OpenAIResponsesConfig) -> str:
    """The paid-cache key: exactly what would be sent, and to what. Change any and the candidate
    is synthesized again; change the bundle outside the rendered window and it is not."""
    payload = {
        "synthesis_version": prompting.SYNTHESIS_VERSION,
        "system_prompt": system,
        "prompts": list(prompts),
        "model": config.model,
        "reasoning_effort": config.effort,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class Synthesize(Node):
    name = "synthesize"
    reads = ("bundles", "owner", "facts")
    writes = ("facts",)

    def __init__(self, conn: sqlite3.Connection, data_root: Path, *, limit: int | None) -> None:
        super().__init__(conn, data_root)
        self.limit = limit
        self.config = OpenAIResponsesConfig.resolve(
            model=MODEL, effort=REASONING_EFFORT, concurrency=None, timeout=120, max_retries=3,
        )
        self.prompt = system_prompt(read_owner(conn))

    def work(self) -> list[Work]:
        """Every bundle whose facts row is absent or carries another fingerprint."""
        rows = self.conn.execute(
            "SELECT b.candidate_id, b.payload_json, f.input_fingerprint AS done "
            "FROM bundles b LEFT JOIN facts f USING (candidate_id) ORDER BY b.candidate_id"
        ).fetchall()
        todo = []
        for row in rows:
            prompts = batch_prompts(CollectionBundle.from_payload(json.loads(row["payload_json"])))
            fingerprint = input_fingerprint(prompts, self.prompt, self.config)
            if fingerprint != row["done"]:
                todo.append(Work(row["candidate_id"], prompts, fingerprint))
        return todo

    def estimate(self) -> dict[str, object]:
        encoder = tiktoken.get_encoding("o200k_base")
        pending = self.work()
        todo = pending[: self.limit]
        calls = sum(len(item.prompts) for item in todo)
        input_tokens = sum(len(encoder.encode(self.prompt + prompt)) for item in todo for prompt in item.prompts)
        bundles = self.conn.execute("SELECT count(*) FROM bundles").fetchone()[0]
        output_tokens = calls * OUTPUT_TOKENS_PER_CALL
        return {
            "bundles": bundles,
            "fresh": bundles - len(pending),
            "pending": len(pending),
            "work": len(todo),
            "calls": calls,
            "input_tokens": input_tokens,
            "output_tokens_assumed": output_tokens,
            "model": self.config.model,
            "reasoning_effort": self.config.effort,
            "synthesis_version": prompting.SYNTHESIS_VERSION,
            "estimated_cost_usd": estimate_cost_usd(input_tokens, output_tokens, self.config.model),
        }

    def execute(self) -> dict[str, int]:
        todo = self.work()[: self.limit]
        written, failed = asyncio.run(self._run(todo))
        if failed:
            raise RuntimeError(f"{failed} of {len(todo)} candidates failed; {written} written, rerun to redo the rest")
        return {"work": len(todo), "facts_written": written, "failed": 0}

    async def _run(self, todo: list[Work]) -> tuple[int, int]:
        async with OpenAIResponsesCaller(self.config) as caller:
            tasks = [asyncio.create_task(self._guarded(caller, item)) for item in todo]
            written = failed = 0
            for task in asyncio.as_completed(tasks):
                item, facts = await task
                if facts is None:
                    failed += 1
                    continue
                self.conn.execute(
                    "INSERT INTO facts (candidate_id, facts_json, input_fingerprint, model, reasoning_effort, "
                    "synthesized_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (candidate_id) DO UPDATE SET "
                    "facts_json = excluded.facts_json, input_fingerprint = excluded.input_fingerprint, "
                    "model = excluded.model, reasoning_effort = excluded.reasoning_effort, "
                    "synthesized_at = excluded.synthesized_at",
                    (item.candidate_id, json.dumps(facts, ensure_ascii=False), item.fingerprint,
                     self.config.model, self.config.effort, now_iso()),
                )
                self.conn.commit()
                written += 1
            return written, failed

    async def _guarded(self, caller: OpenAIResponsesCaller, item: Work) -> tuple[Work, dict[str, object] | None]:
        """One candidate's failure is that candidate's: print it, write nothing, let the others finish."""
        try:
            return item, await self._facts(caller, item)
        except Exception as exc:
            print(f"failed one candidate: {type(exc).__name__}: {exc}")  # ids are emails; never printed
            return item, None

    async def _facts(self, caller: OpenAIResponsesCaller, item: Work) -> dict[str, object]:
        """runner.py:142-177: every batch at once; one answer is the facts, several collapse."""
        responses = await asyncio.gather(*(
            caller.call(system_prompt=self.prompt, user_prompt=prompt, schema=prompting.FACT_SCHEMA,
                        schema_name="person_facts", context="synthesize")
            for prompt in item.prompts
        ))
        chunks = [SynthesizedFacts.from_payload(response.payload) for response in responses]
        merged = chunks[0] if len(chunks) == 1 else collapse_fact_records(FactRecord(facts) for facts in chunks)
        payload = merged.to_payload()
        return {field: payload[field] for field in FACT_FIELDS}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="03 Synthesize: facts per candidate (gpt-6-luna).")
    parser.add_argument("--data-root", type=Path, default=Path(".powerpacks"))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", help="tiktoken estimate only; no API call, no manifest")
    args = parser.parse_args(argv)
    node = Synthesize(open_store(store_path(args.data_root)), args.data_root, limit=args.limit)
    if args.dry_run:
        print(json.dumps(node.estimate(), indent=2))
        return 0
    manifest = node.run()
    print(manifest.status, " ".join(f"{key}={value}" for key, value in manifest.counts.items()), manifest.error or "")
    return 0 if manifest.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
