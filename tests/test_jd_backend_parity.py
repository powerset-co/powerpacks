from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import duckdb
from turbopuffer.types import Row

PRIMITIVES = Path(__file__).resolve().parents[1] / 'packs/search/primitives'
for directory in ['lib', 'shared', 'local', 'turbopuffer']:
    sys.path.insert(0, str(PRIMITIVES / directory))

from local_duckdb_store import LocalDuckDBSearchStore
import turbopuffer_search_backend as remote


class JDBackendParityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db = Path(self.tmp.name) / 'search.duckdb'
        with duckdb.connect(str(db)) as conn:
            conn.execute('''
                create table local_people_positions (
                    id varchar, position_id varchar, base_id varchar,
                    person_id varchar, position_title varchar, city varchar
                );
                insert into local_people_positions values
                    ('a', 'a', 'person-a', 'person-a', 'Engineer', 'SF'),
                    ('b', 'b', 'person-b', 'person-b', 'Engineer', 'SF'),
                    ('c', 'c', 'person-c', 'person-c', 'Engineer', 'NY');
                create table local_job_descriptions (
                    id varchar, vector double[], word_tokens varchar[], tech_skills varchar[]
                );
                insert into local_job_descriptions values
                    ('jd-ineligible', [1, 0], ['backend'], ['go']),
                    ('jd-a', [0.9, 0.435889894], ['backend'], ['go']),
                    ('jd-b', [0.8, 0.6], [], []);
                create table local_job_description_positions (
                    id varchar, job_description_id varchar, position_id varchar,
                    person_id varchar, match_score double, match_type varchar,
                    posting_position_gap_days bigint
                );
                insert into local_job_description_positions values
                    ('map-a', 'jd-a', 'a', 'person-a', 0.2, 'title_overlap', 0),
                    ('map-b', 'jd-b', 'b', 'person-b', 1, 'title_exact', 0),
                    ('map-c', 'jd-ineligible', 'c', 'person-c', 1, 'title_exact', 0);
            ''')
        self.store = LocalDuckDBSearchStore(str(db))
        self.addCleanup(self.store.conn.close)
        self.payload = {'job_description': 'Build backend services', 'query_embedding': [1, 0]}
        self.filters = ('city', 'Eq', 'SF')
        self.attrs = ['base_id', 'person_id', 'position_title', 'city']
        self.events = []

    def _remote(self, top_k):
        owner = self

        class Namespace:
            def __init__(self, name):
                self.name = name

            def exists(self):
                return True

            def query(self, **kwargs):
                owner.events.append(self.name)
                response = owner.store.fork().query_namespace(
                    self.name, kwargs.get('rank_by'), kwargs.get('filters'),
                    kwargs['top_k'], kwargs.get('include_attributes') or [],
                )
                rows = []
                for row in response.rows:
                    fields = {'id': row.id, **row.model_extra}
                    if kwargs['rank_by'][1] == 'kNN':
                        fields['$dist'] = 1 - fields.pop('score')
                    rows.append(Row.from_dict(fields))
                return SimpleNamespace(rows=rows)

            def multi_query(self, **kwargs):
                return SimpleNamespace(results=[self.query(**query) for query in kwargs['queries']])

        def matches(position_ids):
            owner.events.append('mappings')
            cur = owner.store.conn.execute(
                'select * from local_job_description_positions where position_id in (select unnest(?))',
                [position_ids],
            )
            columns = [col[0] for col in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

        with patch.object(remote, 'namespace', side_effect=Namespace), \
             patch.object(remote, 'fetch_job_description_positions', side_effect=matches), \
             patch.object(remote, 'BASE_ID_BATCH_SIZE', 1):
            return asyncio.run(remote.job_description_rows(
                self.payload, self.filters, top_k=top_k, include_attributes=self.attrs,
            ))

    def test_eligibility_precedes_jd_top_k_on_both_backends(self):
        local = self.store.job_description_rows(self.payload, self.filters, 1, self.attrs)
        remote_rows = self._remote(1)
        self.assertEqual([r['person_id'] for r in local], ['person-a'])
        self.assertEqual([r['person_id'] for r in remote_rows], ['person-a'])
        self.assertLess(self.events.index('people'), self.events.index('job_descriptions'))

    def test_mapping_score_orders_people_identically_without_bm25_or_skill_gate(self):
        self.payload['bm25_queries'] = ['backend']
        self.payload['tech_skills'] = ['go']
        local = self.store.job_description_rows(self.payload, self.filters, 2, self.attrs)
        remote_rows = self._remote(2)
        self.assertEqual([r['person_id'] for r in local], ['person-b', 'person-a'])
        self.assertEqual([r['person_id'] for r in remote_rows], ['person-b', 'person-a'])
        for left, right in zip(local, remote_rows):
            self.assertAlmostEqual(left['score'], right['score'])
        self.assertAlmostEqual(local[0]['score'], 0.8)

    def test_no_jd_does_not_query_remote_or_return_local_evidence(self):
        self.payload = {'semantic_query': 'backend', 'query_embedding': [1, 0]}
        self.assertEqual(self.store.job_description_rows(self.payload, self.filters, 2, self.attrs), [])
        self.assertEqual(self._remote(2), [])
        self.assertEqual(self.events, [])
