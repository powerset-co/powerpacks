"""Final fix-round regressions; all sources, responses and embeddings are synthetic."""
from __future__ import annotations

import math
import os
import sqlite3
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

import httpx

from test_memory import FixtureProvider, response, source_db
from packs.memory.generate import Generate, embedded
from packs.memory.models import document
from packs.memory.models import Claim, Filters, Kind
from packs.memory.provider import OpenAIProvider
from packs.memory.query import Query
from packs.memory.sources import V2
from packs.memory.store import Store


class RoundThreeTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.adapter = V2(self.base / 'source')
        self.quote = 'A close friend of the owner for ten years.'
        self.friend = Claim(self.quote, Kind.RELATIONSHIP, 'relationship', 'owner', '', '', '', self.quote)
        self.paraphrase = replace(self.friend, text='Jordan Bravo is described as a close friend of the owner for ten years.',
                                  topic='friendship', scope='With the owner; ten years')
        stories = [{'member': member, 'parent': member, 'name': name, 'claims': []}
                   for member, name in [('c1', 'Jordan Bravo'), ('c2', 'Casey Delta')]]
        source_db(self.adapter.database, stories)
        self.source = sqlite3.connect(self.adapter.database)
        self.addCleanup(self.source.close)
        self.source.execute("UPDATE facts SET facts_json=json_set(facts_json,'$.relationship_to_owner',?)", (self.quote,))
        self.source.commit()
        self.store = Store(self.base / 'memory.sqlite', create=True)
        self.addCleanup(self.store.close)
        self.store.import_source(self.adapter)
        self.provider = FixtureProvider()

    def generated(self, claims: list[Claim]) -> None:
        with patch.object(FixtureProvider, 'generate', return_value=response([asdict(claim) for claim in claims])):
            Generate(self.store, self.provider).run(limit=100)

    def test_shared_quote_dedup_correction_replay_refresh_and_merge(self) -> None:
        self.generated([self.paraphrase])
        query = Query(self.store, self.provider)
        parent = self.store.lookup('Jordan Bravo')[0].parent
        before = query.run('', filters=Filters(parents=(parent,), kind=Kind.RELATIONSHIP)).hits
        self.assertEqual(len(before), 1)
        for text in ('', 'friend'):
            filtered = query.run(text, filters=Filters(parents=(parent,), topic='relationship')).hits
            self.assertEqual([hit.claim.topic for hit in filtered], ['relationship'])
        old = before[0]
        replacement = replace(self.friend, text='Jordan Bravo is a former colleague.', topic='colleague', quote='Human correction')
        replacement_id = self.store.correct(old.claim_id, replacement, 'Human corrected relationship')
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM lexical WHERE lexical MATCH 'friend'").fetchone()[0], 1)
        # The other person still has its authoritative projected relationship. A canonical merge must apply the decision to them too.
        self.source.execute("INSERT INTO candidate_parent(candidate_id,parent_id,reason,created_at) VALUES('c2','c1','human','later')")
        self.source.commit()
        self.store.import_source(self.adapter)
        self.assertEqual(query.run('friend', filters=Filters(parents=(parent,))).lexical_candidates, 0)
        self.source.execute("UPDATE facts SET facts_json=json_set(facts_json,'$.location','Synthetic City') WHERE candidate_id='c1'")
        self.source.commit()
        self.store.import_source(self.adapter)
        self.generated([self.paraphrase])
        # Replay both the unchanged and newly saved responses without buying work.
        self.assertEqual(Generate(self.store, self.provider).run(limit=100).generation_calls, 0)
        result = query.run('friend', filters=Filters(kind=Kind.RELATIONSHIP))
        self.assertEqual(result.lexical_candidates, 0)
        self.assertEqual([hit.claim_id for hit in result.hits], [replacement_id])
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM corrections').fetchone()[0], 1)
        self.assertEqual(self.store.open(old.claim_id).claim.text, self.quote)
        self.assertTrue(query.run('Synthetic', filters=Filters(kind=Kind.STATEMENT)).hits)

    def test_same_quote_preserves_distinct_objects_dates_kinds_and_empty_quotes(self) -> None:
        source = self.store.conn.execute('SELECT id,hash FROM sources LIMIT 1').fetchone()
        shared = 'LP in Cedar Fund I and Cedar Fund II.'
        first = Claim('LP in Cedar Fund I.', Kind.LP, 'LP', '', 'Cedar Fund I', '', '', shared)
        distinct = [replace(first, text='LP in Cedar Fund II.', object='Cedar Fund II'),
                    replace(first, start='2020', end='2021'), replace(first, kind=Kind.MENTION),
                    replace(first, text='Human fact one.', quote=''), replace(first, text='Human fact two.', quote='')]
        for claim in [first, replace(first, text='Another rendering of Fund I.', topic='investment'), *distinct]:
            self.store.add_claim(source[0], source[1], None, claim, human=False)
        self.store.conn.commit()
        query = Query(self.store, self.provider)
        hits = query.run('', filters=Filters(kind=Kind.LP)).hits
        self.assertEqual(len(hits), 6)
        old = next(hit for hit in hits if hit.claim.object == 'Cedar Fund I' and not hit.claim.start and hit.claim.quote)
        self.store.correct(old.claim_id, None, 'Retracted Fund I only')
        self.assertEqual(len(query.run('', filters=Filters(kind=Kind.LP)).hits), 5)
        self.assertEqual(len(query.run('', filters=Filters(kind=Kind.MENTION)).hits), 1)

    def test_confirm_alias_after_both_topics_exist_replay_and_correct(self) -> None:
        self.source.execute("UPDATE facts SET facts_json=json_set(facts_json,'$.topics',json('[\"k8s\",\"kubernetes\"]')) WHERE candidate_id='c1'")
        self.source.commit()
        self.store.import_source(self.adapter)
        self.generated([])
        self.store.confirm_alias('k8s', 'kubernetes')
        self.assertEqual(self.store.topic('k8s'), 'kubernetes')
        query = Query(self.store, self.provider)
        for spelling in ('k8s', 'kubernetes'):
            self.assertEqual({hit.claim.text for hit in query.run('', filters=Filters(topic=spelling)).hits}, {'k8s', 'kubernetes'})
        self.store.import_source(self.adapter)
        self.assertEqual(Generate(self.store, self.provider).run(limit=100).generation_calls, 0)
        self.source.execute("INSERT INTO candidate_parent(candidate_id,parent_id,reason,created_at) VALUES('c2','c1','human','later')")
        self.source.execute("UPDATE facts SET facts_json=json_set(facts_json,'$.title','Synthetic role') WHERE candidate_id='c1'")
        self.source.commit()
        self.store.import_source(self.adapter)
        Generate(self.store, self.provider).run(limit=100)
        hits = query.run('', filters=Filters(topic='k8s')).hits
        self.assertEqual(len(hits), 2)
        old = next(hit for hit in hits if hit.claim.text == 'k8s')
        self.store.correct(old.claim_id, replace(old.claim, text='Corrected Kubernetes mention.'), 'Human correction')
        self.store.import_source(self.adapter)
        Generate(self.store, self.provider).run(limit=100)
        for spelling in ('k8s', 'kubernetes'):
            texts = {hit.claim.text for hit in query.run('', filters=Filters(topic=spelling)).hits}
            self.assertEqual(texts, {'kubernetes', 'Corrected Kubernetes mention.'})
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            self.store.confirm_alias('k8s', 'relationship')
        self.assertEqual(self.store.topic('k8s'), 'kubernetes')

    def test_embedding_normalized_only_at_provider_boundary(self) -> None:
        client = httpx.Client(base_url='https://api.openai.com/v1/', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={'data': [{'embedding': [1, 2, 3]}]})))
        self.addCleanup(client.close)
        provider = OpenAIProvider('synthetic', 'synthetic', client=client, approve_spend=True)
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic'}), patch('packs.memory.models.math.sqrt', wraps=math.sqrt) as norm:
            wanted = embedded(self.store, provider, document(self.friend))
            self.assertEqual(embedded(self.store, provider, document(self.friend)), wanted)
            Query(self.store, provider).run('friend')
            self.assertEqual(norm.call_count, 2)  # one document and one query HTTP response

    def test_store_results_are_dataclasses_with_cli_payload_shape(self) -> None:
        match = self.store.lookup('Jordan Bravo')[0]
        self.assertEqual(match.name, 'Jordan Bravo')
        source = next(self.store.sources())
        self.assertIsInstance(source.id, int)
        self.assertTrue(source.hash)
        self.assertIn(self.quote, source.content)
        hit = Query(self.store, self.provider).run('').hits[0]
        opened = self.store.open(hit.claim_id)
        self.assertIsInstance(opened.claim, Claim)
        self.assertFalse(opened.original_message_citation_verified)
        self.assertEqual(asdict(opened)['claim']['quote'], self.quote)


if __name__ == '__main__':
    unittest.main()
