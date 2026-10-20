"""Filtered FTS and streamed cosine search, reciprocal-rank fusion and tenure overlap."""
from __future__ import annotations

import heapq
import json
import math
import re
import sqlite3
from dataclasses import asdict, dataclass

from packs.memory.generate import embedded
from packs.memory.models import Citation, Claim, Filters, Hit, Provider, normalized, cached_vector
from packs.memory.store import Store, _SAME_CLAIM

SHORTLIST = 100
MAX_CONTEXT_CHARS = 16000


def _first(alias: str, where: str = "1=1") -> str:
    return ("(SELECT min(old.id) FROM active_claims old WHERE old.parent=" + alias + ".parent AND "
            + _SAME_CLAIM.format(claim=alias) + " AND " + where.replace("c.", "old.") + ")")


def _where(store: Store, filters: Filters) -> tuple[str, list[str]]:
    clauses = ["1=1"]
    params: list[str] = []
    if filters.parents:
        clauses.append("c.parent IN (" + ",".join("?" for _ in filters.parents) + ")")
        params.extend(filters.parents)
    for name, value in (("kind", filters.kind), ("topic", filters.topic), ("scope", filters.scope), ("object", filters.object)):
        if value:
            if name == "topic":
                clauses.append("c.topic IN (SELECT ? UNION SELECT alias FROM aliases WHERE topic=? AND confirmed=1)")
                params.extend([store.topic(value)] * 2)
            else:
                clauses.append("c." + name + "=?")
                params.append(normalized(value))
    return " AND ".join(clauses), params


@dataclass(frozen=True)
class QueryResult:
    hits: tuple[Hit, ...]
    lexical_candidates: int
    vector_candidates: int
    missing_embeddings: int
    truncated: bool


class Query:
    def __init__(self, store: Store, provider: Provider) -> None:
        self.store = store
        self.provider = provider

    def run(self, text: str, *, filters: Filters = Filters(), limit: int = 10,
            context_chars: int = MAX_CONTEXT_CHARS) -> QueryResult:
        if not 1 <= limit <= SHORTLIST or not 1 <= context_chars <= MAX_CONTEXT_CHARS or len(text) > 9000:
            raise ValueError("query exceeds bounded result/input limits")
        where, params = _where(self.store, filters)
        lexical: dict[int, int] = {}
        tokens = re.findall(r"\w+", text, re.UNICODE)
        if tokens:
            match = " OR ".join('"' + token + '"' for token in tokens[:64])
            rows = self.store.conn.execute(
                "SELECT " + _first("c", where) + " AS id FROM lexical JOIN active_claims c ON c.id=lexical.rowid "
                "WHERE lexical MATCH ? AND " + where + " ORDER BY bm25(lexical),c.id", (*params, match, *params))
            for row in rows:
                if row["id"] in lexical:
                    continue
                lexical[row["id"]] = len(lexical) + 1
                if len(lexical) == SHORTLIST:
                    break
        nearest: list[tuple[float, int]] = []
        missing = 0
        if text.strip() and self.provider.embedding_key:
            wanted = embedded(self.store, self.provider, text)
            # One pass over the filtered claims; each row carries its vector or NULL.
            rows = self.store.conn.execute(
                "SELECT c.id,e.vector FROM active_claims c LEFT JOIN embeddings e ON e.model=? AND e.hash=c.document_hash "
                "WHERE " + where + " AND c.id=" + _first("c", where), (self.provider.embedding_key, *params, *params))
            for row in rows:
                if row["vector"] is None:
                    missing += 1
                    continue
                values = cached_vector(row["vector"])
                if len(values) != len(wanted):
                    raise ValueError("embedding dimension mismatch for the same model")
                similarity = math.sumprod(wanted, values)
                entry = (similarity, -row["id"])
                if len(nearest) < SHORTLIST:
                    heapq.heappush(nearest, entry)
                elif entry > nearest[0]:
                    heapq.heapreplace(nearest, entry)
        vectors = {-entry[1]: rank for rank, entry in enumerate(sorted(nearest, reverse=True), 1)}
        scores: dict[int, float] = {}
        for ranks in (lexical, vectors):
            for claim_id, rank in ranks.items():
                scores[claim_id] = scores.get(claim_id, 0.0) + 1 / (60 + rank)
        if not text.strip():
            for row in self.store.conn.execute("SELECT c.id FROM active_claims c WHERE " + where + " AND c.id=" +
                                               _first("c", where) + " ORDER BY c.id LIMIT ?", (*params, *params, limit)):
                scores[row[0]] = 0.0
        hits: list[Hit] = []
        used = 0
        truncated = False
        for claim_id in sorted(scores, key=lambda key: (-scores[key], key)):
            row = self.store.conn.execute("SELECT * FROM active_claims WHERE id=?", (claim_id,)).fetchone()
            hit = self._hit(row, scores[claim_id], lexical.get(claim_id), vectors.get(claim_id))
            size = len(json.dumps(asdict(hit), ensure_ascii=False))
            if used + size > context_chars or len(hits) == limit:
                truncated = True
                break
            hits.append(hit)
            used += size
        return QueryResult(tuple(hits), len(lexical), len(vectors), missing, truncated)

    def _hit(self, row: sqlite3.Row, score: float, lexical: int | None, semantic: int | None) -> Hit:
        claim = Claim.parse(json.loads(row["payload"]))
        citations: list[Citation] = []
        for item in self.store.conn.execute(
            "SELECT c.citation,c.source_hash,c.payload,c.error,c.human,c.chunk_id,c.kind,c.source_key FROM active_claims c JOIN claims old ON old.id=? "
            "WHERE c.parent=? AND " + _SAME_CLAIM.format(claim="c") + " ORDER BY c.id LIMIT 8",
            (row["id"], row["parent"])):
            evidence = Claim.parse(json.loads(item["payload"]))
            level = "human correction" if item["human"] else "derived artifact; original message citation unverified"
            if item['kind'] == 'tag' and not item['human']:
                level = ('machine-inferred tag; ' + level) if item['chunk_id'] is not None else (
                    'original human tag' if item['source_key'].startswith('tags:') else level)
            if item["error"]:
                level += "; latest source read failed: " + item["error"]
            citation = Citation(item["citation"], item["source_hash"], evidence.quote, level)
            if citation not in citations:
                citations.append(citation)
        return Hit(row["id"], row["parent"], row["name"], claim, score, lexical, semantic, tuple(citations))

    def overlap(self, parent: str, *, organization: str = "", limit: int = 20) -> tuple[tuple[Hit, Hit], ...]:
        if not 1 <= limit <= SHORTLIST:
            raise ValueError("overlap limit outside 1..100")
        rows = self.store.conn.execute(
            "SELECT a.id,b.id FROM active_claims a JOIN active_claims b ON a.object=b.object AND a.parent<>b.parent "
            "WHERE a.parent=? AND a.kind='tenure' AND b.kind='tenure' AND a.object<>'' "
            "AND a.start<>'' AND b.start<>'' AND a.end<>'' AND b.end<>'' AND date_lower(a.start)<=date_upper(b.end) AND date_lower(b.start)<=date_upper(a.end) "
            "AND (?='' OR a.object=?) AND a.id=" + _first("a") +
            " AND b.id=" + _first("b") + " ORDER BY a.id,b.id LIMIT ?",
            (parent, normalized(organization), normalized(organization), limit))
        found = []
        for pair in rows:
            left = self.store.conn.execute("SELECT * FROM active_claims WHERE id=?", (pair[0],)).fetchone()
            right = self.store.conn.execute("SELECT * FROM active_claims WHERE id=?", (pair[1],)).fetchone()
            found.append((self._hit(left, 0, None, None), self._hit(right, 0, None, None)))
        return tuple(found)
