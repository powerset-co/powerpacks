"""Synthetic graph links and read-only HTTP reads; no network providers."""
from __future__ import annotations

import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlparse

from test_memory import source_db
from packs.memory import job
from packs.memory.directory import Origin, Selector
from packs.memory.graph import graph
from packs.memory.models import Claim, Kind
from packs.memory.sources import V2
from packs.memory.store import Store
from packs.shared.web.memory import MemoryApi


class MemoryGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        adapter = V2(self.root / '.powerpacks')
        source_db(adapter.database, [
            {'member': 'alpha', 'parent': 'a', 'name': 'Jordan Synthetic', 'claims': []},
            {'member': 'duplicate', 'parent': 'a', 'name': 'Jordan Duplicate', 'claims': []},
            {'member': 'bravo', 'parent': 'b', 'name': 'Casey Synthetic', 'claims': []},
            {'member': 'charlie', 'parent': 'c', 'name': 'Morgan Synthetic', 'claims': []},
        ])
        self.store = Store(job.database(self.root), create=True)
        self.addCleanup(self.store.close)
        self.store.import_source(adapter)
        for member, topic in (('alpha', 'sailing'), ('duplicate', 'sailing'), ('bravo', 'sailing'), ('charlie', 'hiking')):
            row = self.store.conn.execute('SELECT id,hash FROM sources WHERE key=?', ('facts:' + member,)).fetchone()
            with self.store.conn:
                self.store.add_claim(row['id'], row['hash'], None,
                                     Claim(topic, Kind.MENTION, topic, '', '', '', '', 'Synthetic supporting quote'), human=False)

    def test_links_use_canonical_people_and_only_current_claims(self) -> None:
        result = graph(self.store)
        self.assertEqual(result.total_people, 3)
        self.assertEqual({node.id for node in result.nodes if node.kind == 'person'}, {'a', 'b', 'c'})
        self.assertEqual(sum(edge.source == 'a' and edge.target == 'topic:sailing' for edge in result.edges), 1)
        self.assertNotIn('Synthetic supporting quote', json.dumps(asdict(result)))
        row = self.store.conn.execute("SELECT id FROM active_claims WHERE topic='hiking'").fetchone()
        self.store.correct(row[0], None, 'Synthetic rejection')
        result = graph(self.store)
        self.assertNotIn('topic:hiking', {node.id for node in result.nodes})

    def test_bounded_focus_shows_person_and_selector_outside_default_graph(self) -> None:
        initial = graph(self.store, limit=2)
        self.assertNotIn('c', {node.id for node in initial.nodes})
        focused = graph(self.store, limit=2, parent='c')
        self.assertEqual({node.id for node in focused.nodes}, {'c', 'topic:hiking'})
        self.assertEqual(focused.selected_ids, ('c',))
        self.assertEqual(len(focused.edges), 1)
        self.assertTrue(focused.truncated)
        selected = graph(self.store, limit=2, selector=Selector(Origin.TOPIC, 'hiking'))
        self.assertEqual(set(selected.selected_ids), {'c', 'topic:hiking'})
        for limit in (1, 1001):
            with self.assertRaises(ValueError):
                graph(self.store, limit=limit)

    def test_graph_does_not_link_people_through_saved_field_categories(self) -> None:
        row = self.store.conn.execute("SELECT id,hash FROM sources WHERE key='facts:alpha'").fetchone()
        with self.store.conn:
            for kind, topic in ((Kind.RELATIONSHIP, 'relationship'), (Kind.MENTION, 'degree'), (Kind.TAG, 'location')):
                self.store.add_claim(row['id'], row['hash'], None,
                                     Claim('Synthetic field value', kind, topic, '', '', '', '', 'Synthetic field value'), human=False)
        ids = {node.id for node in graph(self.store).nodes}
        self.assertTrue({'topic:relationship', 'topic:degree', 'topic:location'}.isdisjoint(ids))

    def test_topic_focus_includes_original_human_tags(self) -> None:
        adapter = V2(self.root / '.powerpacks')
        with closing(sqlite3.connect(adapter.database)) as conn:
            conn.execute("INSERT INTO person_tags(candidate_id,tags,note,updated_at) VALUES('alpha','friends','','now')")
            conn.commit()
        self.store.import_source(adapter)
        focused = graph(self.store, topic='Friends', limit=2)
        self.assertEqual(set(focused.selected_ids), {'a', 'topic:friends'})
        self.assertEqual([(edge.source, edge.target) for edge in focused.edges], [('a', 'topic:friends')])

    def test_read_only_connection_and_http_routes(self) -> None:
        with closing(Store(job.database(self.root), read_only=True)) as store:
            graph(store)
            with self.assertRaises(sqlite3.OperationalError):
                store.conn.execute('DELETE FROM claims')
        api = MemoryApi(self.root)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                api.get(self, urlparse(self.path))

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with closing(http.client.HTTPConnection('127.0.0.1', server.server_port)) as client:
            for query in ({'parent': 'c', 'limit': 2}, {'origin': 'topic', 'value': 'hiking', 'limit': 2}, {'topic': 'hiking', 'limit': 2}):
                client.request('GET', '/api/memory/graph?' + urlencode(query))
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read())
                self.assertIn('c', payload['selected_ids'])
                self.assertLessEqual(len(payload['nodes']), 2)


if __name__ == '__main__':
    unittest.main()
