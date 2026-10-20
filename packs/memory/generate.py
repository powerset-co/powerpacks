"""Bounded, replenished generation requests; SQLite checkpoints stay on the calling thread."""
from __future__ import annotations

import json
from dataclasses import dataclass
from collections.abc import Generator, Iterator
from concurrent.futures import Future, ThreadPoolExecutor, as_completed

import tiktoken

from packs.indexing.lib.openai_usage_tiers import env_or_profile_int
from packs.ingestion.primitives.common.jsonio import short_hash
from packs.memory.models import Claim, Provider, cached_vector, document, packed
from packs.memory.provider import CHUNK_CHARS, MAX_CLAIMS, MAX_OUTPUT_TOKENS, SYSTEM, NeedsApproval, SpendReached, parse_response
from packs.memory.store import Store
from packs.memory.progress import Progress, Step, consume, retry_first


GENERATION_WORKERS = 64


def embedded(store: Store, provider: Provider, text: str) -> tuple[float, ...]:
    key = short_hash(text, n=64)
    row = store.conn.execute("SELECT vector FROM embeddings WHERE model=? AND hash=?", (provider.embedding_key, key)).fetchone()
    if row is not None:
        return tuple(cached_vector(row[0]))
    result = provider.embed(text)
    with store.conn:
        store.conn.execute("INSERT INTO embeddings VALUES(?,?,?)", (provider.embedding_key, key, packed(result)))
    return tuple(cached_vector(packed(result)))


@dataclass(frozen=True)
class _Chunk:
    source_id: int
    source_hash: str
    position: int
    text: str
    fingerprint: str


@dataclass
class GenerationReport:
    failures: tuple[tuple[str, str], ...] = ()
    generation_calls: int = 0
    embedding_calls: int = 0
    reused_chunks: int = 0
    claims: int = 0
    exclusions: int = 0


class Generate:
    def __init__(self, store: Store, provider: Provider) -> None:
        self.store = store
        self.provider = provider

    def run(self, *, limit: int) -> GenerationReport:
        return consume(self.progress(limit=limit))

    def progress(self, *, limit: int, first: tuple[str, ...] = ()) -> Generator[Progress, None, GenerationReport]:
        report = GenerationReport()
        for event in self.chunks(limit=limit, first=first, report=report):
            yield event
            if event.key and not event.error:
                yield from self.index(limit=limit, report=report, source_id=int(event.key.split(":")[0]))
        yield from self.index(limit=limit, report=report)
        return report

    def chunks(self, *, limit: int, first: tuple[str, ...] = (), report: GenerationReport | None = None) -> Generator[Progress, None, GenerationReport]:
        if limit < 1:
            raise ValueError("limit must be positive")
        report = report if report is not None else GenerationReport()
        total = sum((len(source.content) + CHUNK_CHARS - 1) // CHUNK_CHARS for source in self.store.sources())
        done = 0
        inputs = self._inputs(first)
        concurrency = min(GENERATION_WORKERS, env_or_profile_int(
            "POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency", fallback=GENERATION_WORKERS))
        pending: dict[Future[str], list[_Chunk]] = {}
        stopped = False
        terminal: BaseException | None = None
        with ThreadPoolExecutor(max_workers=concurrency) as workers:
            while True:
                events: list[tuple[str, str]] = []
                try:
                    if terminal is None and not stopped:
                        slots = concurrency - sum(len(items) for items in pending.values())
                        for _ in range(slots):
                            item = next(inputs, None)
                            if item is None:
                                stopped = True
                                break
                            old = self.store.conn.execute(
                                "SELECT response FROM chunks WHERE source_id=? AND fingerprint=? ORDER BY id LIMIT 1",
                                (item.source_id, item.fingerprint)).fetchone()
                            if old is not None:
                                report.reused_chunks += 1
                                error = self._save(item, old[0], report)
                                key = f"{item.source_id}:{item.position}"
                                if pending:
                                    events.append((key, error))
                                else:
                                    done += 1
                                    yield Progress(Step.GENERATE, done, total, key, error)
                                continue
                            existing = next((items for items in pending.values() if items[0].source_id == item.source_id
                                             and items[0].fingerprint == item.fingerprint), None)
                            if existing is not None:
                                existing.append(item)
                                continue
                            if report.generation_calls + report.embedding_calls >= limit:
                                stopped = True
                                break
                            report.generation_calls += 1
                            pending[workers.submit(self.provider.generate, item.text)] = [item]
                    while pending:
                        ready = [future for future in pending if future.done()]
                        if not ready:
                            if events:
                                break
                            ready = [next(as_completed(pending))]
                        for future in ready:
                            items = pending[future]
                            try:
                                response = future.result()
                            except (SpendReached, NeedsApproval) as exc:
                                terminal = terminal or exc
                            except Exception as exc:
                                events.extend((f"{item.source_id}:{item.position}", str(exc)) for item in items)
                            except BaseException as exc:
                                terminal = terminal or exc
                                if not future.done() or future.exception() is not exc:
                                    continue
                            else:
                                for index, item in enumerate(items):
                                    try:
                                        error = self._save(item, response, report)
                                        report.reused_chunks += int(index > 0)
                                        events.append((f"{item.source_id}:{item.position}", error))
                                    except Exception as exc:
                                        events.append((f"{item.source_id}:{item.position}", str(exc)))
                            pending.pop(future)
                    for key, error in events:
                        done += 1
                        if terminal is None:
                            yield Progress(Step.GENERATE, done, total, key, error)
                except BaseException as exc:
                    terminal = terminal or exc
                # Closing progress, cancellation and spend errors still drain every paid request.
                if pending:
                    continue
                if terminal is not None:
                    if isinstance(terminal, SpendReached):
                        yield Progress(Step.GENERATE, done, total, "")
                    raise terminal
                if stopped:
                    return report

    def _inputs(self, first: tuple[str, ...]) -> Iterator[_Chunk]:
        first_sources = tuple(key.split(":")[0] for key in first)
        for source in retry_first(self.store.sources, lambda source: str(source.id), first_sources):
            positions = list(enumerate(range(0, len(source.content), CHUNK_CHARS)))
            positions.sort(key=lambda item: f"{source.id}:{item[0]}" not in first)
            for position, offset in positions:
                text = source.content[offset:offset + CHUNK_CHARS]
                fingerprint = short_hash(self.provider.generation_key + text, n=64)
                yield _Chunk(source.id, source.hash, position, text, fingerprint)

    def _save(self, chunk: _Chunk, response: str, report: GenerationReport) -> str:
        # Save the paid response before parsing: malformed/incomplete output is never re-bought.
        with self.store.conn:
            self.store.conn.execute(
                "INSERT OR IGNORE INTO chunks(source_id,source_hash,position,fingerprint,response,error) VALUES(?,?,?,?,?,'')",
                (chunk.source_id, chunk.source_hash, chunk.position, chunk.fingerprint, response))
        claims, errors = parse_response(response, chunk.text)
        report.exclusions += len(errors)
        with self.store.conn:
            chunk_id = self.store.conn.execute(
                "SELECT id FROM chunks WHERE source_id=? AND source_hash=? AND position=? AND fingerprint=?",
                (chunk.source_id, chunk.source_hash, chunk.position, chunk.fingerprint)).fetchone()[0]
            self.store.conn.execute("UPDATE chunks SET error=? WHERE id=?", ("; ".join(errors), chunk_id))
            self.store.conn.execute(
                "UPDATE claims SET active=0 WHERE human=0 AND chunk_id IN "
                "(SELECT id FROM chunks WHERE source_id=? AND position=? AND id<>?)",
                (chunk.source_id, chunk.position, chunk_id))
            for claim in claims:
                # The saved relationship_to_owner field already owns this evidence.
                projected = self.store.conn.execute(
                    "SELECT 1 FROM claims WHERE source_id=? AND chunk_id IS NULL AND kind='relationship' "
                    "AND json_extract(payload,'$.quote')=?",
                    (chunk.source_id, claim.quote)).fetchone() if claim.kind == "relationship" else None
                if projected:
                    continue
                self.store.add_claim(chunk.source_id, chunk.source_hash, chunk_id, claim, human=False)
                report.claims += 1
        return "; ".join(error for error in errors if error.startswith("invalid or incomplete generation"))

    def index(self, *, limit: int, report: GenerationReport | None = None, first: tuple[str, ...] = (), source_id: int | None = None) -> Generator[Progress, None, GenerationReport]:
        report = report if report is not None else GenerationReport()
        where = " WHERE source_id=?" if source_id is not None else ""
        params = (source_id,) if source_id is not None else ()
        total = self.store.conn.execute("SELECT count(DISTINCT payload) FROM active_claims" + where, params).fetchone()[0]
        rows = self.store.conn.execute(
            "SELECT DISTINCT payload,document_hash FROM active_claims" + where
            + " ORDER BY document_hash IN (SELECT value FROM json_each(?)) DESC", (*params, json.dumps(first)))
        for done, row in enumerate(rows, 1):
            text = document(Claim.parse(json.loads(row[0])))
            key = row["document_hash"]
            try:
                exists = self.store.conn.execute("SELECT 1 FROM embeddings WHERE model=? AND hash=?",
                                                 (self.provider.embedding_key, key)).fetchone()
                if exists is None:
                    if report.generation_calls + report.embedding_calls >= limit:
                        return report
                    report.embedding_calls += 1
                    embedded(self.store, self.provider, text)
                yield Progress(Step.INDEX, done, total, key)
            except SpendReached:
                yield Progress(Step.INDEX, done - 1, total, "")
                raise
            except NeedsApproval:
                raise
            except Exception as exc:
                yield Progress(Step.INDEX, done, total, key, str(exc))
        return report

    def estimate(self, *, limit: int) -> dict[str, object]:
        encoder = tiktoken.get_encoding("o200k_base")
        pending = 0
        tokens = 0
        total = 0
        for source in self.store.sources():
            for offset in range(0, len(source.content), CHUNK_CHARS):
                text = source.content[offset:offset + CHUNK_CHARS]
                total += 1
                fingerprint = short_hash(self.provider.generation_key + text, n=64)
                cached = self.store.conn.execute("SELECT 1 FROM chunks WHERE source_id=? AND fingerprint=?",
                                                 (source.id, fingerprint)).fetchone()
                if cached is None:
                    pending += 1
                    if pending <= limit:
                        tokens += len(encoder.encode(SYSTEM + text))
        missing_embeddings = 0
        embedding_tokens = 0
        for row in self.store.conn.execute("SELECT payload FROM active_claims"):
            text = document(Claim.parse(json.loads(row[0])))
            if not self.store.conn.execute("SELECT 1 FROM embeddings WHERE model=? AND hash=?",
                                           (self.provider.embedding_key, short_hash(text, n=64))).fetchone():
                missing_embeddings += 1
                embedding_tokens += len(encoder.encode(text))
        return {"chunks": total, "pending_chunks": pending, "limit": limit, "generation_input_tokens": tokens,
                "generation_output_token_cap": min(limit, pending) * MAX_OUTPUT_TOKENS,
                "cached_document_embedding_misses": missing_embeddings, "embedding_input_tokens": embedding_tokens,
                "new_document_embedding_calls_cap": min(limit, missing_embeddings + min(limit, pending) * MAX_CLAIMS),
                "total_fresh_calls_cap": limit,
                "generation_key": self.provider.generation_key, "embedding_model": self.provider.embedding_key,
                "pricing": "apply approved per-token prices; new claim embedding tokens unknown until generation"}
