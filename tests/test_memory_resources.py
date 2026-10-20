"""Current-schema offline workload; coordinator also runs it under a 1 GiB cgroup."""
from __future__ import annotations

import json
import resource
import sqlite3
import sys
import tempfile
import time
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

from test_memory import FixtureProvider, source_db

from packs.ingestion.primitives.common.jsonio import sha256_file
from packs.ingestion.primitives.deep_context_v2.db.schema import SCHEMA_VERSION
from packs.memory.directory import Directory, Origin, Selector
from packs.memory.generate import Generate
from packs.memory.discovery import Discover
from packs.memory.models import Claim, document, packed
from packs.ingestion.primitives.common.jsonio import short_hash
from packs.memory.models import Filters, Kind, Source
from packs.memory.query import Query
from packs.memory.sources import V2
from packs.memory.store import Store


def stories(count: int) -> Iterator[dict]:
    for number in range(count):
        text = f"Synthetic person {number} built a permission architecture."
        claim = {"text": text, "kind": "work", "topic": "access controls", "scope": "Synthetic Corp",
                 "object": "Synthetic Corp", "start": "", "end": "", "quote": text, "aliases": []}
        yield {"member": f"c{number:05d}", "parent": f"p:{number}",
               "name": f"Synthetic Person {number}", "claims": [claim]}


class ResourceTests(unittest.TestCase):
    def test_current_sqlite_import_checkpoint_noop_and_query_10000_people(self) -> None:
        count = 10000
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            adapter = V2(base / "source")
            source_db(adapter.database, stories(count))
            with sqlite3.connect(adapter.database) as source:
                self.assertEqual(source.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "6")
                self.assertEqual(SCHEMA_VERSION, 6)
                # Current typed facts are searchable before any generation.
                source.execute("UPDATE facts SET facts_json=json_set(facts_json,'$.topics',json('[\"access controls\"]')) WHERE candidate_id='c00000'")
                source.execute("INSERT INTO person_tags(candidate_id,tags,note,updated_at) VALUES('c00000','friend','Human note','now')")
                # A candidate pending identity resolution stays outside memory.
                source.execute("INSERT INTO candidates VALUES('pending','Synthetic Pending',0,'{}','now')")
                source.execute("INSERT INTO facts SELECT 'pending',facts_json,input_fingerprint,model,reasoning_effort,synthesized_at FROM facts WHERE candidate_id='c00000'")
            before = sha256_file(adapter.database)
            inventory = adapter.inventory()
            self.assertEqual(inventory['counts']['candidates'] - inventory['counts']['current_parent'], 1)
            store = Store(base / "memory.sqlite", create=True)
            self.addCleanup(store.close)
            original_sources = V2.sources

            def interrupted_sources(source: V2) -> Iterator[Source]:
                for number, item in enumerate(original_sources(source)):
                    if number == 250:
                        raise ValueError("synthetic import interruption")
                    yield item

            with patch.object(V2, 'sources', interrupted_sources):
                with self.assertRaisesRegex(ValueError, 'import interruption'):
                    store.import_source(adapter)
            store.close()
            store = Store(base / "memory.sqlite")
            self.addCleanup(store.close)
            self.assertEqual(store.conn.execute('SELECT count(*) FROM identities').fetchone()[0], count)
            self.assertEqual(store.conn.execute('SELECT count(*) FROM sources').fetchone()[0], 250)
            imported = store.import_source(adapter)
            self.assertEqual((imported.members, imported.parents, imported.sources, imported.changed),
                             (count, count, count * 2 + 1, count * 2 + 1 - 250))
            self.assertEqual(store.lookup('Synthetic Pending'), [])
            directory = Directory(store)
            started = time.perf_counter()
            selector = directory.resolve('person')[0].selector
            self.assertEqual(selector, Selector(Origin.SOURCE_NAME, 'person'))
            after, enumerated, previous = '', 0, ''
            while True:
                page = directory.enumerate(selector, limit=100, after=after)
                self.assertEqual(page.total, count)
                self.assertLessEqual(len(page.people), 100)
                for person in page.people:
                    self.assertGreater(person.parent, previous)
                    previous = person.parent
                    enumerated += 1
                if not page.next_after:
                    break
                after = page.next_after
            self.assertEqual(enumerated, count)
            directory_seconds = time.perf_counter() - started
            provider = FixtureProvider()
            query = Query(store, provider)
            immediate = query.run('', filters=Filters(kind=Kind.MENTION, topic='access controls'))
            self.assertEqual([hit.name for hit in immediate.hits], ['Synthetic Person 0'])
            self.assertEqual(provider.generations + provider.embeddings, 0)
            first = Generate(store, provider).run(limit=1)
            self.assertEqual((first.generation_calls, first.embedding_calls), (1, 0))
            report = Generate(store, provider).run(limit=count * 3)
            self.assertEqual(report.generation_calls, count)
            # Each generated source uses one request; the extra projected claims share one batch.
            self.assertEqual(report.embedding_calls, count + 1)
            self.assertEqual(store.conn.execute("SELECT count(*) FROM embeddings").fetchone()[0], count + 3)
            saved = tuple(store.conn.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                          for table in ('revisions', 'chunks', 'claims', 'embeddings'))
            again = store.import_source(adapter)
            self.assertEqual((again.changed, again.failed_reads), (0, 0))
            resumed = Generate(store, provider).run(limit=count * 3)
            self.assertEqual((resumed.generation_calls, resumed.embedding_calls), (0, 0))
            self.assertEqual(tuple(store.conn.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                                   for table in ('revisions', 'chunks', 'claims', 'embeddings')), saved)
            result = query.run('permission architecture', filters=Filters(kind=Kind.WORK))
            self.assertEqual(len(result.hits), 10)
            self.assertEqual(result.vector_candidates, 100)
            self.assertTrue(all(hit.citations and '#facts/' in hit.citations[0].source for hit in result.hits))
            self.assertEqual(sha256_file(adapter.database), before)
            # Upstream identity correction alone remaps memory without new facts or calls.
            with sqlite3.connect(adapter.database) as source:
                source.execute("INSERT INTO candidate_parent(candidate_id,parent_id,reason,created_at) VALUES('c00001','p:0','human','later')")
            remapped = store.import_source(adapter)
            self.assertEqual((remapped.parents, remapped.changed), (count - 1, 0))
            self.assertEqual(store.lookup('Synthetic Person 1')[0].parent, store.lookup('Synthetic Person 0')[0].parent)
            self.assertEqual(Generate(store, provider).run(limit=1).generation_calls, 0)
            self.assertEqual(store.conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(store.conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            peak_bytes = rss if sys.platform == 'darwin' else rss * 1024
            self.assertLessEqual(peak_bytes, 256 * 1024 * 1024)
            limits = {}
            for name in ('memory.max', 'memory.swap.max', 'memory.peak', 'memory.events'):
                path = Path('/sys/fs/cgroup') / name
                if path.exists():
                    limits[name] = path.read_text().strip()
            print(json.dumps({'people': count, 'schema_version': SCHEMA_VERSION, 'sources': count * 2 + 1,
                              'directory_people': enumerated, 'directory_seconds': directory_seconds,
                              'process_tree_peak_rss_bytes': peak_bytes, 'children': 0, 'cgroup': limits,
                              'provider': 'synthetic; semantic quality unverified'}))

    def test_semantic_preview_bounds_10000_unique_cached_passages(self) -> None:
        count = 10000
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            adapter = V2(base / 'source')
            source_db(adapter.database, ({'member': f'c{i:05d}', 'parent': f'c{i:05d}',
                                         'name': f'Synthetic{i}', 'claims': []} for i in range(count)))
            with sqlite3.connect(adapter.database) as conn:
                conn.execute("UPDATE facts SET facts_json=json_set(facts_json,'$.location','Shared conference experience '||candidate_id)")
            store = Store(base / 'memory.sqlite', create=True)
            self.addCleanup(store.close)
            store.import_source(adapter)
            vector = packed([1.0] + [0.0] * 1535)
            with store.conn:
                for row in store.conn.execute('SELECT payload FROM active_claims'):
                    text = document(Claim.parse(json.loads(row[0])))
                    store.conn.execute('INSERT INTO embeddings VALUES(?,?,?)', ('synthetic-cache', short_hash(text, n=64), vector))
            class CachedOnly:
                discovery_key = 'synthetic-discovery'
                embedding_key = 'synthetic-cache'

                def discover(self, content: str) -> str:
                    raise AssertionError('preview cannot call a provider')

            discover = Discover(store, CachedOnly())
            with patch.object(discover, '_vector', wraps=discover._vector) as vectors:
                preview = discover.preview(limit=1)
            self.assertEqual((preview.pending_calls, preview.groups), (0, ()))
            self.assertEqual(vectors.call_count, 0)
            self.assertLessEqual(vectors.call_count, count + 400)
            self.assertLessEqual(preview.max_input_chars, 24000)
            print(json.dumps({'semantic_unique_passages': count, 'cached_dimensions': 1536,
                              'vector_reads': vectors.call_count, 'preview_batches': preview.batches,
                              'max_input_chars': preview.max_input_chars}))


if __name__ == '__main__':
    unittest.main()
