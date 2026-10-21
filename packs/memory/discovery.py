"""Incremental cross-person interpretation using original evidence and cached retrieval."""
from __future__ import annotations

import json
import re
from collections.abc import Generator, Iterator

from packs.ingestion.primitives.common.jsonio import short_hash
from packs.memory.discovery_provider import Decision, DiscoveryProvider, INPUT_CHARS
from packs.memory.discovery_run import Group, Person, Evidence, Insight, DiscoveryReport, Preview, preview, progress
from packs.memory.models import Claim, Kind, IdentityMatch, cached_vector, document, normalized
from packs.memory.store import Store
from packs.memory.progress import Progress, consume

# Original saved evidence: never a model's own output, and never the owner labels (family, close friend …),
# which already say who someone is and must not seed or re-key paid discovery.
_ORIGINAL = "c.chunk_id IS NULL AND c.human=0 AND NOT (c.kind='tag' AND c.scope='owner')"
_STOPWORDS = frozenset("a an and as at be by for he her him his i if in is it its me my no not of on or our out she so the to up us we you about after again also been before being both could does doing during each from have here into just more most much must only other over same should some such than that their them then there these they this those through under very were what when where which while with would your".split())
_SEED_TOPICS = "'title','school','field_of_study','location','location_str','schoolname','degree','fieldofstudy'"


def _name_seed(phrase: str, name: str) -> bool:
    tokens = re.findall(r"[^\W_]+", phrase)
    acronyms = {token.casefold() for token in re.findall(r"[^\W_]+", name)
                if sum(character.isupper() for character in token) >= 2}
    return len(phrase) <= 40 and bool(tokens) and all(
        not token.isnumeric() and token not in _STOPWORDS and (len(token) >= 4 or token in acronyms)
        for token in tokens)


NEIGHBORS = 8
MIN_SIMILARITY = 0.35  # Pilot within-concept p25=.390; this rejects remote candidates, not false claims.


class Discover:
    def __init__(self, store: Store, provider: DiscoveryProvider) -> None:
        self.store, self.provider = store, provider
        self.next_after = ""
        self._indexed = False
        store.conn.create_function("discovery_name_seed", 2, _name_seed, deterministic=True)

    def _index(self) -> None:
        if self._indexed:
            return
        self._indexed = True
        conn = self.store.conn
        conn.executescript("CREATE TEMP TABLE IF NOT EXISTS discovery_vectors("
                           "claim_id INTEGER,band INTEGER,value TEXT,PRIMARY KEY(claim_id,band)); "
                           "CREATE INDEX IF NOT EXISTS temp.discovery_vector_values ON discovery_vectors(band,value);")
        conn.executescript("DELETE FROM discovery_vectors; "
                           "CREATE VIRTUAL TABLE IF NOT EXISTS temp.discovery_lexical USING fts5(text,topic,scope,object);")
        conn.execute('DELETE FROM discovery_lexical')
        conn.execute('INSERT INTO discovery_lexical(rowid,text,topic,scope,object) '
                     'SELECT c.id,c.text,c.topic,c.scope,c.object FROM active_claims c WHERE ' + _ORIGINAL)
        conn.executescript("CREATE VIRTUAL TABLE IF NOT EXISTS temp.discovery_words USING fts5vocab('temp','discovery_lexical','row');")
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
        self._index()
        for row in conn.execute(
            'SELECT c.parent,min(c.id) id FROM active_claims c WHERE ' + _ORIGINAL + ' AND c.grouping_value=? '
            'GROUP BY c.parent ORDER BY c.parent', (group.phrase,)):
            yield Person(row['parent'], row['id'])
        found: set[str] = set()
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
                if parent in found or conn.execute(
                    'SELECT 1 FROM active_claims c WHERE ' + _ORIGINAL +
                    ' AND c.grouping_value=? AND c.parent=? LIMIT 1', (group.phrase, parent)).fetchone():
                    continue
                yield Person(parent, claim_id)
                found.add(parent)
                added += 1
                if added == NEIGHBORS:
                    break
        for person in self._lexical(group.phrase):
            if person.parent not in found and not conn.execute(
                'SELECT 1 FROM active_claims c WHERE ' + _ORIGINAL +
                ' AND c.grouping_value=? AND c.parent=? LIMIT 1', (group.phrase, person.parent)).fetchone():
                yield person
                found.add(person.parent)

    def groups(self, *, min_people: int = 3, phrase: str = '', after: str = '') -> Iterator[Group]:
        if min_people < 2:
            raise ValueError('min_people must be at least 2')
        phrase = normalized(phrase)
        inspected = 0
        self.next_after = ''
        conn = self.store.conn
        if after < 'saved:':
            cursor = after.removeprefix('name:')
            name_filter = 'p.phrase=?' if phrase else 'discovery_name_seed(p.phrase,r.content)'
            params = (cursor, phrase, min_people) if phrase else (cursor, min_people)
            for row in list(conn.execute(
                'SELECT p.phrase,count(DISTINCT i.parent) people FROM phrases p JOIN sources s ON s.id=p.source_id '
                'JOIN identities i USING(namespace,member) JOIN revisions r ON r.source_id=s.id AND r.hash=s.hash '
                'WHERE s.present=1 AND i.present=1 AND p.phrase>? AND ' + name_filter +
                ' GROUP BY p.phrase HAVING count(DISTINCT i.parent)>=? ORDER BY p.phrase LIMIT 101', params)):
                if inspected == 100:
                    return
                inspected += 1
                self.next_after = 'name:' + row['phrase']
                yield Group(self.next_after, row['phrase'], row['people'])
        cursor = after.removeprefix('saved:') if after.startswith('saved:') else ''
        saved_filter = 'c.grouping_value=?' if phrase else (
            "length(c.grouping_value) BETWEEN 1 AND 40 AND (c.object<>'' OR c.kind IN ('mention','tag') OR c.topic IN (" + _SEED_TOPICS + '))')
        params = (cursor, phrase, min_people) if phrase else (cursor, min_people)
        for row in list(conn.execute(
            'SELECT c.grouping_value phrase,min(c.id) seed,count(DISTINCT c.parent) people '
            'FROM active_claims c WHERE ' + _ORIGINAL + ' AND c.grouping_value>? AND ' + saved_filter +
            ' GROUP BY c.grouping_value HAVING count(DISTINCT c.parent)>=? ORDER BY c.grouping_value LIMIT 101', params)):
            if inspected == 100:
                return
            inspected += 1
            self.next_after = 'saved:' + row['phrase']
            yield Group(self.next_after, row['phrase'], row['people'], row['seed'])
        self.next_after = ''

    def _evidence(self, person: Person, phrase: str) -> tuple[Evidence, ...]:
        self._index()
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
                "WHERE i.parent=? AND i.present=1 AND s.present=1 AND s.key NOT LIKE 'labels:%' ORDER BY s.id", (parent,)))
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
        return preview(self, min_people=min_people, phrase=phrase, after=after, limit=limit)

    def run(self, *, limit: int, min_people: int = 3, phrase: str = '', after: str = '') -> DiscoveryReport:
        return consume(self.progress(limit=limit, min_people=min_people, phrase=phrase, after=after))

    def progress(self, *, limit: int, min_people: int = 3, phrase: str = '', after: str = '',
                 first: tuple[str, ...] = ()) -> Generator[Progress, None, DiscoveryReport]:
        return progress(self, limit=limit, min_people=min_people, phrase=phrase, after=after, first=first)

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
