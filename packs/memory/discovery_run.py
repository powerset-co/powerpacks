"""Yield each discovery batch after saving its response and applying supported tags."""
from __future__ import annotations

import json
from collections.abc import Generator, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from packs.ingestion.primitives.common.jsonio import short_hash
from packs.memory.discovery_provider import Decision, INPUT_CHARS, OUTPUT_TOKENS, PEOPLE, SYSTEM, Tag, Outcome
from packs.memory.models import normalized, IdentityMatch
from packs.memory.progress import Progress, Step, retry_first
from packs.memory.provider import NeedsApproval, SpendReached


if TYPE_CHECKING:
    from packs.memory.discovery import Discover


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
    failures: tuple[tuple[str, str], ...] = ()
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


def progress(discover: Discover, *, limit: int, min_people: int = 3, phrase: str = '', after: str = '',
             first: tuple[str, ...] = ()) -> Generator[Progress, None, DiscoveryReport]:
    if limit < 1:
        raise ValueError('limit must be positive')
    conn, report = discover.store.conn, DiscoveryReport()
    conn.executescript('DROP TABLE IF EXISTS temp.discovery_seen; CREATE TEMP TABLE discovery_seen(key TEXT PRIMARY KEY);')
    previous_group = last_group = after
    def batch_key(batch: tuple[Group, list[Person]]) -> str:
        group, parents = batch
        return group.key + ':' + short_hash(json.dumps([person.parent for person in parents]), n=64)
    def items() -> Iterator[tuple[Group, list[Person]]]:
        cursor = after
        while True:
            yield from batches(discover, min_people=min_people, phrase=phrase, after=cursor)
            if not discover.next_after:
                return
            cursor = discover.next_after
    total = sum(1 for _ in items())
    for done, (group, parents) in enumerate(retry_first(items, batch_key, first), 1):
        if group.key != last_group:
            previous_group, last_group = last_group, group.key
        key = group.key + ':' + short_hash(json.dumps([person.parent for person in parents]), n=64)
        conn.execute('INSERT INTO discovery_seen VALUES(?)', (key,))
        error = ""
        try:
            content, fingerprint, evidence = discover._batch(group, parents)
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
                response = discover.provider.discover(content)
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
                except (ValueError, KeyError, TypeError):
                    report.exclusions += 1
                    error = "invalid discovery; saved response retained"
                    conn.execute("UPDATE chunks SET error=? WHERE id=?", (error, chunk_id))
                else:
                    try:
                        report.abstentions += decision.decision != 'supported'
                        report.tags += discover._apply(decision, evidence, chunk_id)
                        if len(report.insights) < 4:
                            report.insights += (discover._insight(group, parents, decision),)
                        conn.execute('UPDATE chunks SET error=\'\' WHERE id=?', (chunk_id,))
                    except (ValueError, KeyError, TypeError):
                        report.exclusions += 1
                        conn.execute("UPDATE chunks SET error=? WHERE id=?",
                                     ("unsupported discovery; saved response retained", chunk_id))
        except SpendReached:
            yield Progress(Step.DISCOVER, done - 1, total, "")
            raise
        except NeedsApproval:
            raise
        except Exception as exc:
            error = str(exc)
        yield Progress(Step.DISCOVER, done, total, key, error)
    with conn:
        for row in conn.execute('SELECT key,chunk_id FROM discovery WHERE key NOT IN (SELECT key FROM discovery_seen)'):
            group_key = row['key'].rsplit(':', 1)[0]
            if group_key <= after or (discover.next_after and group_key > discover.next_after):
                continue
            if not phrase or group_key in ('name:' + normalized(phrase), 'saved:' + normalized(phrase)):
                conn.execute('UPDATE claims SET active=0 WHERE chunk_id=? AND human=0', (row['chunk_id'],))
                conn.execute('DELETE FROM discovery WHERE key=?', (row['key'],))
    report.next_after, report.has_more = discover.next_after, bool(discover.next_after)
    report.groups = conn.execute('SELECT count(*) FROM discovery_seen').fetchone()[0]
    return report



def preview(discover: Discover, *, min_people: int = 3, phrase: str = '', after: str = '', limit: int = 100) -> Preview:
    if limit < 1:
        raise ValueError('limit must be positive')
    groups: list[Group] = []
    insights: list[Insight] = []
    total = 0
    pending = 0
    max_chars = 0
    previous_group = last_group = after
    for group, parents in batches(discover, min_people=min_people, phrase=phrase, after=after):
        if group.key != last_group:
            previous_group, last_group = last_group, group.key
        content, fingerprint, _ = discover._batch(group, parents)
        max_chars = max(max_chars, len(content))
        total += 1
        saved = discover.store.conn.execute('SELECT response,error FROM chunks WHERE fingerprint=? AND position=-1', (fingerprint,)).fetchone()
        pending += saved is None
        if saved is not None and not saved['error'] and len(insights) < 4:
            try:
                insights.append(discover._insight(group, parents, Decision.parse(saved[0])))
            except (ValueError, KeyError, TypeError):
                pass
        if len(groups) < 100:
            groups.append(group)
        if pending == limit:
            return Preview(tuple(groups), total, pending, max_chars, INPUT_CHARS, OUTPUT_TOKENS,
                           len(SYSTEM), min(limit, pending), previous_group, True, tuple(insights))
    return Preview(tuple(groups), total, pending, max_chars, INPUT_CHARS, OUTPUT_TOKENS,
                   len(SYSTEM), min(limit, pending), discover.next_after, bool(discover.next_after), tuple(insights))

def batches(discover: Discover, *, min_people: int, phrase: str, after: str) -> Iterator[tuple[Group, list[Person]]]:
    for group in discover.groups(min_people=min_people, phrase=phrase, after=after):
        parents: list[Person] = []
        size = 0
        for parent in discover._parents(group):
            added = sum(len(item.quote) + len(parent.parent) + 120 for item in discover._evidence(parent, group.phrase))
            if parents and (len(parents) == PEOPLE or size + added > INPUT_CHARS - 8000):
                yield group, parents
                parents, size = [], 0
            parents.append(parent)
            size += added
        if parents:
            yield group, parents
