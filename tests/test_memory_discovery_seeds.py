"""Exact repeated seeds precede optional cached neighbor retrieval."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import test_memory_discovery as fixtures
from packs.memory.discovery import Discover
from packs.memory.models import Claim, Kind, packed
from packs.memory.store import Store


class DiscoverySeedTests(unittest.TestCase):
    setUp = fixtures.DiscoveryTests.setUp
    imported = fixtures.DiscoveryTests.imported
    person = staticmethod(fixtures.DiscoveryTests.person)

    def test_auto_seeds_reject_prose_and_name_noise_preserve_short_labels(self):
        people = [self.person(f'c{i}', f'Amy and OF THE Jordan{i} SoF SF BA') for i in range(3)]
        fields = {person['member']: {'location': 'San Francisco', 'title': 'Engineer', 'school': 'Stanford',
                  'topics': ['robotics', 'A detailed biography that exceeds forty characters and repeats'],
                  'relationship_to_owner': 'Owner met this person during a conference.',
                  'notable_events': [{'date': '2026', 'summary': 'Shared event summary'}]}
                  for person in people}
        store = self.imported(people, fields)
        with store.conn:
            for row in store.conn.execute("SELECT id,hash FROM sources WHERE key LIKE 'facts:%'"):
                store.add_claim(row['id'], row['hash'], None,
                                Claim('SoF', Kind.TAG, 'SoF', '', '', '', '', 'SoF'), human=False)
                store.add_claim(row['id'], row['hash'], None,
                                Claim('Short biography', Kind.STATEMENT, 'summary', '', '', '', '', 'Short biography'), human=False)
        discover = Discover(store, self.provider)
        with patch.object(discover, '_parents', side_effect=AssertionError('counting retrieves neighbors')):
            groups = list(discover.groups())
        names = {group.phrase for group in groups if not group.seed}
        saved = {group.phrase for group in groups if group.seed}
        self.assertTrue({'sof', 'sf', 'ba'} <= names)
        self.assertFalse(any(set(name.split()) & {'amy', 'and', 'of', 'the'} for name in names))
        self.assertEqual(saved, {'san francisco', 'engineer', 'stanford', 'robotics', 'sof'})
        self.assertTrue(list(discover.groups(phrase='SoF')))
        self.assertTrue(list(discover.groups(phrase='Amy')))

    def test_neighbors_cannot_supply_minimum_people(self):
        people = [self.person(f'c{i}', f'Person{i}') for i in range(3)]
        store = self.imported(people, {f'c{i}': {'topics': [f'unique{i}']} for i in range(3)})
        discover = Discover(store, self.provider)
        with patch.object(discover, '_parents', side_effect=AssertionError('singleton retrieved')):
            self.assertEqual(list(discover.groups()), [])
            self.assertEqual(list(discover.groups(phrase='unique0')), [])

    def test_grouping_migration_preserves_saved_outputs_and_uses_value_index(self):
        people = [self.person(f'c{i}', f'Person{i}') for i in range(3)]
        store = self.imported(people, {person['member']: {'topics': ['Robotics']} for person in people})
        with store.conn:
            source = store.conn.execute('SELECT id,hash FROM sources LIMIT 1').fetchone()
            store.conn.execute("INSERT INTO chunks(source_id,source_hash,position,fingerprint,response,error) VALUES(?,?,0,'saved','paid response','')", tuple(source))
            store.conn.execute("INSERT INTO embeddings VALUES('saved','hash',?)", (packed([1., 0.]),))
        queries = ('SELECT id,payload,document_hash FROM claims', 'SELECT * FROM chunks', 'SELECT * FROM embeddings')
        original = [[tuple(row) for row in store.conn.execute(query)] for query in queries]
        with store.conn:
            store.conn.execute('DROP INDEX claims_grouping')
            store.conn.execute('ALTER TABLE claims DROP COLUMN grouping_value')
        migrated = Store(self.base / 'memory.sqlite')
        self.addCleanup(migrated.close)
        self.assertEqual([[tuple(row) for row in migrated.conn.execute(query)] for query in queries], original)
        plans = []
        migrated.conn.set_trace_callback(lambda sql: plans.append(sql))
        groups = list(Discover(migrated, self.provider).groups(after='saved:robot'))
        self.assertEqual([group.phrase for group in groups], ['robotics'])
        query = next(sql for sql in plans if 'min(c.id) seed' in sql)
        plan = [row['detail'] for row in migrated.conn.execute('EXPLAIN QUERY PLAN ' + query)]
        self.assertTrue(any('claims_grouping (grouping_value>?)' in line for line in plan), plan)
        self.assertNotIn('normalized(', query)

    def test_run_retrieves_once_per_batch_without_a_counting_pass(self):
        people = [self.person(f'c{i}', f'Person{i}') for i in range(3)]
        store = self.imported(people, {person['member']: {'topics': [f'group{j:03d}' for j in range(105)]}
                                      for person in people})
        discover = Discover(store, self.provider)
        statements = []
        store.conn.set_trace_callback(statements.append)
        with patch.object(discover, '_parents', wraps=discover._parents) as parents:
            report = discover.run(limit=1)
        self.assertEqual(report.calls, 1)
        self.assertTrue(report.has_more)
        self.assertEqual(parents.call_count, 2)
        self.assertEqual(sum(sql.startswith('INSERT INTO discovery_lexical') for sql in statements), 1)



if __name__ == '__main__':
    unittest.main()
