"""Resolve saved meanings, then enumerate canonical people without ranked-search limits."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from packs.memory.models import Citation, IdentityMatch, object_dict, string, strings, normalized
from packs.memory.provider import OpenAIProvider
from packs.memory.query import Query
from packs.memory.store import Store


class Origin(StrEnum):
    SOURCE_NAME = 'source_name'
    HUMAN_TAG = 'human_tag'
    TOPIC = 'topic'
    SAVED_VALUE = 'saved_value'


@dataclass(frozen=True)
class Selector:
    origin: Origin
    value: str


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
            return (sql + 'NOT (' + original + ") AND normalized(CASE WHEN c.object<>'' THEN c.object ELSE c.text END)=?", (value,))
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

    def resolve(self, text: str, *, embedding_model: str = '', approve_spend: bool = False) -> tuple[Meaning, ...]:
        value = normalized(text)
        if not value or len(text) > 9000:
            raise ValueError('resolve requires 1..9000 characters')
        meanings: list[Meaning] = []
        topics = {value, *(row[0] for row in self.store.conn.execute('SELECT topic FROM aliases WHERE alias=? AND confirmed=1', (value,)))}
        for origin in Origin:
            for topic in sorted(topics if origin in (Origin.HUMAN_TAG, Origin.TOPIC) else {value}):
                selector = Selector(origin, topic)
                page = self.enumerate(selector, limit=1)
                if page.total:
                    meanings.append(Meaning(selector, page.total, page.people[0].evidence, 'exact or confirmed alias'))
        if meanings:
            return tuple(meanings)
        provider = OpenAIProvider('', embedding_model, approve_spend=approve_spend)
        try:
            hits = Query(self.store, provider).run(text, limit=20).hits
        finally:
            provider.close()
        selectors: set[Selector] = set()
        for hit in hits:
            origin = Origin.HUMAN_TAG if any(c.level == 'original human tag' for c in hit.citations) else Origin.TOPIC
            selector = Selector(origin, hit.claim.topic) if hit.claim.kind in ('mention', 'tag') else Selector(Origin.SAVED_VALUE, hit.claim.object or hit.claim.text)
            if selector in selectors:
                continue
            selectors.add(selector)
            page = self.enumerate(selector, limit=1)
            meanings.append(Meaning(selector, page.total, hit.citations, 'suggestion; confirm meaning before enumeration'))
        return tuple(meanings)
