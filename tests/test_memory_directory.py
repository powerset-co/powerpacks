"""Synthetic directory and native action contracts; no network or private stores."""
from __future__ import annotations

import contextlib
import io
import json
import os

import httpx
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from test_memory import FixtureProvider, source_db
from packs.ingestion.primitives.common.jsonio import short_hash
from packs.ingestion.primitives.deep_context_v2.db.store import open_store
from packs.memory.__main__ import main
from packs.memory.actions import act
from packs.memory.directory import Directory, Origin, Selection, Selector
from packs.memory.generate import Generate
from packs.memory.models import Claim, Kind, packed
from packs.memory.provider import NeedsApproval
from packs.memory.projection import project
from packs.memory.sources import V2, readonly
from packs.memory.store import Store


class DirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.adapter = V2(self.base / 'source')
        stories = [{'member': f'c{i:03}', 'parent': f'p{i:03}', 'name': f'Jordan Synthetic{i} SoF', 'claims': []} for i in range(137)]
        stories.extend([
            {'member': 'duplicate', 'parent': 'p000', 'name': 'Jordan Alternative SoF', 'claims': []},
            {'member': 'sf', 'parent': 'sf', 'name': 'Casey Synthetic SF', 'claims': []},
            {'member': 'hidden', 'parent': 'hidden', 'name': 'Hidden Synthetic SoF', 'claims': []},
        ])
        source_db(self.adapter.database, stories)
        with contextlib.closing(open_store(self.adapter.database)) as conn:
            conn.execute("INSERT INTO deleted_parents VALUES('hidden','now')")
            conn.execute("INSERT INTO person_tags(candidate_id,tags,note,updated_at) VALUES('c000','mentor|SoF','human note','now')")
            conn.execute("INSERT INTO person_tags(candidate_id,tags,note,updated_at) VALUES('duplicate','SoF','duplicate note','now')")
            conn.execute("UPDATE facts SET facts_json=json_set(facts_json,'$.location','South of France') WHERE candidate_id='c001'")
            conn.execute("UPDATE facts SET facts_json=json_set(facts_json,'$.location','London') WHERE candidate_id='c002'")
            conn.commit()
        self.store = Store(self.base / 'memory.sqlite', create=True)
        self.addCleanup(self.store.close)
        self.store.import_source(self.adapter)
        self.directory = Directory(self.store)

    def test_exact_origins_counts_pages_and_hidden(self) -> None:
        with patch('httpx.HTTPTransport.handle_request', side_effect=AssertionError('unsolicited API')):
            meanings = self.directory.resolve('SoF')
            by_origin = {meaning.selector.origin: meaning for meaning in meanings}
            self.assertEqual(by_origin[Origin.SOURCE_NAME].count, 137)
            self.assertEqual(by_origin[Origin.HUMAN_TAG].count, 1)
            self.assertNotIn(Origin.TOPIC, by_origin)
            selector = by_origin[Origin.SOURCE_NAME].selector
            after, people = '', []
            while True:
                page = self.directory.enumerate(selector, limit=23, after=after)
                self.assertEqual(page.total, 137)
                people.extend(page.people)
                if not page.next_after:
                    break
                after = page.next_after
            self.assertEqual(len({person.parent for person in people}), 137)
            self.assertEqual([person.parent for person in people], sorted(person.parent for person in people))
            self.assertEqual(people[0].members, ('c000', 'duplicate'))
            self.assertTrue(all(person.evidence for person in people))
            self.assertEqual(self.directory.resolve('SF')[0].count, 1)
            with contextlib.closing(readonly(self.adapter.database)) as conn:
                self.assertIsNone(conn.execute("SELECT 1 FROM current_parent WHERE parent_id='hidden'").fetchone())

    def test_geographic_value_never_enumerates_other_locations(self) -> None:
        meanings = self.directory.resolve('South of France')
        selected = next(meaning.selector for meaning in meanings if meaning.selector.origin == Origin.SAVED_VALUE)
        self.assertEqual([person.parent for person in self.directory.enumerate(selected).people], ['p001'])
        suggestions = self.directory.resolve('France')
        geographic = next(meaning for meaning in suggestions if 'France' in meaning.selector.value)
        self.assertEqual(geographic.selector.origin, Origin.SAVED_VALUE)
        self.assertEqual(geographic.count, 1)
        self.assertNotIn('p002', [person.parent for person in self.directory.enumerate(geographic.selector).people])

    def test_alias_ambiguity_and_semantic_existing_selector_recall(self) -> None:
        self.store.conn.execute("INSERT INTO topics VALUES('engineering')")
        self.store.conn.execute("INSERT INTO topics VALUES('mentorship')")
        with self.store.conn:
            self.store.conn.executemany('INSERT INTO aliases VALUES(?,?,0)', [('guide', 'engineering'), ('guide', 'mentorship')])
        self.assertEqual(self.directory.resolve('guide'), ())
        self.store.confirm_alias('guide', 'mentor')
        meanings = self.directory.resolve('guide')
        self.assertEqual(meanings[0].selector, Selector(Origin.HUMAN_TAG, 'mentor'))
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            self.store.confirm_alias('guide', 'engineering')
        provider = FixtureProvider()
        Generate(self.store, provider).run(limit=1000)
        # Paraphrase contains no lexical mentor token. The cached synthetic query maps
        # to the same vector as the existing original mentor tag.
        query = 'experienced advisor'
        with self.store.conn:
            self.store.conn.execute('INSERT INTO embeddings VALUES(?,?,?)', (provider.embedding_key, short_hash(query, n=64), packed([0., 0., 1.])))
        with patch('httpx.HTTPTransport.handle_request', side_effect=AssertionError('unsolicited API')):
            suggestions = self.directory.resolve(query, embedding_model=provider.embedding_key)
        self.assertIn(Selector(Origin.HUMAN_TAG, 'mentor'), [meaning.selector for meaning in suggestions])
        with self.assertRaises(NeedsApproval):
            self.directory.resolve('novel uncached wording', embedding_model=provider.embedding_key)
        self.assertEqual(self.directory.resolve('novel uncached wording'), ())

    def test_profile_school_and_shared_context_suggestions_preserve_values(self) -> None:
        for member, school, context in (
            ('c001', 'Stanford University', 'Visited Solstice conference'),
            ('c002', 'Different Synthetic University', 'Visited another conference'),
        ):
            source = self.store.conn.execute('SELECT id,hash FROM sources WHERE key=?', ('facts:' + member,)).fetchone()
            profile_claims = project('profile:synthetic', json.dumps({'education': [{'schoolName': school, 'degree': 'Synthetic degree', 'fieldOfStudy': 'Synthetic field'}]}))
            context_claim = Claim(context, Kind.STATEMENT, 'shared context', 'owner', '', '', '', context)
            with self.store.conn:
                for claim in (*profile_claims, context_claim):
                    self.store.add_claim(source['id'], source['hash'], None, claim, human=False)
        for query in ('Stanford', 'Solstice'):
            meanings = self.directory.resolve(query)
            self.assertEqual(len(meanings), 1)
            self.assertEqual(meanings[0].selector.origin, Origin.SAVED_VALUE)
            page = self.directory.enumerate(meanings[0].selector)
            self.assertEqual([person.parent for person in page.people], ['p001'])

    def test_fresh_semantic_query_reuses_existing_approval_and_cache(self) -> None:
        provider = FixtureProvider()
        Generate(self.store, provider).run(limit=1000)
        query = 'experienced advisor without shared terms'
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, json={'usage': {'prompt_tokens': 1}, 'data': [{'index': 0, 'embedding': [0., 0., 1.]}]}))
        with patch('httpx.HTTPTransport.handle_request', side_effect=transport.handle_request) as request, patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic-key'}):
            with self.assertRaises(NeedsApproval):
                self.directory.resolve(query, embedding_model=provider.embedding_key)
            request.assert_not_called()
            suggestions = self.directory.resolve(query, embedding_model=provider.embedding_key, approve_spend=True)
            self.assertIn(Selector(Origin.HUMAN_TAG, 'mentor'), [meaning.selector for meaning in suggestions])
            self.assertEqual(request.call_count, 1)
            self.directory.resolve(query, embedding_model=provider.embedding_key)
            self.assertEqual(request.call_count, 1)

    def test_exact_selection_preview_apply_and_all_or_nothing_validation(self) -> None:
        page = self.directory.enumerate(Selector(Origin.SOURCE_NAME, 'sof'), limit=2)
        selection = Selection.parse(json.loads(json.dumps(asdict(page))))
        preview = act(self.store, self.adapter, selection)
        self.assertFalse(preview.applied)
        for invalid in (
            Selection('foreign', selection.people),
            Selection(selection.corpus, (selection.people[0], type(selection.people[1])('unknown', 'Unknown Synthetic', ('unknown',)))),
            Selection(selection.corpus, (type(selection.people[0])('hidden', 'Hidden Synthetic', ('hidden',)),)),
        ):
            with self.assertRaises(ValueError):
                act(self.store, self.adapter, invalid, confirm=True)
            with contextlib.closing(readonly(self.adapter.database)) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM deleted_parents').fetchone()[0], 1)
        with contextlib.closing(open_store(self.adapter.database)) as conn:
            conn.execute("INSERT INTO candidate_parent(candidate_id,parent_id,reason,created_at) VALUES('sf','p000','human','later')")
            conn.commit()
        with self.assertRaisesRegex(ValueError, 'stale'):
            act(self.store, self.adapter, selection, confirm=True)
        self.store.import_source(self.adapter)
        selection = Selection.parse(json.loads(json.dumps(asdict(self.directory.enumerate(Selector(Origin.SOURCE_NAME, 'sof'), limit=2)))))
        self.assertTrue(act(self.store, self.adapter, selection, confirm=True).applied)
        self.assertEqual(self.directory.enumerate(Selector(Origin.SOURCE_NAME, 'sof')).total, 135)
        with contextlib.closing(readonly(self.adapter.database)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM candidates').fetchone()[0], 140)
            self.assertEqual(conn.execute('SELECT count(*) FROM deleted_parents').fetchone()[0], 3)

    def test_human_and_inferred_label_removal_never_hides_people(self) -> None:
        page = self.directory.enumerate(Selector(Origin.HUMAN_TAG, 'sof'))
        selection = Selection.parse(json.loads(json.dumps(asdict(page))))
        with self.assertRaisesRegex(ValueError, 'source-name editing'):
            act(self.store, self.adapter, selection, selector=Selector(Origin.SOURCE_NAME, 'sof'), confirm=True)
        act(self.store, self.adapter, selection, selector=Selector(Origin.HUMAN_TAG, 'sof'), confirm=True)
        self.assertEqual(self.directory.enumerate(Selector(Origin.HUMAN_TAG, 'sof')).total, 0)
        self.assertEqual(self.directory.enumerate(Selector(Origin.SOURCE_NAME, 'sof')).total, 137)
        source = self.store.conn.execute("SELECT id,hash FROM sources WHERE key='facts:c001'").fetchone()
        with self.store.conn:
            self.store.add_claim(source['id'], source['hash'], None, Claim('Associated with South of France', Kind.TAG, 'South of France', '', '', '', '', 'South of France'), human=False)
        inferred = Selector(Origin.TOPIC, 'south of france')
        selection = Selection.parse(json.loads(json.dumps(asdict(self.directory.enumerate(inferred)))))
        act(self.store, self.adapter, selection, selector=inferred, confirm=True)
        self.store.import_source(self.adapter)
        self.assertEqual(self.directory.enumerate(inferred).total, 0)
        self.assertEqual(self.directory.enumerate(Selector(Origin.SOURCE_NAME, 'sof')).total, 137)
        with contextlib.closing(readonly(self.adapter.database)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM deleted_parents').fetchone()[0], 1)

    def test_cli_resolve_enumerate_and_action_use_explicit_ids(self) -> None:
        def cli(*args: str) -> object:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(['--db', str(self.base / 'memory.sqlite'), *args])
            self.assertEqual(code, 0)
            return json.loads(output.getvalue())
        self.assertTrue(cli('resolve', 'SoF'))
        page = cli('enumerate', 'source_name', 'sof', '--limit', '2')
        path = self.base / 'selection.json'
        path.write_text(json.dumps(page))
        preview = cli('hide', '--selection', str(path), '--v2-root', str(self.adapter.root))
        self.assertFalse(preview['applied'])
        applied = cli('hide', '--selection', str(path), '--v2-root', str(self.adapter.root), '--confirm')
        self.assertTrue(applied['applied'])
        self.assertEqual([person['parent'] for person in preview['selection']['people']], [person['parent'] for person in applied['selection']['people']])


if __name__ == '__main__':
    unittest.main()
