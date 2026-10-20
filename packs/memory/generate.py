"""Serial generation and cached embedding checkpoints; memory stays bounded to one chunk."""
from __future__ import annotations

import json
from dataclasses import dataclass

import tiktoken

from packs.ingestion.primitives.common.jsonio import short_hash
from packs.memory.models import Claim, Provider, cached_vector, document, packed
from packs.memory.provider import CHUNK_CHARS, MAX_CLAIMS, MAX_OUTPUT_TOKENS, SYSTEM, parse_response
from packs.memory.store import Store


def embedded(store: Store, provider: Provider, text: str) -> tuple[float, ...]:
    key = short_hash(text, n=64)
    row = store.conn.execute("SELECT vector FROM embeddings WHERE model=? AND hash=?", (provider.embedding_key, key)).fetchone()
    if row is not None:
        return tuple(cached_vector(row[0]))
    result = provider.embed(text)
    with store.conn:
        store.conn.execute("INSERT INTO embeddings VALUES(?,?,?)", (provider.embedding_key, key, packed(result)))
    return tuple(cached_vector(packed(result)))


@dataclass
class GenerationReport:
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
        if limit < 1:
            raise ValueError("limit must be positive")
        report = GenerationReport()
        for source in self.store.sources():
            for position, offset in enumerate(range(0, len(source.content), CHUNK_CHARS)):
                text = source.content[offset:offset + CHUNK_CHARS]
                fingerprint = short_hash(self.provider.generation_key + text, n=64)
                old = self.store.conn.execute(
                    "SELECT response FROM chunks WHERE source_id=? AND fingerprint=? ORDER BY id LIMIT 1",
                    (source.id, fingerprint)).fetchone()
                if old is None:
                    if report.generation_calls + report.embedding_calls >= limit:
                        return report
                    response = self.provider.generate(text)
                    report.generation_calls += 1
                else:
                    response = old[0]
                    report.reused_chunks += 1
                # Save the paid response before parsing: malformed/incomplete output is never re-bought.
                with self.store.conn:
                    self.store.conn.execute(
                        "INSERT OR IGNORE INTO chunks(source_id,source_hash,position,fingerprint,response,error) VALUES(?,?,?,?,?,'')",
                        (source.id, source.hash, position, fingerprint, response))
                claims, errors = parse_response(response, text)
                report.exclusions += len(errors)
                with self.store.conn:
                    chunk_id = self.store.conn.execute(
                        "SELECT id FROM chunks WHERE source_id=? AND source_hash=? AND position=? AND fingerprint=?",
                        (source.id, source.hash, position, fingerprint)).fetchone()[0]
                    self.store.conn.execute("UPDATE chunks SET error=? WHERE id=?", ("; ".join(errors), chunk_id))
                    self.store.conn.execute(
                        "UPDATE claims SET active=0 WHERE human=0 AND chunk_id IN "
                        "(SELECT id FROM chunks WHERE source_id=? AND position=? AND id<>?)",
                        (source.id, position, chunk_id))
                    for claim in claims:
                        # The saved relationship_to_owner field already owns this evidence.
                        projected = self.store.conn.execute(
                            "SELECT 1 FROM claims WHERE source_id=? AND chunk_id IS NULL AND kind='relationship' "
                            "AND json_extract(payload,'$.quote')=?",
                            (source.id, claim.quote)).fetchone() if claim.kind == "relationship" else None
                        if projected:
                            continue
                        self.store.add_claim(source.id, source.hash, chunk_id, claim, human=False)
                        report.claims += 1
                self._embeddings(report, limit, source_id=source.id)
        self._embeddings(report, limit)
        return report

    def _embeddings(self, report: GenerationReport, limit: int, *, source_id: int | None = None) -> None:
        where = " WHERE source_id=?" if source_id is not None else ""
        params = (source_id,) if source_id is not None else ()
        for row in self.store.conn.execute("SELECT DISTINCT payload FROM active_claims" + where, params):
            if report.generation_calls + report.embedding_calls >= limit:
                return
            text = document(Claim.parse(json.loads(row[0])))
            exists = self.store.conn.execute("SELECT 1 FROM embeddings WHERE model=? AND hash=?",
                                             (self.provider.embedding_key, short_hash(text, n=64))).fetchone()
            if exists is None:
                embedded(self.store, self.provider, text)
                report.embedding_calls += 1

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
