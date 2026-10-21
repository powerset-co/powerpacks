"""Resolve saved meanings, then enumerate canonical people without ranked-search limits."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re

from packs.memory.models import Citation, IdentityMatch, object_dict, string, strings, normalized
from packs.memory.provider import OpenAIProvider
from packs.memory.query import Query
from packs.memory.store import Store

MAX_MEANINGS = 40


class Origin(StrEnum):
    SOURCE_NAME = 'source_name'
    HUMAN_TAG = 'human_tag'
    TOPIC = 'topic'
    SAVED_VALUE = 'saved_value'


@dataclass(frozen=True)
class Selector:
    origin: Origin
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, 'value', normalized(self.value))


@dataclass(frozen=True)
class Meaning:
    selector: Selector
    count: int
    evidence: tuple[Citation, ...]
    match: str


@dataclass(frozen=True)
class Person(IdentityMatch):
    members: tuple[str, ...]
    evidence: tuple[Citation, ...] = ()


@dataclass(frozen=True)
class Selection:
    corpus: str
    people: tuple[Person, ...]

    @classmethod
    def parse(cls, value: object) -> Selection:
        row = object_dict(value)
        entries = row['people']
        if not isinstance(entries, list) or not entries:
            raise ValueError('selection requires explicit people')
        people = []
        for value in entries:
            person = object_dict(value)
            people.append(Person(string(person['parent']), string(person['name']), strings(person['members'])))
        return cls(string(row['corpus']), tuple(people))


@dataclass(frozen=True)
class Page(Selection):
    selector: Selector
    total: int
    next_after: str


class Directory:
    def __init__(self, store: Store) -> None:
        self.store = store
        row = store.conn.execute('SELECT DISTINCT namespace FROM identities').fetchone()
        if row is None:
            raise ValueError('import a source corpus first')
        self.corpus: str = row[0]

    def matching_sql(self, selector: Selector) -> tuple[str, tuple[str, ...]]:
        value = normalized(selector.value)
        if selector.origin == Origin.SOURCE_NAME:
            return ("SELECT i.parent,i.name,s.citation,s.hash source_hash,r.content quote,0 claim_id "
                    "FROM identities i CROSS JOIN sources s USING(namespace,member) CROSS JOIN phrases p ON p.source_id=s.id "
                    "JOIN revisions r ON r.source_id=s.id AND r.hash=s.hash "
                    "WHERE p.phrase=? AND s.present=1 AND i.present=1", (value,))
        sql = "SELECT c.parent,c.name,c.citation,c.source_hash,json_extract(c.payload,'$.quote') quote,c.id claim_id FROM active_claims c WHERE "
        original = "c.kind='tag' AND c.source_key LIKE 'tags:%' AND c.chunk_id IS NULL AND c.human=0"
        if selector.origin == Origin.SAVED_VALUE:
            return (sql + 'NOT (' + original + ') AND (normalized(c.object)=? OR normalized(c.text)=?)', (value, value))
        where = original if selector.origin == Origin.HUMAN_TAG else 'NOT (' + original + ')'
        return (sql + where + " AND c.topic IN "
                "(SELECT ? UNION SELECT alias FROM aliases WHERE topic=? AND confirmed=1)", (value, value))

    def enumerate(self, selector: Selector, *, after: str = '', limit: int = 50) -> Page:
        if not 1 <= limit <= 100:
            raise ValueError('page limit outside 1..100')
        sql, params = self.matching_sql(selector)
        conn = self.store.conn
        total = conn.execute('SELECT count(DISTINCT parent) FROM (' + sql + ')', params).fetchone()[0]
        prefix = self.corpus + '#'
        rows = list(conn.execute('SELECT parent,min(name) name FROM (' + sql + ') WHERE parent>? '
                                 'GROUP BY parent ORDER BY parent LIMIT ?', (*params, prefix + after, limit + 1)))
        people = []
        for row in rows[:limit]:
            evidence = tuple(Citation(item['citation'], item['source_hash'], item['quote'], selector.origin.value)
                             for item in conn.execute('SELECT DISTINCT citation,source_hash,quote FROM (' + sql + ') '
                                                      'WHERE parent=? ORDER BY citation,source_hash,quote LIMIT 8', (*params, row['parent'])))
            members = tuple(item[0] for item in conn.execute('SELECT member FROM identities WHERE parent=? AND present=1 ORDER BY member', (row['parent'],)))
            people.append(Person(row['parent'][len(prefix):], row['name'], members, evidence))
        return Page(self.corpus, tuple(people), selector, total, people[-1].parent if len(rows) > limit else '')

    def _meaning(self, selector: Selector, match: str) -> Meaning | None:
        sql, params = self.matching_sql(selector)
        row = self.store.conn.execute('SELECT count(DISTINCT parent) total,citation,source_hash,quote FROM (' + sql + ')', params).fetchone()
        if not row['total']:
            return None
        citation = Citation(row['citation'], row['source_hash'], row['quote'], selector.origin.value)
        return Meaning(selector, row['total'], (citation,), match)

    def resolve(self, text: str, *, embedding_model: str = '', approve_spend: bool = False) -> tuple[Meaning, ...]:
        value = normalized(text)
        if not value or len(text) > 9000:
            raise ValueError('resolve requires 1..9000 characters')
        meanings: list[Meaning] = []
        topics = {value, *(row[0] for row in self.store.conn.execute('SELECT topic FROM aliases WHERE alias=? AND confirmed=1', (value,)))}
        for origin in Origin:
            for topic in sorted(topics if origin in (Origin.HUMAN_TAG, Origin.TOPIC) else {value}):
                selector = Selector(origin, topic)
                meaning = self._meaning(selector, 'exact or confirmed alias')
                if meaning:
                    meanings.append(meaning)
        selectors = {meaning.selector for meaning in meanings}
        tokens = re.findall(r'\w+', value, re.UNICODE)[:64]
        if tokens:
            match = ' OR '.join('"' + token + '"' for token in tokens)
            rows = list(self.store.conn.execute(
                'SELECT c.* FROM lexical JOIN active_claims c ON c.id=lexical.rowid '
                'WHERE lexical MATCH ? ORDER BY bm25(lexical),c.id', (match,)))
            # Shared topics and explicit relationships precede incidental prose matches.
            rows.sort(key=lambda row: (
                0 if row['kind'] == 'relationship' else
                1 if row['kind'] in ('tag', 'mention') and set(tokens) & set(re.findall(r'\w+', row['topic'], re.UNICODE))
                else 2))
            for row in rows:
                original = row['kind'] == 'tag' and row['source_key'].startswith('tags:') and row['chunk_id'] is None and not row['human']
                topic_match = bool(set(tokens) & set(re.findall(r'\w+', row['topic'], re.UNICODE)))
                if original:
                    selector = Selector(Origin.HUMAN_TAG, row['topic'])
                elif row['kind'] in ('mention', 'tag') and topic_match:
                    selector = Selector(Origin.TOPIC, row['topic'])
                else:
                    object_match = bool(set(tokens) & set(re.findall(r'\w+', row['object'], re.UNICODE)))
                    selector = Selector(Origin.SAVED_VALUE, row['object'] if object_match else row['text'])
                if selector in selectors:
                    continue
                selectors.add(selector)
                meaning = self._meaning(selector, 'saved lexical match')
                if meaning:
                    meanings.append(meaning)
                if len(meanings) >= MAX_MEANINGS:
                    break
        if meanings or not embedding_model:
            return tuple(meanings)
        provider = OpenAIProvider('', embedding_model, approve_spend=approve_spend)
        try:
            hits = Query(self.store, provider).run(text, limit=20).hits
        finally:
            provider.close()
        for hit in hits:
            origin = Origin.HUMAN_TAG if any(c.level == 'original human tag' for c in hit.citations) else Origin.TOPIC
            selector = Selector(origin, hit.claim.topic) if hit.claim.kind in ('mention', 'tag') else Selector(Origin.SAVED_VALUE, hit.claim.object or hit.claim.text)
            if selector in selectors:
                continue
            selectors.add(selector)
            page = self.enumerate(selector, limit=1)
            meanings.append(Meaning(selector, page.total, hit.citations, 'suggestion; confirm meaning before enumeration'))
        return tuple(meanings)
