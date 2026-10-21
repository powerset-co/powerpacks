"""Filtered FTS and streamed cosine search, reciprocal-rank fusion and tenure overlap."""
from __future__ import annotations

import heapq
import json
import math
import re
import sqlite3
from dataclasses import asdict, dataclass

from packs.memory.generate import embedded
from packs.memory.models import Citation, Claim, Filters, Hit, People, PersonHit, Provider, normalized, cached_vector
from packs.memory.store import Store, _SAME_CLAIM

SHORTLIST = 100
MAX_CONTEXT_CHARS = 16000
# Below this cosine a claim is not about the words typed; it never ranks a person on its own.
# Measured on Arthur's store: matching claims sit at 0.43-0.60, unrelated ones at 0.36-0.42.
VECTOR_FLOOR = 0.40
# A held label counts as much as a top-ranked matching claim in both lists.
LAYER_SCORE = 0.03
# A person's score is their best few distinct claims, so twenty location lines never outrank one exact answer.
CLAIMS_PER_PERSON = 3
# Words that carry no meaning of their own: "my family" is the family layer, not a filter on "my".
STOP_WORDS = frozenset("a an and are at by for from how i in is me my of on or our s that the this to was were what which who whom with".split())
# Words people type for the labels Deep Context saves (packs/memory/sources.py LABELS and relationship kinds).
LAYER_WORDS = {
    "wife": ("romantic partner",), "husband": ("romantic partner",), "spouse": ("romantic partner",),
    "partner": ("romantic partner",), "girlfriend": ("romantic partner",), "boyfriend": ("romantic partner",),
    "significant other": ("romantic partner",), "close friend": ("close friend",), "close friends": ("close friend",),
    "best friend": ("close friend",), "best friends": ("close friend",),
    "family": ("family",), "relatives": ("family",), "mom": ("family",),
    "mother": ("family",), "dad": ("family",), "father": ("family",), "parents": ("family",), "sibling": ("family",),
    "siblings": ("family",), "brother": ("family",), "sister": ("family",), "friend": ("close friend",),
    "friends": ("close friend",), "coworker": ("past coworker", "current coworker"),
    "coworkers": ("past coworker", "current coworker"), "co-worker": ("past coworker", "current coworker"),
    "co-workers": ("past coworker", "current coworker"), "colleague": ("past coworker", "current coworker"),
    "colleagues": ("past coworker", "current coworker"), "classmate": ("classmate",), "classmates": ("classmate",),
    "investor": ("investor",), "investors": ("investor",), "recruiter": ("recruiter",), "recruiters": ("recruiter",),
    "founder": ("founder",), "founders": ("founder",), "mentor": ("mentor or advisor",), "mentors": ("mentor or advisor",),
    "advisor": ("mentor or advisor",), "advisors": ("mentor or advisor",),
}


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

    def people(self, text: str, *, layer: str = "", limit: int = 50) -> People:
        """Everyone the words name, warmest first, with the claims that matched.

        Words that name an owner label (family, wife, coworkers …) select that layer. Alone, a layer lists every
        person holding it, the words still ordering them ("mother" puts Mother first among family). With other
        words, it filters: "roblox coworkers" is coworkers ranked by Roblox evidence. Otherwise a person ranks on a
        lexical hit or a cosine hit above VECTOR_FLOOR; nothing matching is an empty answer."""
        if not 1 <= limit <= 500 or len(text) > 9000:
            raise ValueError("search exceeds bounded result/input limits")
        words = normalized(text)
        layers = set(filter(None, (normalized(layer),)))
        held = {row[0] for row in self.store.conn.execute(
            "SELECT DISTINCT topic FROM active_claims WHERE kind='tag' AND scope='owner' AND chunk_id IS NULL "
            "AND (source_key LIKE 'labels:%' OR source_key LIKE 'facts:%')")}
        rest = words
        for phrase, targets in sorted({**{name: (name,) for name in held}, **LAYER_WORDS}.items(), key=lambda item: -len(item[0])):
            if re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", rest):
                layers.update(target for target in targets if target in held)
                rest = re.sub(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", " ", rest)
        meaningful = [token for token in re.findall(r"\w+", rest, re.UNICODE) if token not in STOP_WORDS]
        filtered = bool(layers) and bool(meaningful)
        tokens = meaningful if filtered else [token for token in re.findall(r"\w+", words, re.UNICODE) if token not in STOP_WORDS]
        by_layer: dict[str, set[str]] = {}
        if layers:
            marks = ",".join("?" for _ in layers)
            for row in self.store.conn.execute(
                    "SELECT DISTINCT parent,topic FROM active_claims WHERE kind='tag' AND scope='owner' AND chunk_id IS NULL "
                    "AND topic IN (" + marks + ")", tuple(sorted(layers))):
                by_layer.setdefault(row["parent"], set()).add(row["topic"])
        lexical: dict[int, int] = {}
        if tokens:
            # What was said and what it is about; never the scope or object columns ("owner" is not a word someone typed).
            match = "{text topic}: (" + " OR ".join('"' + token + '"' for token in tokens[:64]) + ")"
            for row in self.store.conn.execute(
                    "SELECT " + _first("c") + " AS id FROM lexical JOIN active_claims c ON c.id=lexical.rowid "
                    "WHERE lexical MATCH ? ORDER BY bm25(lexical),c.id", (match,)):
                if row["id"] not in lexical:
                    lexical[row["id"]] = len(lexical) + 1
                    if len(lexical) == SHORTLIST:
                        break
        nearest: list[tuple[float, int]] = []
        missing = 0
        if tokens and self.provider.embedding_key:
            wanted = embedded(self.store, self.provider, text)
            for row in self.store.conn.execute(
                    "SELECT c.id,e.vector FROM active_claims c LEFT JOIN embeddings e ON e.model=? AND e.hash=c.document_hash "
                    "WHERE c.id=" + _first("c"), (self.provider.embedding_key,)):
                if row["vector"] is None:
                    missing += 1
                    continue
                similarity = math.sumprod(wanted, cached_vector(row["vector"]))
                if similarity < VECTOR_FLOOR:
                    continue
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
        # One score per distinct claim text per person: four merged "Mother" rows count once.
        best: dict[str, dict[str, tuple[float, int]]] = {}
        for claim_id, score in scores.items():
            row = self.store.conn.execute("SELECT parent,text FROM active_claims WHERE id=?", (claim_id,)).fetchone()
            texts = best.setdefault(row["parent"], {})
            key = normalized(row["text"])
            if key not in texts or score > texts[key][0]:
                texts[key] = (score, claim_id)
        if filtered:
            parents = set(best) & set(by_layer)
        elif layers:
            parents = set(by_layer)
        else:
            parents = set(best)
        warmth = {row[0]: float(row[1]) for row in self.store.conn.execute(
            "SELECT i.parent,max(json_extract(r.content,'$.warmth')) FROM sources s "
            "JOIN revisions r ON r.source_id=s.id AND r.hash=s.hash JOIN identities i USING(namespace,member) "
            "WHERE s.key LIKE 'labels:%' AND s.present=1 AND i.present=1 GROUP BY i.parent")}
        ranked: list[tuple[float, str, float]] = []
        for parent in parents:
            top = sorted((entry[0] for entry in best.get(parent, {}).values()), reverse=True)[:CLAIMS_PER_PERSON]
            score = sum(top) + LAYER_SCORE * len(by_layer.get(parent, ()))
            ranked.append((score * (1 + warmth.get(parent, 0.0) / 10), parent, score))
        ranked.sort(key=lambda entry: (-entry[0], entry[1]))
        # People carry the native parent id the graph and the People page use, without the corpus prefix.
        prefix = self.store.conn.execute("SELECT namespace FROM identities LIMIT 1").fetchone()[0] + "#"
        people = []
        for final, parent, score in ranked[:limit]:
            # The claims that matched the words first, then the labels the layer selected on.
            evidence: dict[int, Hit] = {}
            for claim_score, claim_id in sorted(best.get(parent, {}).values(), reverse=True)[:CLAIMS_PER_PERSON]:
                row = self.store.conn.execute("SELECT * FROM active_claims WHERE id=?", (claim_id,)).fetchone()
                evidence[claim_id] = self._hit(row, claim_score, lexical.get(claim_id), vectors.get(claim_id))
            for label in sorted(by_layer.get(parent, ())):
                row = self.store.conn.execute(
                    "SELECT * FROM active_claims WHERE parent=? AND kind='tag' AND scope='owner' AND chunk_id IS NULL AND topic=? "
                    "ORDER BY id LIMIT 1", (parent, label)).fetchone()
                evidence.setdefault(row["id"], self._hit(row, LAYER_SCORE, None, None))
            name = self.store.conn.execute("SELECT min(name) FROM identities WHERE parent=? AND present=1", (parent,)).fetchone()[0]
            members = tuple(row[0] for row in self.store.conn.execute(
                "SELECT member FROM identities WHERE parent=? AND present=1 ORDER BY member", (parent,)))
            people.append(PersonHit(parent[len(prefix):], name, warmth.get(parent, 0.0), final, tuple(evidence.values()), members))
        return People(tuple(people), tuple(sorted(layers)), filtered, len(ranked), len(lexical), len(vectors), missing)

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
