"""Block 03 Synthesize: facts per candidate from its own bundle, with gpt-6-luna.

Each candidate's bundle is rendered newest-first into 9,000-character batches (at most 20); each
batch is one strict-schema call; several batches collapse into one facts object of exactly the 16
schema fields, stored in `facts`. Reuse is keyed on what would be sent: system prompt, rendered
batches, model and effort. A candidate whose row carries that key is skipped at $0.

The paid path commits one row per finished candidate, so a stopped run resumes from its own
rows. A candidate whose call fails writes nothing; the others finish, the run fails with the
count, and a rerun redoes the failed ones. `--dry-run` estimates tokens and cost with tiktoken
and makes no call.

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
from typing import Awaitable

import tiktoken

from packs.indexing.lib.llm_config import DEFAULT_SYNTHESIS_MODEL
from packs.ingestion.primitives.deep_context_v2.collect.bundle import CollectionBundle
from packs.ingestion.primitives.deep_context_v2.db import queries
from packs.ingestion.primitives.deep_context_v2.db.owner import OwnerProfile, owner_background_block, read_owner
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso, open_store, store_path
from packs.ingestion.primitives.deep_context_v2.node import Node
from packs.indexing.lib.openai_responses import estimate_cost_usd
from packs.ingestion.primitives.deep_context_v2.openai import OpenAIResponsesCaller, OpenAIResponsesConfig
from packs.ingestion.primitives.deep_context_v2.synthesize import prompt as prompting
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import SynthesizedFacts, collapse

MODEL = DEFAULT_SYNTHESIS_MODEL
REASONING_EFFORT = "medium"  # the pinned default; the resolved config may carry the one env override
CHUNK_CHARS = 9000           # one batch = up to this many characters of rendered messages
MAX_BATCHES = 20             # a very long history is cut here; newest messages win
OUTPUT_TOKENS_PER_CALL = 750  # assumed output+reasoning tokens per call, estimate only
DEFAULT_LIMIT = 100_000      # more candidates than any store has; --limit N synthesizes the first N pending


@dataclass(frozen=True)
class Work:
    """One candidate to synthesize: its rendered batch prompts and the reuse key they hash to."""

    candidate_id: str
    prompts: tuple[str, ...]
    fingerprint: str


def system_prompt(owner: OwnerProfile) -> str:
    """The fixed system text, who the owner is, and the owner's background. Same for every call."""
    return (prompting.SYSTEM_PROMPT + prompting.owner_identity_block(owner.name, owner.emails)
            + prompting.OWNER_PROMPT_SUFFIX + owner_background_block(owner))


def batch_prompts(bundle: CollectionBundle) -> tuple[str, ...]:
    """One user prompt per batch. Most candidates fit in one; a long history is several."""
    prompts: list[str] = []
    for batch in prompting.batches(bundle.messages, chunk_chars=CHUNK_CHARS, max_batches=MAX_BATCHES):
        prompts.append(prompting.render_batch(bundle, batch))
    return tuple(prompts)


def input_fingerprint(prompts: tuple[str, ...], system: str, config: OpenAIResponsesConfig) -> str:
    """The paid-cache key: exactly what would be sent, and to what. Change any of it and the candidate
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

    def __init__(self, conn: sqlite3.Connection, data_root: Path, *, limit: int) -> None:
        super().__init__(conn, data_root)
        self.limit = limit
        # Resolved once; the same effort goes into the request, the fingerprint and the row.
        self.config = OpenAIResponsesConfig.resolve(model=MODEL, effort=REASONING_EFFORT, timeout=120, max_retries=3)
        self.prompt = system_prompt(read_owner(conn))

    def work(self) -> list[Work]:
        """Every bundle whose facts row is absent or carries another fingerprint."""
        todo: list[Work] = []
        for row in queries.bundles_with_facts_fingerprint(self.conn):
            bundle: CollectionBundle = CollectionBundle.from_payload(json.loads(row.payload_json))
            prompts: tuple[str, ...] = batch_prompts(bundle)
            fingerprint: str = input_fingerprint(prompts, self.prompt, self.config)
            if fingerprint != row.facts_fingerprint:
                todo.append(Work(row.candidate_id, prompts, fingerprint))
        return todo

    def estimate(self) -> dict[str, object]:
        """The dry run: count calls and input tokens with tiktoken, price them. No API call."""
        encoder = tiktoken.get_encoding("o200k_base")
        pending: list[Work] = self.work()
        todo: list[Work] = pending[: self.limit]
        calls: int = 0
        input_tokens: int = 0
        for item in todo:
            for prompt in item.prompts:
                calls += 1
                input_tokens += len(encoder.encode(self.prompt + prompt))
        bundles: int = queries.count_bundles(self.conn)
        output_tokens: int = calls * OUTPUT_TOKENS_PER_CALL
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
        todo: list[Work] = self.work()[: self.limit]
        written: int
        failed: int
        written, failed = asyncio.run(self._run(todo))
        if failed:
            raise RuntimeError(f"{failed} of {len(todo)} candidates failed; {written} written, rerun to redo the rest")
        return {"work": len(todo), "facts_written": written, "failed": 0}

    async def _run(self, todo: list[Work]) -> tuple[int, int]:
        """All candidates in flight at once (the client holds the concurrency limit); each row is
        committed as its candidate finishes, so a stopped run keeps what it paid for."""
        async with OpenAIResponsesCaller(self.config) as caller:
            tasks: list[asyncio.Task[tuple[Work, SynthesizedFacts | None]]] = []
            for item in todo:
                tasks.append(asyncio.create_task(self._guarded(caller, item)))
            written: int = 0
            failed: int = 0
            for task in asyncio.as_completed(tasks):
                item, facts = await task
                if facts is None:
                    failed += 1
                    continue
                queries.upsert_facts(self.conn, item.candidate_id, json.dumps(facts.to_payload(), ensure_ascii=False),
                                     item.fingerprint, self.config.model, self.config.effort, now_iso())
                self.conn.commit()
                written += 1
            return written, failed

    async def _guarded(self, caller: OpenAIResponsesCaller, item: Work) -> tuple[Work, SynthesizedFacts | None]:
        """One candidate's failure is that candidate's: print it, write nothing, let the others finish."""
        try:
            return item, await self._facts(caller, item)
        except Exception as exc:
            print(f"failed one candidate: {type(exc).__name__}: {exc}")  # ids are emails; never printed
            return item, None

    async def _facts(self, caller: OpenAIResponsesCaller, item: Work) -> SynthesizedFacts:
        """One strict-schema call per batch, all at once. One batch: its answer is the facts.
        Several: the answers are collapsed into one facts object."""
        calls: list[Awaitable[dict[str, object]]] = []
        for prompt in item.prompts:
            calls.append(caller.call(system_prompt=self.prompt, user_prompt=prompt, schema=prompting.FACT_SCHEMA,
                                     schema_name="person_facts", context="synthesize"))
        responses: list[dict[str, object]] = await asyncio.gather(*calls)
        chunks: list[SynthesizedFacts] = []
        for payload in responses:
            chunks.append(SynthesizedFacts.from_payload(payload))
        if len(chunks) == 1:
            return chunks[0]
        return collapse(chunks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="03 Synthesize: facts per candidate (gpt-6-luna).")
    parser.add_argument("--data-root", type=Path, default=Path(".powerpacks"))
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="synthesize only the first N pending candidates")
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
