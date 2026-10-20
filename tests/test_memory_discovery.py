"""Cross-person discovery and correction regressions use synthetic current-schema inputs."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_memory import FixtureProvider, source_db
from packs.memory.models import Claim, Filters, Kind, packed
from packs.memory.query import Query
from packs.memory.sources import V2
from packs.memory.store import Store


class CorrectnessTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.adapter = V2(Path(temp.name) / 'source')
        source_db(self.adapter.database, [{'member': 'c1', 'parent': 'c1', 'name': 'Jordan Bravo', 'claims': []}])
        self.store = Store(Path(temp.name) / 'memory.sqlite', create=True)
        self.addCleanup(self.store.close)
        self.store.import_source(self.adapter)
        self.source = self.store.conn.execute('SELECT id,hash FROM sources LIMIT 1').fetchone()
        self.provider = FixtureProvider()
        self.provider.embedding_key = ''

    def test_shared_quote_distinct_topics_and_scopes_survive_correction(self) -> None:
        first = Claim('Built IAM.', Kind.WORK, 'iam', 'work', '', '', '', 'Built IAM and billing.')
        claims = [first, replace(first, text='Built billing.', topic='billing'),
                  replace(first, text='Prefers email.', kind=Kind.PREFERENCE, topic='communication', scope='email'),
                  replace(first, text='Prefers meetings.', kind=Kind.PREFERENCE, topic='communication', scope='meetings')]
        ids = [self.store.add_claim(*self.source, None, claim, human=False) for claim in claims]
        self.store.conn.commit()
        self.assertEqual(len(Query(self.store, self.provider).run('').hits), 4)
        self.store.correct(ids[0], None, 'Only IAM was wrong')
        self.store.correct(ids[2], None, 'Only email was wrong')
        self.assertEqual({hit.claim.text for hit in Query(self.store, self.provider).run('').hits}, {'Built billing.', 'Prefers meetings.'})

    def test_conflicting_alias_confirmation_rejected_without_poisoning_writes(self) -> None:
        with self.store.conn:
            self.store.conn.executemany('INSERT INTO topics VALUES(?)', [('machine learning',), ('markup language',)])
        self.store.confirm_alias('ml', 'machine learning')
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            self.store.confirm_alias('ml', 'markup language')
        self.assertEqual(self.store.topic('ml'), 'machine learning')
        self.store.import_source(self.adapter)
        self.assertEqual(self.store.conn.execute("SELECT topic FROM aliases WHERE alias='ml' AND confirmed=1").fetchall()[0][0], 'machine learning')

    def test_alias_confirmation_does_not_resurrect_or_duplicate_claims(self) -> None:
        with sqlite3.connect(self.adapter.database) as conn:
            conn.execute("UPDATE facts SET facts_json=json_set(facts_json,'$.topics',json('[\"ML\",\"k8s\"]'))")
        self.store.import_source(self.adapter)
        old = self.store.conn.execute("SELECT id FROM active_claims WHERE topic='ml'").fetchone()[0]
        self.store.correct(old, None, 'Human rejected ML')
        with self.store.conn:
            self.store.conn.executemany('INSERT INTO topics VALUES(?)', [('machine learning',), ('kubernetes',)])
        self.store.confirm_alias('ml', 'machine learning')
        self.store.confirm_alias('k8s', 'kubernetes')
        self.store.import_source(self.adapter)
        query = Query(self.store, self.provider)
        self.assertEqual(query.run('', filters=Filters(topic='machine learning')).hits, ())
        for spelling in ('k8s', 'kubernetes'):
            hits = query.run('', filters=Filters(topic=spelling)).hits
            self.assertEqual([hit.claim.topic for hit in hits], ['k8s'])



def discovery_response(payload: dict) -> str:
    return json.dumps({'status': 'completed', 'output': [{'type': 'message', 'content': [
        {'type': 'output_text', 'text': json.dumps(payload)}]}]})


class DiscoveryFixture:
    discovery_key = 'synthetic-discovery-v1'
    embedding_key = ''

    def __init__(self) -> None:
        self.inputs: list[dict] = []
        self.fail_call = 0

    def discover(self, content: str) -> str:
        self.inputs.append(json.loads(content))
        if len(self.inputs) == self.fail_call:
            raise ValueError('synthetic interruption')
        evidence = self.inputs[-1]['evidence']
        matches = []
        for item in evidence:
            if any(word in item['quote'] for word in ('visited France', 'visited San Francisco', 'owner’s cousin')):
                matches.append({key: item[key] for key in ('parent', 'source_id', 'quote')})
        label = 'French Riviera visitors' if self.inputs[-1]['candidate'] == 'sof' else 'San Francisco association'
        if any('owner’s cousin' in item['quote'] for item in evidence):
            label = 'family'
        payload = {'decision': 'supported' if matches else 'ambiguous', 'reason': 'Checked individual saved evidence',
                   'tags': [{'label': label, 'explanation': label + ' supported by saved evidence', 'aliases': [],
                             'evidence': matches}] if matches else []}
        return discovery_response(payload)


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.adapter = V2(self.base / 'source')
        self.provider = DiscoveryFixture()

    def imported(self, stories: list[dict], fields: dict[str, dict] | None = None) -> Store:
        source_db(self.adapter.database, stories)
        with sqlite3.connect(self.adapter.database) as conn:
            for member, changes in (fields or {}).items():
                payload = json.loads(conn.execute('SELECT facts_json FROM facts WHERE candidate_id=?', (member,)).fetchone()[0])
                payload.update(changes)
                conn.execute('UPDATE facts SET facts_json=? WHERE candidate_id=?', (json.dumps(payload, ensure_ascii=False), member))
        store = Store(self.base / 'memory.sqlite', create=True)
        self.addCleanup(store.close)
        store.import_source(self.adapter)
        return store

    @staticmethod
    def person(member: str, name: str, parent: str = '') -> dict:
        return {'member': member, 'parent': parent or member, 'name': name, 'claims': []}

    def tags(self, store: Store) -> tuple:
        lexical = FixtureProvider()
        lexical.embedding_key = ''
        return tuple(hit for row in store.conn.execute('SELECT DISTINCT parent FROM active_claims')
                     for hit in Query(store, lexical).run('', filters=Filters(parents=(row[0],), kind=Kind.TAG), limit=100).hits)

    def test_original_names_canonical_counts_abbreviations_and_numeric_noise(self) -> None:
        from packs.memory.discovery import Discover
        people = [self.person(f'c{i}', f'Synthetic Name{i} SoF. 2026') for i in range(3)]
        people += [self.person('dup', 'Synthetic Other SoF', 'c2')]
        people += [self.person(f's{i}', f'Synthetic West{i} SF 2026') for i in range(3)]
        fields = {person['member']: {'canonical_name': 'Canonical ' + person['member']} for person in people}
        store = self.imported(people, fields)
        groups = list(Discover(store, self.provider).groups())
        counts = {group.phrase: group.people for group in groups if group.key.startswith('name:')}
        self.assertEqual((counts['sof'], counts['sf']), (3, 3))
        self.assertNotIn('2026', counts)
        self.assertEqual(store.lookup('Canonical c0')[0].name, 'Canonical c0')
        original = store.conn.execute("SELECT r.content,s.citation FROM sources s JOIN revisions r ON r.source_id=s.id AND r.hash=s.hash WHERE s.key='name:c0'").fetchone()
        self.assertEqual(original['content'], 'Synthetic Name0 SoF. 2026')
        self.assertTrue(original['citation'].endswith('#candidates/c0'))
        result = Discover(store, self.provider).run(limit=1, phrase='sof')
        self.assertEqual((result.calls, result.abstentions, result.tags), (1, 1, 0))
        self.assertEqual(self.tags(store), ())
        preview = Discover(store, self.provider).preview(phrase='sof', limit=1)
        self.assertEqual(preview.insights[0].decision, 'ambiguous')
        self.assertEqual(len(preview.insights[0].members), 3)
        self.assertEqual(Discover(store, self.provider).run(limit=1, phrase='sof').calls, 0)

    def test_per_person_checks_unsampled_exclusion_and_independent_abbreviations(self) -> None:
        from packs.memory.discovery import Discover
        people = [self.person(f'c{i:02d}', f'Synthetic West{i} SF') for i in range(17)]
        fields = {person['member']: {'location': 'The person visited San Francisco with the owner.'} for person in people[:-1]}
        store = self.imported(people, fields)
        report = Discover(store, self.provider).run(limit=1, phrase='sf')
        self.assertEqual((report.calls, report.tags), (1, 16))
        self.assertEqual(len(self.tags(store)), 16)
        self.assertNotIn(store.lookup('Synthetic West16 SF')[0].parent, {hit.parent for hit in self.tags(store)})
        again = Discover(store, self.provider).run(limit=3, phrase='sf')
        self.assertEqual((again.calls, again.reused, again.abstentions), (1, 1, 1))
        self.assertEqual(len(self.tags(store)), 16)
        for hit in self.tags(store):
            self.assertEqual(hit.claim.topic, 'san francisco association')
            self.assertEqual(hit.claim.scope, 'association')
            self.assertIn('machine-inferred tag', hit.citations[0].level)
            self.assertIn('visited San Francisco', store.open(hit.claim_id).saved_content)
        self.assertLessEqual(max(len(json.dumps(value, ensure_ascii=False)) for value in self.provider.inputs), 24000)
        self.assertEqual(Discover(store, self.provider).run(limit=3, phrase='sf').calls, 0)

    def test_family_needs_relationship_and_common_surname_abstains(self) -> None:
        from packs.memory.discovery import Discover
        people = [self.person(f'f{i}', f'Synthetic Relative{i} Zeller') for i in range(3)]
        people += [self.person(f'n{i}', f'Synthetic Stranger{i} Chen') for i in range(3)]
        fields = {f'f{i}': {'relationship_to_owner': 'This person is the owner’s cousin.'} for i in range(3)}
        store = self.imported(people, fields)
        self.assertEqual(Discover(store, self.provider).run(limit=1, phrase='chen').abstentions, 1)
        self.assertEqual(Discover(store, self.provider).run(limit=1, phrase='zeller').tags, 3)
        self.assertEqual({hit.claim.topic for hit in self.tags(store)}, {'family'})

    def test_corrections_edits_removals_merges_and_no_generated_evidence(self) -> None:
        from packs.memory.discovery import Discover
        people = [self.person(f'c{i}', f'Synthetic South{i} SoF') for i in range(4)]
        fields = {person['member']: {'location': 'This person visited France with the owner.'} for person in people}
        store = self.imported(people, fields)
        self.assertEqual(Discover(store, self.provider).run(limit=5, phrase='sof').calls, 1)
        first = self.tags(store)[0]
        store.correct(first.claim_id, None, 'Human rejected this association')
        self.assertEqual(Discover(store, self.provider).run(limit=5, phrase='sof').calls, 0)
        self.assertEqual(len(self.tags(store)), 3)
        with sqlite3.connect(self.adapter.database) as conn:
            conn.execute("UPDATE facts SET facts_json=json_set(facts_json,'$.location','No geographic evidence.') WHERE candidate_id='c1'")
        store.import_source(self.adapter)
        self.assertEqual(Discover(store, self.provider).run(limit=5, phrase='sof').calls, 1)
        self.assertEqual(len(self.tags(store)), 2)
        with sqlite3.connect(self.adapter.database) as conn:
            conn.execute("DELETE FROM candidate_parent WHERE candidate_id='c2'")
        store.import_source(self.adapter)
        self.assertEqual(Discover(store, self.provider).run(limit=5, phrase='sof').calls, 1)
        self.assertEqual(len(self.tags(store)), 1)
        with sqlite3.connect(self.adapter.database) as conn:
            conn.execute("INSERT INTO candidate_parent(candidate_id,parent_id,reason,created_at) VALUES('c3','c0','human','later')")
        store.import_source(self.adapter)
        self.assertEqual(Discover(store, self.provider).run(limit=5, phrase='sof').calls, 0)
        self.assertEqual(self.tags(store), ())  # fewer than three canonical people
        self.assertEqual(store.conn.execute('SELECT count(*) FROM corrections').fetchone()[0], 1)
        for payload in self.provider.inputs:
            self.assertTrue(all('supported by saved evidence' not in item['quote'] for item in payload['evidence']))

    def test_cached_semantic_paraphrases_without_shared_labels_and_label_reuse(self) -> None:
        from packs.memory.discovery import Discover
        from packs.memory.generate import embedded
        from packs.memory.models import document
        texts = ['permission architecture'] * 3 + ['identity governance', 'access control engineering']
        people = [self.person(f'c{i}', f'Synthetic Person{i}') for i in range(len(texts))]
        fields = {f'c{i}': {'topics': [text]} for i, text in enumerate(texts)}
        store = self.imported(people, fields)
        vector_provider = FixtureProvider()
        for row in store.conn.execute('SELECT payload FROM active_claims'):
            claim = Claim.parse(json.loads(row[0]))
            with patch.object(FixtureProvider, 'embed', return_value=(1.0, 0.0, 0.0)):
                embedded(store, vector_provider, document(claim))
        self.provider.embedding_key = vector_provider.embedding_key
        groups = list(Discover(store, self.provider).groups())
        self.assertEqual({group.phrase for group in groups if group.seed}, {texts[0]})
        group = next(group for group in groups if group.seed)
        preview = Discover(store, self.provider).preview(phrase=group.phrase, limit=1)
        self.assertEqual((preview.pending_calls, preview.groups[0].people), (1, 3))
        self.assertEqual(self.provider.inputs, [])
        response = {'decision': 'supported', 'reason': 'Shared access-controls experience', 'tags': []}

        def semantic(content: str) -> str:
            payload = json.loads(content)
            self.assertEqual({item['parent'] for item in payload['evidence']}, {store.lookup(person['name'])[0].parent for person in people})
            self.assertTrue(any(text in item['quote'] for text in texts for item in payload['evidence']))
            supports = [{key: item[key] for key in ('parent', 'source_id', 'quote')}
                        for item in payload['evidence'] if not item['quote'].startswith('Synthetic')]
            response['tags'] = [{'label': 'access controls', 'explanation': 'Shared access-controls experience', 'aliases': ['IAM'], 'evidence': supports}]
            return discovery_response(response)

        with patch.object(self.provider, 'discover', side_effect=semantic):
            self.assertEqual(Discover(store, self.provider).run(limit=1, phrase=group.phrase).tags, 5)
            self.assertEqual(Discover(store, self.provider).run(limit=1, phrase=group.phrase).calls, 0)
        self.assertEqual({hit.claim.topic for hit in self.tags(store)}, {'access controls'})
        self.assertEqual(store.conn.execute("SELECT confirmed FROM aliases WHERE alias='iam'").fetchone()[0], 0)

    def test_outside_parent_or_quote_rejects_entire_response(self) -> None:
        from packs.memory.discovery import Discover
        people = [self.person(f'c{i}', f'Synthetic West{i} SF') for i in range(3)]
        fields = {person['member']: {'location': 'This person visited San Francisco.'} for person in people}
        store = self.imported(people, fields)

        def unsupported(content: str) -> str:
            payload = json.loads(content)
            item = next(item for item in payload['evidence'] if 'visited San Francisco' in item['quote'])
            valid = {key: item[key] for key in ('parent', 'source_id', 'quote')}
            invalid = dict(valid, parent='outside the batch')
            return discovery_response({'decision': 'supported', 'reason': 'Synthetic validation failure', 'tags': [
                {'label': 'association', 'explanation': 'Must not partially apply', 'aliases': [], 'evidence': [valid, invalid]}]})

        with patch.object(self.provider, 'discover', side_effect=unsupported):
            report = Discover(store, self.provider).run(limit=1, phrase='sf')
        self.assertEqual(report.exclusions, 1)
        self.assertEqual(report.failures, ())
        self.assertEqual(self.tags(store), ())
        self.assertEqual(Discover(store, self.provider).preview(phrase='sf').insights, ())

    def test_bad_response_and_interruption_keep_checkpoints_without_partial_tags(self) -> None:
        from packs.memory.discovery import Discover
        people = [self.person(f'c{i:02d}', f'Synthetic West{i} SF') for i in range(20)]
        fields = {person['member']: {'location': 'The person visited San Francisco.'} for person in people}
        store = self.imported(people, fields)
        self.provider.fail_call = 2
        events = list(Discover(store, self.provider).progress(limit=5, phrase='sf'))
        self.assertTrue(any('interruption' in event.error for event in events))
        self.assertEqual(store.conn.execute('SELECT count(*) FROM chunks WHERE position=-1').fetchone()[0], 1)
        self.provider.fail_call = 0
        self.assertEqual(Discover(store, self.provider).run(limit=5, phrase='sf').calls, 1)
        with patch.object(self.provider, 'discover', return_value='bad JSON'):
            self.provider.discovery_key = 'new-key'
            report = Discover(store, self.provider).run(limit=5, phrase='sf')
            self.assertEqual((report.calls, report.exclusions), (2, 2))
            self.assertEqual(Discover(store, self.provider).run(limit=5, phrase='sf').calls, 0)
        self.assertEqual(self.tags(store), ())

    def test_lexical_distinctive_shared_passages_nominate_without_vectors(self) -> None:
        from packs.memory.discovery import Discover
        texts = ['speleology alpine expedition'] * 3 + ['speleology advanced expedition', 'alpine expedition planning']
        people = [self.person(f'c{i}', f'Synthetic{i}') for i in range(len(texts))]
        store = self.imported(people, {f'c{i}': {'topics': [text] + [f'decoy{i} topic{j}' for j in range(12)]} for i, text in enumerate(texts)})
        discover = Discover(store, self.provider)
        groups = list(discover.groups(phrase=texts[0]))
        self.assertEqual([(group.phrase, group.people) for group in groups], [(texts[0], 3)])
        self.assertEqual(len(list(discover._parents(groups[0]))), 5)

    def test_retrieved_paraphrase_after_fourteen_decoys_is_supplied(self) -> None:
        from packs.memory.discovery import Discover
        from packs.memory.models import document
        from packs.ingestion.primitives.common.jsonio import short_hash
        texts = ['permission architecture'] * 3 + ['identity governance', 'access control engineering']
        people = [self.person(f'c{i}', f'Synthetic{i}') for i in range(len(texts))]
        fields = {f'c{i}': {'topics': [f'Unrelated decoy topic {i} {j}' for j in range(14)] + [text]}
                  for i, text in enumerate(texts)}
        store = self.imported(people, fields)
        self.provider.embedding_key = 'synthetic-cache'
        with store.conn:
            for row in store.conn.execute('SELECT payload FROM active_claims'):
                claim = Claim.parse(json.loads(row[0]))
                if claim.topic in texts:
                    store.conn.execute('INSERT OR IGNORE INTO embeddings VALUES(?,?,?)',
                                       ('synthetic-cache', short_hash(document(claim), n=64), packed([1., 0., 0.])))
        discover = Discover(store, self.provider)
        group = next(discover.groups(phrase=texts[0]))
        content, _, _ = discover._batch(group, list(discover._parents(group)))
        evidence = json.loads(content)['evidence']
        for person, text in zip(people, texts):
            parent = store.lookup(person['name'])[0].parent
            self.assertTrue(any(text in item['quote'] for item in evidence if item['parent'] == parent), text)

    def test_cosine_shortlist_selects_highest_ranked_neighbors(self) -> None:
        import math
        from packs.memory.discovery import Discover
        from packs.memory.models import document
        from packs.ingestion.primitives.common.jsonio import short_hash
        people = [self.person(f'c{i:02d}', f'Synthetic{i}') for i in range(12)]
        store = self.imported(people, {person['member']: {'topics': [f'uniqueconcept{0 if i < 3 else i}']} for i, person in enumerate(people)})
        self.provider.embedding_key = 'synthetic-cache'
        with store.conn:
            for row in store.conn.execute('SELECT c.member,a.payload FROM active_claims a JOIN sources c ON c.id=a.source_id'):
                number = int(row['member'][1:])
                score = 1.0 if number == 0 else 0.35 + number * 0.05
                vector = packed([score, math.sqrt(1 - score * score), 0.0])
                claim = Claim.parse(json.loads(row['payload']))
                store.conn.execute('INSERT OR IGNORE INTO embeddings VALUES(?,?,?)', ('synthetic-cache', short_hash(document(claim), n=64), vector))
        discover = Discover(store, self.provider)
        group = next(discover.groups(phrase='uniqueconcept0'))
        matches = list(discover._parents(group))
        self.assertEqual([match.parent.rsplit('#', 1)[-1] for match in matches], ['c00', 'c01', 'c02', 'c11', 'c10', 'c09', 'c08', 'c07', 'c06', 'c05', 'c04'])

    def test_unrelated_random_vectors_and_common_boilerplate_do_not_nominate(self) -> None:
        import random
        from packs.memory.discovery import Discover
        from packs.memory.models import document
        from packs.memory.models import vector
        from packs.ingestion.primitives.common.jsonio import short_hash
        people = [self.person(f'c{i:03d}', f'Synthetic{i}') for i in range(300)]
        store = self.imported(people, {person['member']: {'topics': [f'Contact profile notes person{i} topic{j}' for j in range(5)]}
                                      for i, person in enumerate(people)})
        self.provider.embedding_key = 'synthetic-cache'
        random_values = random.Random(2026)
        with store.conn:
            for row in store.conn.execute('SELECT payload FROM active_claims'):
                claim = Claim.parse(json.loads(row[0]))
                values = vector([random_values.gauss(0, 1) for _ in range(256)])
                store.conn.execute('INSERT INTO embeddings VALUES(?,?,?)', ('synthetic-cache', short_hash(document(claim), n=64), packed(values)))
        self.assertEqual(list(Discover(store, self.provider).groups()), [])
        self.assertEqual(self.provider.inputs, [])

    def test_escaped_quotes_backslashes_and_newlines_keep_saved_provenance(self) -> None:
        from packs.memory.discovery import Discover
        text = 'Expedition "cave diving" at C:\\trips\nShared with the owner.'
        people = [self.person(f'c{i}', f'Synthetic{i} Expedition') for i in range(3)]
        store = self.imported(people, {person['member']: {'location': text} for person in people})
        discover = Discover(store, self.provider)
        group = next(discover.groups(phrase='expedition'))
        content, _, evidence = discover._batch(group, list(discover._parents(group)))
        escaped = json.dumps(text, ensure_ascii=False)[1:-1]
        saved = [item for item in evidence if escaped in item.quote]
        self.assertEqual(len(saved), 3)
        for item in saved:
            original = store.conn.execute('SELECT content FROM revisions WHERE source_id=? AND hash=?', (item.source_id, item.source_hash)).fetchone()[0]
            self.assertIn(item.quote, original)
        self.assertIn('cave diving', content)
        def supported(content: str) -> str:
            spans = json.loads(content)['evidence']
            supports = [{'parent': item['parent'], 'source_id': item['source_id'], 'quote': escaped}
                        for item in spans if escaped in item['quote']]
            return discovery_response({'decision': 'supported', 'reason': 'Explicit expedition evidence', 'tags': [
                {'label': 'cave diving expedition', 'explanation': 'Shared expedition association', 'aliases': [], 'evidence': supports}]})

        with patch.object(self.provider, 'discover', side_effect=supported):
            self.assertEqual(discover.run(limit=1, phrase='expedition').tags, 3)
        for hit in self.tags(store):
            self.assertEqual(hit.citations[0].quote, escaped)
            self.assertIn(hit.citations[0].quote, store.open(hit.claim_id).saved_content)

    def test_high_cosine_paraphrases_without_exact_sign_band_collision(self) -> None:
        from packs.memory.discovery import Discover
        from packs.memory.models import document
        from packs.memory.models import vector
        from packs.ingestion.primitives.common.jsonio import short_hash
        people = [self.person(f'c{i}', f'Synthetic{i}') for i in range(5)]
        store = self.imported(people, {person['member']: {'topics': [f'uniqueseed{0 if i < 3 else i}']} for i, person in enumerate(people)})
        self.provider.embedding_key = 'synthetic-cache'
        values = []
        for number in range(5):
            raw = [0.01] * 32 + [1.0]
            if number >= 3:
                for index in range((number - 3) * 4, (number - 2) * 4):
                    raw[index] = -0.01
            values.append(vector(raw))
        for other in values[3:]:
            self.assertGreater(sum(a * b for a, b in zip(values[0], other)), 0.99)
            self.assertFalse(any(all((a > 0) == (b > 0) for a, b in zip(values[0][band::4][:8], other[band::4][:8]))
                                 for band in range(4)))
        with store.conn:
            for row in store.conn.execute('SELECT c.member,a.payload FROM active_claims a JOIN sources c ON c.id=a.source_id'):
                claim = Claim.parse(json.loads(row['payload']))
                store.conn.execute('INSERT OR IGNORE INTO embeddings VALUES(?,?,?)',
                                   ('synthetic-cache', short_hash(document(claim), n=64), packed(values[int(row['member'][1:])])) )
        discover = Discover(store, self.provider)
        groups = list(discover.groups(phrase='uniqueseed0'))
        self.assertEqual([group.people for group in groups], [3])
        self.assertEqual(len(list(discover._parents(groups[0]))), 5)

    def test_changed_explanation_and_quote_respect_removal_replacement_and_merge(self) -> None:
        from packs.memory.discovery import Discover
        people = [self.person(f'c{i}', f'Synthetic South{i} SoF') for i in range(4)]
        fields = {person['member']: {'location': 'This person visited France with the owner.'} for person in people}
        store = self.imported(people, fields)
        calls = 0

        def rephrased(content: str) -> str:
            nonlocal calls
            calls += 1
            quote = 'This person visited France with the owner.' if calls == 1 else 'visited France'
            explanation = 'A shared France travel association.' if calls == 1 else 'A France connection recorded in saved evidence.'
            label = 'France association' if calls == 1 else ' FRANCE ASSOCIATION '
            supports = [{'parent': item['parent'], 'source_id': item['source_id'], 'quote': quote}
                        for item in json.loads(content)['evidence'] if quote in item['quote']]
            return discovery_response({'decision': 'supported', 'reason': 'Recorded travel context', 'tags': [
                {'label': label, 'explanation': explanation, 'aliases': [], 'evidence': supports}]})

        with patch.object(self.provider, 'discover', side_effect=rephrased):
            Discover(store, self.provider).run(limit=5, phrase='sof')
            first = {hit.parent.rsplit('#', 1)[-1]: hit for hit in self.tags(store)}
            store.correct(first['c0'].claim_id, None, 'Human removed this inferred label')
            replacement = replace(first['c1'].claim, text='Human corrected association.', topic='travel contact', quote='Human correction')
            replacement_id = store.correct(first['c1'].claim_id, replacement, 'Human changed the inferred label')
            with sqlite3.connect(self.adapter.database) as conn:
                conn.execute("UPDATE facts SET facts_json=json_set(facts_json,'$.topics',json('[\"cycling\"]')) WHERE candidate_id='c3'")
            store.import_source(self.adapter)
            self.assertEqual(Discover(store, self.provider).run(limit=5, phrase='sof').calls, 1)
            france = {hit.parent.rsplit('#', 1)[-1] for hit in self.tags(store) if hit.claim.topic == 'france association'}
            self.assertEqual(france, {'c2', 'c3'})
            self.assertIn(replacement_id, {hit.claim_id for hit in self.tags(store)})
            with sqlite3.connect(self.adapter.database) as conn:
                conn.execute("INSERT INTO candidate_parent(candidate_id,parent_id,reason,created_at) VALUES('c0','c2','human','later')")
            store.import_source(self.adapter)
            self.assertEqual(Discover(store, self.provider).run(limit=5, phrase='sof').calls, 1)
            france = {hit.parent.rsplit('#', 1)[-1] for hit in self.tags(store) if hit.claim.topic == 'france association'}
            self.assertEqual(france, {'c3'})
            self.assertIn(replacement_id, {hit.claim_id for hit in self.tags(store)})
            self.assertEqual(Discover(store, self.provider).run(limit=5, phrase='sof').calls, 0)
        self.assertEqual(store.conn.execute('SELECT count(*) FROM corrections').fetchone()[0], 2)
