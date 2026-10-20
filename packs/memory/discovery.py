"""Incremental cross-person interpretation using original evidence and cached retrieval."""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass

from packs.ingestion.primitives.common.jsonio import short_hash
from packs.memory.discovery_provider import Decision, DiscoveryProvider, INPUT_CHARS, OUTPUT_TOKENS, PEOPLE, SYSTEM, Tag, Outcome
from packs.memory.models import Claim, Kind, IdentityMatch, cached_vector, document, normalized
from packs.memory.store import Store

_ORIGINAL = "c.chunk_id IS NULL AND c.human=0"
_VALUE = "normalized(CASE WHEN c.object<>'' THEN c.object ELSE c.text END)"
NEIGHBORS = 8
MIN_SIMILARITY = 0.35  # Pilot within-concept p25=.390; this rejects remote candidates, not false claims.


@dataclass(frozen=True)
class Group:
    key: str
    phrase: str
    people: int
    seed: int = 0


@dataclass(frozen=True)
class Person:
    parent: str
    claim_id: int = 0


@dataclass(frozen=True)
class Evidence:
    parent: str
    source_id: int
    source_hash: str
    quote: str


@dataclass(frozen=True)
class Insight:
    candidate: str
    decision: Outcome
    reason: str
    members: tuple[IdentityMatch, ...]
    tags: tuple[Tag, ...]


@dataclass
class DiscoveryReport:
    groups: int = 0
    batches: int = 0
    calls: int = 0
    reused: int = 0
    tags: int = 0
    abstentions: int = 0
    exclusions: int = 0
    max_input_chars: int = 0
    next_after: str = ""
    has_more: bool = False
    insights: tuple[Insight, ...] = ()


@dataclass(frozen=True)
class Preview:
    groups: tuple[Group, ...]
    batches: int
    pending_calls: int
    max_input_chars: int
    input_chars_cap: int
    output_tokens_per_call_cap: int
    instruction_chars: int
    fresh_calls_cap: int
    next_after: str
    has_more: bool
    insights: tuple[Insight, ...] = ()
    semantic_retrieval: str = 'cached approximate neighbors; no new embeddings'


class Discover:
    def __init__(self, store: Store, provider: DiscoveryProvider) -> None:
        self.store, self.provider = store, provider
        self.next_after = ""

    def _index(self) -> None:
        conn = self.store.conn
        conn.executescript("DROP TABLE IF EXISTS temp.discovery_vectors; CREATE TEMP TABLE discovery_vectors("
                           "claim_id INTEGER,band INTEGER,value TEXT,PRIMARY KEY(claim_id,band)); "
                           "CREATE INDEX temp.discovery_vector_values ON discovery_vectors(band,value);")
        conn.executescript("DROP TABLE IF EXISTS temp.discovery_lexical; "
                           "CREATE VIRTUAL TABLE temp.discovery_lexical USING fts5(text,topic,scope,object);")
        conn.execute('INSERT INTO discovery_lexical(rowid,text,topic,scope,object) '
                     'SELECT c.id,c.text,c.topic,c.scope,c.object FROM active_claims c WHERE ' + _ORIGINAL)
        conn.executescript("DROP TABLE IF EXISTS temp.discovery_words; "
                           "CREATE VIRTUAL TABLE temp.discovery_words USING fts5vocab('temp','discovery_lexical','row');")
        if not self.provider.embedding_key:
            return
        for row in conn.execute("SELECT c.id,e.vector FROM active_claims c JOIN embeddings e ON e.model=? AND e.hash=c.document_hash "
                                "WHERE " + _ORIGINAL, (self.provider.embedding_key,)):
            vector = cached_vector(row['vector'])
            for band in range(min(4, len(vector))):
                value = ''.join('1' if number > 0 else '0' for number in vector[band::4][:8])
                conn.execute('INSERT INTO discovery_vectors VALUES(?,?,?)', (row['id'], band, value))

    def _vector(self, payload: str) -> tuple[float, ...] | None:
        key = short_hash(document(Claim.parse(json.loads(payload))), n=64)
        row = self.store.conn.execute('SELECT vector FROM embeddings WHERE model=? AND hash=?',
                                      (self.provider.embedding_key, key)).fetchone()
        return tuple(cached_vector(row[0])) if row else None

    def _lexical(self, phrase: str) -> Iterator[Person]:
        tokens = sorted({word for word in re.findall(r'\w+', phrase) if len(word) >= 3 and not word.isnumeric()})
        if not tokens:
            return
        conn = self.store.conn
        common = max(3, conn.execute('SELECT count(*) FROM discovery_lexical').fetchone()[0] // 10)
        words = [row[0] for row in conn.execute(
            'SELECT term FROM discovery_words WHERE term IN (' + ','.join('?' for _ in tokens) + ') '
            'AND doc BETWEEN 2 AND ? ORDER BY doc,term LIMIT 8', (*tokens, common))]
        if not words:
            return
        match = ' OR '.join('"' + word + '"' for word in words)
        for row in conn.execute('SELECT c.parent,c.id FROM discovery_lexical JOIN active_claims c ON c.id=discovery_lexical.rowid '
                                'WHERE discovery_lexical MATCH ? ORDER BY bm25(discovery_lexical),c.id LIMIT 16', (match,)):
            yield Person(row['parent'], row['id'])

    def _parents(self, group: Group) -> Iterator[Person]:
        conn = self.store.conn
        if not group.seed:
            for row in conn.execute(
                'SELECT DISTINCT i.parent FROM phrases p JOIN sources s ON s.id=p.source_id '
                'JOIN identities i USING(namespace,member) WHERE p.phrase=? AND s.present=1 AND i.present=1 ORDER BY i.parent',
                (group.phrase,)):
                yield Person(row[0])
            return
        people = [Person(row['parent'], row['id']) for row in conn.execute(
            'SELECT c.parent,min(c.id) id FROM active_claims c WHERE ' + _ORIGINAL + ' AND ' + _VALUE + '=? '
            'GROUP BY c.parent ORDER BY c.parent', (group.phrase,))]
        found = {person.parent for person in people}
        seed = conn.execute('SELECT payload FROM active_claims WHERE id=?', (group.seed,)).fetchone()
        wanted = self._vector(seed[0]) if self.provider.embedding_key else None
        if wanted is not None:
            probes: list[tuple[int, str, int]] = []
            for band, value in conn.execute('SELECT band,value FROM discovery_vectors WHERE claim_id=?', (group.seed,)):
                probes.append((band, value, 2))
                for position, bit in enumerate(value):
                    changed = value[:position] + ('0' if bit == '1' else '1') + value[position + 1:]
                    probes.append((band, changed, 1))
            nearest: list[tuple[float, int, str]] = []
            for row in conn.execute(
                'WITH probes(band,value,weight) AS (VALUES ' + ','.join('(?,?,?)' for _ in probes) + ') '
                'SELECT c.id,c.parent,c.payload FROM probes a JOIN discovery_vectors b '
                'ON a.band=b.band AND a.value=b.value JOIN active_claims c ON c.id=b.claim_id '
                'GROUP BY c.id ORDER BY sum(a.weight) DESC,c.id LIMIT 100', tuple(value for probe in probes for value in probe)):
                vector = self._vector(row['payload'])
                if vector is None or len(vector) != len(wanted):
                    continue
                score = sum(a * b for a, b in zip(wanted, vector))
                if score >= MIN_SIMILARITY:
                    nearest.append((score, row['id'], row['parent']))
            added = 0
            for _, claim_id, parent in sorted(nearest, key=lambda item: (-item[0], item[1])):
                if parent in found:
                    continue
                people.append(Person(parent, claim_id))
                found.add(parent)
                added += 1
                if added == NEIGHBORS:
                    break
        for person in self._lexical(group.phrase):
            if person.parent not in found:
                people.append(person)
                found.add(person.parent)
        yield from people

    def groups(self, *, min_people: int = 3, phrase: str = '', after: str = '') -> Iterator[Group]:
        if min_people < 2:
            raise ValueError('min_people must be at least 2')
        phrase = normalized(phrase)
        self._index()
        inspected = 0
        self.next_after = ''
        for row in self.store.conn.execute(
            'SELECT p.phrase,count(DISTINCT i.parent) people FROM phrases p JOIN sources s ON s.id=p.source_id '
            'JOIN identities i USING(namespace,member) WHERE s.present=1 AND i.present=1 '
            'AND (?=\'\' OR p.phrase=?) AND \'name:\'||p.phrase>? GROUP BY p.phrase HAVING count(DISTINCT i.parent)>=? ORDER BY p.phrase',
            (phrase, phrase, after, min_people)):
            self.next_after = 'name:' + row['phrase']
            inspected += 1
            yield Group(self.next_after, row['phrase'], row['people'])
            if inspected == 100:
                return
        for row in self.store.conn.execute(
            'SELECT ' + _VALUE + ' phrase,min(c.id) seed,count(DISTINCT c.parent) people '
            'FROM active_claims c WHERE ' + _ORIGINAL + " AND (?='' OR " + _VALUE + '=?) AND \'saved:\'||' + _VALUE + '>? GROUP BY phrase ORDER BY phrase',
            (phrase, phrase, after)):
            if inspected == 100:
                return
            inspected += 1
            self.next_after = 'saved:' + row['phrase']
            group = Group('saved:' + row['phrase'], row['phrase'], row['people'], row['seed'])
            count = sum(1 for _ in self._parents(group))
            if count < min_people:
                continue
            yield Group(group.key, group.phrase, count, group.seed)
        self.next_after = ''

    def _evidence(self, person: Person, phrase: str) -> tuple[Evidence, ...]:
        conn = self.store.conn
        parent = person.parent
        result: list[Evidence] = []
        for row in conn.execute(
            'SELECT s.id,s.hash,r.content FROM sources s JOIN identities i USING(namespace,member) '
            'JOIN revisions r ON r.source_id=s.id AND r.hash=s.hash '
            "WHERE i.parent=? AND i.present=1 AND s.present=1 AND s.key LIKE 'name:%' ORDER BY EXISTS(SELECT 1 FROM phrases p WHERE p.source_id=s.id AND p.phrase=?) DESC,s.id LIMIT 1", (parent, phrase)):
            result.append(Evidence(parent, row['id'], row['hash'], row['content'][-240:]))
        tokens = re.findall(r'\w+', phrase)[:32]
        match = ' OR '.join('"' + token + '"' for token in tokens)
        rows = list(conn.execute(
            'SELECT c.id,c.source_id,c.source_hash,c.payload FROM discovery_lexical JOIN active_claims c ON c.id=discovery_lexical.rowid '
            'WHERE discovery_lexical MATCH ? AND c.parent=? AND ' + _ORIGINAL + ' ORDER BY bm25(discovery_lexical),c.id LIMIT 2', (match, parent))) if match else []
        if not rows:
            rows = list(conn.execute('SELECT c.id,c.source_id,c.source_hash,c.payload FROM active_claims c WHERE c.parent=? AND '
                                    + _ORIGINAL + " ORDER BY (c.kind='relationship') DESC,c.id LIMIT 2", (parent,)))
        if person.claim_id:
            matched = conn.execute('SELECT id,source_id,source_hash,payload FROM active_claims WHERE id=?', (person.claim_id,)).fetchone()
            rows = [matched, *(row for row in rows if row['id'] != person.claim_id)]
        for row in rows[:2]:
            content = conn.execute('SELECT content FROM revisions WHERE source_id=? AND hash=?',
                                   (row['source_id'], row['source_hash'])).fetchone()[0]
            quote = Claim.parse(json.loads(row['payload'])).quote
            for saved_quote in (quote, json.dumps(quote, ensure_ascii=False)[1:-1], json.dumps(quote)[1:-1]):
                offset = content.find(saved_quote)
                if offset >= 0:
                    result.append(Evidence(parent, row['source_id'], row['source_hash'], content[offset:offset + 320]))
                    break
        return tuple(result)

    def _batch(self, group: Group, parents: list[Person]) -> tuple[str, str, tuple[Evidence, ...]]:
        conn = self.store.conn
        evidence = tuple(item for person in parents for item in self._evidence(person, group.phrase))
        labels: set[str] = set()
        dependencies: list[tuple[object, ...]] = []
        for person in parents:
            parent = person.parent
            dependencies.extend(tuple(row) for row in conn.execute(
                'SELECT s.id,s.hash,s.error,i.member,i.parent FROM sources s JOIN identities i USING(namespace,member) '
                'WHERE i.parent=? AND i.present=1 AND s.present=1 ORDER BY s.id', (parent,)))
            labels.update(row[0] for row in conn.execute(
                'SELECT DISTINCT c.topic FROM active_claims c WHERE c.parent=? AND ' + _ORIGINAL + ' ORDER BY c.topic LIMIT 12', (parent,)))
        match = ' OR '.join('"' + token + '"' for token in re.findall(r'\w+', group.phrase)[:32])
        if match:
            for row in conn.execute('SELECT c.topic,c.source_id,c.source_hash FROM discovery_lexical JOIN active_claims c ON c.id=discovery_lexical.rowid '
                                    'WHERE discovery_lexical MATCH ? AND ' + _ORIGINAL + ' ORDER BY bm25(discovery_lexical),c.id LIMIT 16', (match,)):
                labels.add(row['topic'])
                dependencies.append(tuple(row))
        aliases = [tuple(row) for row in conn.execute('SELECT alias,topic FROM aliases WHERE confirmed=1 ORDER BY alias,topic')
                   if row['alias'] in labels or row['topic'] in labels or row['alias'] == group.phrase]
        owner = conn.execute('SELECT content FROM context').fetchone()[0]
        payload: dict[str, object] = {'candidate': group.phrase, 'owner': owner[:2000], 'existing_labels': sorted(labels)[:32],
                                      'confirmed_aliases': aliases[:32], 'evidence': [{'parent': item.parent, 'source_id': item.source_id, 'quote': item.quote} for item in evidence]}
        content = json.dumps(payload, ensure_ascii=False)
        fingerprint = short_hash(self.provider.discovery_key + content + json.dumps((dependencies, owner, aliases)), n=64)
        # Inferred labels are naming context only; they never change the evidence fingerprint.
        inferred = [row[0] for row in conn.execute("SELECT topic FROM active_claims WHERE kind='tag' AND chunk_id IS NOT NULL GROUP BY topic ORDER BY max(id) DESC LIMIT 32")]
        payload['inferred_labels_for_naming_only'] = inferred
        content = json.dumps(payload, ensure_ascii=False)
        if len(content) > INPUT_CHARS:
            raise ValueError('discovery input exceeds bound')
        return content, fingerprint, evidence

    def preview(self, *, min_people: int = 3, phrase: str = '', after: str = '', limit: int = 100) -> Preview:
        if limit < 1:
            raise ValueError('limit must be positive')
        groups: list[Group] = []
        insights: list[Insight] = []
        total = 0
        pending = 0
        max_chars = 0
        previous_group = last_group = after
        for group, parents in self._batches(min_people=min_people, phrase=phrase, after=after):
            if group.key != last_group:
                previous_group, last_group = last_group, group.key
            content, fingerprint, _ = self._batch(group, parents)
            max_chars = max(max_chars, len(content))
            total += 1
            saved = self.store.conn.execute('SELECT response,error FROM chunks WHERE fingerprint=? AND position=-1', (fingerprint,)).fetchone()
            pending += saved is None
            if saved is not None and not saved['error'] and len(insights) < 4:
                try:
                    insights.append(self._insight(group, parents, Decision.parse(saved[0])))
                except (ValueError, KeyError, TypeError):
                    pass
            if len(groups) < 100:
                groups.append(group)
            if pending == limit:
                return Preview(tuple(groups), total, pending, max_chars, INPUT_CHARS, OUTPUT_TOKENS,
                               len(SYSTEM), min(limit, pending), previous_group, True, tuple(insights))
        return Preview(tuple(groups), total, pending, max_chars, INPUT_CHARS, OUTPUT_TOKENS,
                       len(SYSTEM), min(limit, pending), self.next_after, bool(self.next_after), tuple(insights))

    def _batches(self, *, min_people: int, phrase: str, after: str) -> Iterator[tuple[Group, list[Person]]]:
        for group in self.groups(min_people=min_people, phrase=phrase, after=after):
            parents: list[Person] = []
            size = 0
            for parent in self._parents(group):
                added = sum(len(item.quote) + len(parent.parent) + 120 for item in self._evidence(parent, group.phrase))
                if parents and (len(parents) == PEOPLE or size + added > INPUT_CHARS - 8000):
                    yield group, parents
                    parents, size = [], 0
                parents.append(parent)
                size += added
            if parents:
                yield group, parents

    def run(self, *, limit: int, min_people: int = 3, phrase: str = '', after: str = '') -> DiscoveryReport:
        if limit < 1:
            raise ValueError('limit must be positive')
        conn, report = self.store.conn, DiscoveryReport()
        conn.executescript('DROP TABLE IF EXISTS temp.discovery_seen; CREATE TEMP TABLE discovery_seen(key TEXT PRIMARY KEY);')
        previous_group = last_group = after
        for group, parents in self._batches(min_people=min_people, phrase=phrase, after=after):
            if group.key != last_group:
                previous_group, last_group = last_group, group.key
            key = group.key + ':' + short_hash(json.dumps([person.parent for person in parents]), n=64)
            conn.execute('INSERT INTO discovery_seen VALUES(?)', (key,))
            content, fingerprint, evidence = self._batch(group, parents)
            report.max_input_chars = max(report.max_input_chars, len(content))
            old = conn.execute('SELECT d.chunk_id,c.fingerprint FROM discovery d JOIN chunks c ON c.id=d.chunk_id WHERE d.key=?', (key,)).fetchone()
            if old is not None and old['fingerprint'] != fingerprint:
                with conn:
                    conn.execute('UPDATE claims SET active=0 WHERE chunk_id=? AND human=0', (old['chunk_id'],))
            saved = conn.execute('SELECT id,response FROM chunks WHERE fingerprint=? AND position=-1 ORDER BY id LIMIT 1', (fingerprint,)).fetchone()
            if saved is None:
                if report.calls == limit:
                    report.next_after, report.has_more = previous_group, True
                    return report
                response = self.provider.discover(content)
                report.calls += 1
                source = evidence[0]
                with conn:
                    conn.execute('INSERT INTO chunks(source_id,source_hash,position,fingerprint,response,error) VALUES(?,?,-1,?,?,\'\')',
                                          (source.source_id, source.source_hash, fingerprint, response))
                chunk_id = int(conn.execute('SELECT last_insert_rowid()').fetchone()[0])
            else:
                chunk_id, response = saved['id'], saved['response']
                report.reused += 1
            report.batches += 1
            with conn:
                conn.execute('INSERT INTO discovery VALUES(?,?) ON CONFLICT(key) DO UPDATE SET chunk_id=excluded.chunk_id', (key, chunk_id))
                try:
                    decision = Decision.parse(response)
                    report.abstentions += decision.decision != 'supported'
                    report.tags += self._apply(decision, evidence, chunk_id)
                    if len(report.insights) < 4:
                        report.insights += (self._insight(group, parents, decision),)
                    conn.execute('UPDATE chunks SET error=\'\' WHERE id=?', (chunk_id,))
                except (ValueError, KeyError, TypeError):
                    report.exclusions += 1
                    conn.execute("UPDATE chunks SET error='invalid or unsupported discovery; saved response retained' WHERE id=?", (chunk_id,))
        with conn:
            for row in conn.execute('SELECT key,chunk_id FROM discovery WHERE key NOT IN (SELECT key FROM discovery_seen)'):
                group_key = row['key'].rsplit(':', 1)[0]
                if group_key <= after or (self.next_after and group_key > self.next_after):
                    continue
                if not phrase or group_key in ('name:' + normalized(phrase), 'saved:' + normalized(phrase)):
                    conn.execute('UPDATE claims SET active=0 WHERE chunk_id=? AND human=0', (row['chunk_id'],))
                    conn.execute('DELETE FROM discovery WHERE key=?', (row['key'],))
        report.next_after, report.has_more = self.next_after, bool(self.next_after)
        report.groups = conn.execute('SELECT count(*) FROM discovery_seen').fetchone()[0]
        return report

    def _insight(self, group: Group, parents: list[Person], decision: Decision) -> Insight:
        members = tuple(IdentityMatch(person.parent, self.store.conn.execute(
            'SELECT min(name) FROM identities WHERE parent=? AND present=1', (person.parent,)).fetchone()[0]) for person in parents)
        return Insight(group.phrase, decision.decision, decision.reason, members, decision.tags)

    def _apply(self, decision: Decision, evidence: tuple[Evidence, ...], chunk_id: int) -> int:
        validated: list[tuple[Evidence, Claim]] = []
        for tag in decision.tags:
            for support in tag.evidence:
                source = next((item for item in evidence if item.parent == support.parent and item.source_id == support.source_id
                               and support.quote.strip() and support.quote in item.quote), None)
                if source is None:
                    raise ValueError('discovery evidence is outside the batch')
                claim = Claim(tag.explanation, Kind.TAG, tag.label, 'association', '', '', '', support.quote, tag.aliases)
                validated.append((source, claim))
        for source, claim in validated:
            self.store.add_claim(source.source_id, source.source_hash, chunk_id, claim, human=False)
        return len(validated)
