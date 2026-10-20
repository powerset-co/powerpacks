"""Fix-round regressions using synthetic source schemas and the real CLI."""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

from test_memory import FixtureProvider, STORIES, response, source_db
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import SynthesizedFacts, collapse
from packs.memory.__main__ import main
from packs.memory.generate import Generate
from packs.memory.models import Claim, Filters, Kind, date_bound
from packs.memory.projection import fact_claims
from packs.memory.provider import OpenAIProvider
from packs.memory.query import Query
from packs.memory.sources import V2
from packs.memory.store import Store


class RoundTwoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.adapter = V2(self.base / 'source')
        source_db(self.adapter.database, STORIES)
        self.store = Store(self.base / 'memory.sqlite', create=True)
        self.addCleanup(self.store.close)
        self.provider = FixtureProvider()

    def cli(self, *args: str) -> tuple[int, object]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(['--db', str(self.base / 'memory.sqlite'), *args])
        return status, json.loads(output.getvalue())

    def test_free_structured_projection_and_family_semantics(self) -> None:
        conn = sqlite3.connect(self.adapter.database)
        originals = []
        for member, relationship, topic in [('c1', 'A friend.', 'identity access management work'),
                                           ('c1b', 'A close friend from school.', 'identity access management work systems')]:
            payload = json.loads(conn.execute('SELECT facts_json FROM facts WHERE candidate_id=?', (member,)).fetchone()[0])
            payload.update(relationship_to_owner=relationship, topics=[topic], title='Engineer',
                           employers=[{'name': 'Synthetic Corp', 'role': 'Engineer', 'status': 'current'}])
            originals.append(SynthesizedFacts.from_payload(payload))
            conn.execute('UPDATE facts SET facts_json=? WHERE candidate_id=?', (json.dumps(payload), member))
        conn.execute("INSERT INTO person_tags(candidate_id,tags,note,updated_at) VALUES('c1','friend|private','human preference note','now')")
        conn.commit()
        conn.close()
        self.store.import_source(self.adapter)
        lexical = FixtureProvider()
        lexical.embedding_key = ''
        query = Query(self.store, lexical)
        parent = self.store.lookup('Jordan Bravo')[0].parent
        hits = query.run('', filters=Filters(parents=(parent,)), limit=100).hits
        expected = {c.fingerprint() for c in fact_claims(collapse(originals))}
        actual = {hit.claim.fingerprint() for hit in hits if hit.claim.kind != Kind.TAG and hit.claim.topic != 'human note'}
        self.assertEqual(actual, expected)
        self.assertEqual(len(query.run('identity', filters=Filters(kind=Kind.MENTION)).hits), 1)
        self.assertEqual(query.run('', filters=Filters(kind=Kind.WORK)).hits, ())
        self.assertEqual(query.run('', filters=Filters(kind=Kind.LP)).hits, ())
        self.assertEqual(len(query.run('', filters=Filters(kind=Kind.TAG)).hits), 2)
        status, result = self.cli('query', 'Engineer', '--person', 'Jordan Bravo')
        self.assertEqual(status, 0)
        self.assertTrue(result['hits'])
        self.assertTrue(all(hit['citations'] for hit in result['hits']))
        self.assertEqual((self.provider.generations, lexical.embeddings), (0, 0))
        self.store.import_source(self.adapter)
        self.assertEqual(len(query.run('', filters=Filters(parents=(parent,)), limit=100).hits), len(hits))

    def test_imported_correction_survives_source_refresh_without_generation(self) -> None:
        conn = sqlite3.connect(self.adapter.database)
        payload = json.loads(conn.execute("SELECT facts_json FROM facts WHERE candidate_id='c4'").fetchone()[0])
        payload['topics'] = ['IAM']
        conn.execute("UPDATE facts SET facts_json=? WHERE candidate_id='c4'", (json.dumps(payload),))
        conn.commit()
        self.store.import_source(self.adapter)
        lexical = FixtureProvider()
        lexical.embedding_key = ''
        query = Query(self.store, lexical)
        old = query.run('IAM').hits[0]
        replacement = Claim('ACL', Kind.MENTION, 'ACL', '', '', '', '', 'Human correction')
        self.store.correct(old.claim_id, replacement, 'Human corrected topic')
        payload['location'] = 'Synthetic City'
        conn.execute("UPDATE facts SET facts_json=? WHERE candidate_id='c4'", (json.dumps(payload),))
        conn.commit()
        conn.close()
        self.assertEqual(self.store.import_source(self.adapter).changed, 1)
        self.assertEqual(query.run('IAM').hits, ())
        self.assertEqual(len(query.run('ACL').hits), 1)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM corrections').fetchone()[0], 1)
        self.assertIn('IAM', self.store.open(old.claim_id).saved_content)
        self.assertEqual((lexical.generations, lexical.embeddings), (0, 0))

    def test_one_corpus_invariant_and_cli_negative(self) -> None:
        self.store.import_source(self.adapter)
        other = V2(self.base / 'unrelated')
        source_db(other.database, STORIES)
        with self.assertRaisesRegex(ValueError, 'one source corpus'):
            self.store.import_source(other)
        self.assertEqual(self.store.conn.execute('SELECT count(DISTINCT namespace) FROM identities').fetchone()[0], 1)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(main(['--db', str(self.base / 'other.sqlite'), 'import', '--v2-root', str(self.adapter.root),
                                   '--v2-root', str(other.root)]), 1)
        self.assertFalse((self.base / 'other.sqlite').exists())

    def test_batched_import_interrupt_preserves_completed_sources_and_absence(self) -> None:
        stories = [dict(STORIES[0], member=f'c{i}', parent=f'p:{i}') for i in range(260)]
        adapter = V2(self.base / 'many')
        source_db(adapter.database, stories)
        self.store.import_source(adapter)
        original = V2.sources

        def interrupted(instance):
            for i, item in enumerate(original(instance)):
                if i == 150:
                    raise RuntimeError('synthetic enumeration interruption')
                yield replace(item, content=item.content + ' ')

        with patch.object(V2, 'sources', interrupted):
            with self.assertRaises(RuntimeError):
                self.store.import_source(adapter)
        changed = self.store.conn.execute('SELECT count(*) FROM sources s JOIN revisions r ON s.id=r.source_id AND s.hash=r.hash WHERE r.content LIKE ? AND s.present=1', ('% ',)).fetchone()[0]
        self.assertEqual(changed, 150)
        self.assertEqual(self.store.conn.execute('SELECT sum(present) FROM sources').fetchone()[0], 520)
        resumed = self.store.import_source(adapter)
        self.assertEqual(resumed.changed, 150)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM sources').fetchone()[0], 520)
        with patch.object(V2, 'sources', interrupted):
            with self.assertRaises(RuntimeError):
                self.store.import_source(adapter)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM sources WHERE present=0').fetchone()[0], 0)

    def test_shared_profile_once_and_stable_across_changed_child_membership(self) -> None:
        conn = sqlite3.connect(self.adapter.database)
        conn.execute("INSERT INTO candidates VALUES('c0','Synthetic Extra',0,'{}','now')")
        conn.execute("INSERT INTO candidate_parent(candidate_id,parent_id,reason,created_at) VALUES('c0','p:0','human','now')")
        url = 'https://www.linkedin.com/in/synthetic-jordan'
        for member in ('c1', 'c1b'):
            conn.execute("INSERT INTO candidate_parent(candidate_id,parent_id,reason,created_at) VALUES(?,'li:123','human','later')", (member,))
            conn.execute("INSERT INTO candidate_linkedins(candidate_id,linkedin_url,member_id,origin,verdict,decided_by,judgment_fingerprint,reason,created_at) VALUES(?,?,'123','human_override','confirmed','human','x','','now')", (member, url))
        conn.commit()
        conn.close()
        path = self.adapter.root / 'network-import/profile_cache_v2/synthetic-jordan.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'normalized_profile': {'success': True, 'member_id': '123', 'full_name': 'Jordan Bravo',
                        'experiences': [{'title': 'Engineer', 'company_name': 'Synthetic Corp', 'starts_at': {'year': 2020}, 'is_current_position': True}]},
                        'raw_response': {}, 'fetched_at': 'now'}))
        self.store.import_source(self.adapter)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM sources WHERE key LIKE 'profile:%'").fetchone()[0], 1)
        Generate(self.store, self.provider).run(limit=100)
        before = self.store.conn.execute("SELECT id,hash FROM sources WHERE key='profile:li:123'").fetchone()
        conn = sqlite3.connect(self.adapter.database)
        conn.execute("INSERT INTO candidate_parent(candidate_id,parent_id,reason,created_at) VALUES('c0','li:123','human','later')")
        conn.commit()
        conn.close()
        self.assertEqual(self.store.import_source(self.adapter).changed, 0)
        self.assertEqual(tuple(before), tuple(self.store.conn.execute("SELECT id,hash FROM sources WHERE key='profile:li:123'").fetchone()))
        self.assertEqual(Generate(self.store, self.provider).run(limit=100).generation_calls, 0)
        tenures = Query(self.store, self.provider).run('', filters=Filters(kind=Kind.TENURE, object='Synthetic Corp')).hits
        self.assertEqual(len(tenures), 1)
        self.assertEqual((tenures[0].claim.start, tenures[0].claim.end), ('2020', 'present'))

    def test_partial_dates_current_unknown_disjoint_and_invalid(self) -> None:
        stories = []
        for member, start, end in [('a', '2020', 'present'), ('b', '2020-06', '2021'),
                                   ('c', '2010', '2019-12'), ('d', '2021', ''), ('e', '', 'present')]:
            claim = Claim(f'Synthetic {member} worked at Acme.', Kind.TENURE, 'employment', '', 'Acme', start, end, f'Synthetic {member} worked at Acme.')
            stories.append({'member': member, 'parent': member, 'name': f'Synthetic {member}', 'claims': [json.loads(claim.serialized())]})
        adapter = V2(self.base / 'tenures')
        source_db(adapter.database, stories)
        self.store.import_source(adapter)
        Generate(self.store, self.provider).run(limit=100)
        parent = self.store.lookup('Synthetic a')[0].parent
        self.assertEqual({right.name for _, right in Query(self.store, self.provider).overlap(parent)}, {'Synthetic b'})
        self.assertEqual(date_bound('2020', upper=True), '2020-12-31')
        self.assertEqual(date_bound('2020-02', upper=True), '2020-02-29')
        self.assertEqual(Claim.parse(json.loads(Claim('text', Kind.TENURE, 'work', '', 'Acme', '2020-06', '2020', 'text').serialized())).end, '2020')
        for start, end in [('2020-13', ''), ('2021', '2020'), ('present', '')]:
            with self.assertRaises(ValueError):
                Claim.parse(json.loads(Claim('text', Kind.TENURE, 'work', '', '', start, end, 'text').serialized()))

    def test_raw_malformed_incomplete_saved_and_later_chunks_continue(self) -> None:
        self.store.import_source(self.adapter)
        outputs = ['not JSON', response([], 'incomplete'), response(STORIES[2]['claims']), response([]), response([])]
        with patch.object(FixtureProvider, 'generate', side_effect=outputs) as generate:
            report = Generate(self.store, self.provider).run(limit=100)
            self.assertEqual((report.generation_calls, report.exclusions), (5, 2))
            self.assertEqual(self.store.conn.execute("SELECT count(*) FROM chunks WHERE error<>''").fetchone()[0], 2)
            self.assertEqual(Generate(self.store, self.provider).run(limit=100).generation_calls, 0)
            self.assertEqual(generate.call_count, 5)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM active_claims').fetchone()[0], 3)
        saved = self.store.conn.execute('SELECT response FROM chunks ORDER BY id LIMIT 1').fetchone()[0]
        self.assertEqual(saved, 'not JSON')

    def test_total_fresh_limit_includes_embeddings_on_resume(self) -> None:
        self.store.import_source(self.adapter)
        report = Generate(self.store, self.provider).run(limit=1)
        self.assertEqual((report.generation_calls, report.embedding_calls), (1, 0))
        report = Generate(self.store, self.provider).run(limit=1)
        self.assertEqual((report.generation_calls, report.embedding_calls), (0, 1))
        Generate(self.store, self.provider).run(limit=100)
        self.assertEqual(Generate(self.store, self.provider).run(limit=1).generation_calls, 0)

    def test_spend_gate_cli_generation_query_and_cached_free_paths(self) -> None:
        self.store.import_source(self.adapter)
        called = []

        def respond(request):
            called.append(request.url.path)
            if request.url.path.endswith('responses'):
                return httpx.Response(200, json={**json.loads(response([])), "usage": {"input_tokens": 1, "output_tokens": 1}})
            return httpx.Response(200, json={'usage': {'prompt_tokens': 1}, 'data': [{'embedding': [1, 0, 0]}]})

        client = httpx.Client(base_url='https://api.openai.com/v1/', transport=httpx.MockTransport(respond))
        self.addCleanup(client.close)
        import os
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic'}):
            status, payload = self.cli('generate', '--model', 'test', '--embedding-model', 'test', '--limit', '1')
            self.assertEqual((status, payload['status']), (20, 'needs_approval'))
            status, payload = self.cli('query', 'test', '--embedding-model', 'test')
            self.assertEqual((status, payload['status']), (20, 'needs_approval'))
            self.assertEqual(called, [])
            provider = OpenAIProvider('test', 'test', client=client, approve_spend=True)
            Generate(self.store, provider).run(limit=100)
            Query(self.store, provider).run('cached query')
            count = len(called)
            # Same methods, same caches, no approval and no HTTP work on the next run.
            provider.approve_spend = False
            self.assertEqual(Generate(self.store, provider).run(limit=100).generation_calls, 0)
            Query(self.store, provider).run('cached query')
            self.assertEqual(len(called), count)


if __name__ == '__main__':
    unittest.main()
