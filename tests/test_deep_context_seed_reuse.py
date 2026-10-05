"""Seeded facts remain usable until their carried message evidence changes."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest import mock

from test_deep_context_seed import SeedFixture
from packs.ingestion.primitives.deep_context.collection.models import CollectionBundle
from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.deep_context.db.projectors import project_person_fact, project_person_source_bundle
from packs.ingestion.primitives.deep_context.synthesis import prompting, selection
from packs.ingestion.primitives.deep_context.migration.seed import ColdIndex, Ids, Seed


class SeedReuseTests(SeedFixture):
    def _seed_with_messages(self):
        path = self.legacy / 'deep-context/raw/person-jordan.json'
        payload = json.loads(path.read_text())
        payload['messages'] = [
            {'channel': 'imessage', 'at': '2026-09-01', 'direction': 'from_them', 'subject': '', 'text': 'Hello Jordan'},
            {'channel': 'imessage', 'at': '2026-09-02', 'direction': 'from_me', 'subject': '', 'text': 'Hello Casey'},
        ]
        path.write_text(json.dumps(payload))
        db = self.cold_store()
        self.seed(db)
        person_id = next(row.person_id for row in queries.people(db) if row.person_id == 'person-jordan')
        return db, person_id

    def _pending(self, db, **overrides):
        params = dict(system_prompt='current system', chunk_chars=1000, max_batches=2, force=False)
        params.update(overrides)
        return {bundle.person_id for bundle in selection.pending_target_bundles(db, **params)}

    def test_seed_skips_unchanged_and_recollected_evidence(self):
        db, person_id = self._seed_with_messages()
        self.assertNotIn(person_id, self._pending(db))
        path = self.deep_context / f'raw/{person_id}.json'
        payload = json.loads(path.read_text())
        payload['collected_at'] = '2026-10-01'
        payload['messages'].reverse()
        path.write_text(json.dumps(payload, indent=2))
        project_person_source_bundle(db, path, person_id)
        self.assertNotIn(person_id, self._pending(db, system_prompt='new prompt'))
        bundle = CollectionBundle.from_payload(payload)
        self.assertEqual(prompting.seed_evidence_fingerprint(bundle), prompting.seed_evidence_fingerprint(replace(bundle, person_id='old-id')))

    def test_new_message_requires_synthesis_but_group_names_do_not_replay_messages(self):
        db, person_id = self._seed_with_messages()
        path = self.deep_context / f'raw/{person_id}.json'
        original = json.loads(path.read_text())
        for field, added in [('messages', {'channel': 'imessage', 'at': '2026-10-01', 'direction': 'from_them', 'subject': '', 'text': 'New job'}), ('groups', 'New colleagues')]:
            with self.subTest(field=field):
                payload = dict(original)
                payload[field] = [*original.get(field, []), added]
                path.write_text(json.dumps(payload))
                project_person_source_bundle(db, path, person_id)
                if field == 'messages':
                    self.assertIn(person_id, self._pending(db))
                else:
                    self.assertNotIn(person_id, self._pending(db))

    def test_explicit_recompute_overrides_seed(self):
        db, person_id = self._seed_with_messages()
        self.assertIn(person_id, self._pending(db, force=True))
        path = self.deep_context / f'facts/{person_id}.jsonl'
        record = json.loads(path.read_text())
        record['model'] = 'old-model'
        record['reasoning_effort'] = 'medium'
        path.write_text(json.dumps(record) + '\n')
        project_person_fact(db, path, person_id)
        self.assertIn(person_id, self._pending(db, model='new-model', reasoning_effort='medium'))

    def test_missing_raw_does_not_invent_baseline(self):
        db, _ = self._seed_with_messages()
        person_id = next(row.person_id for row in queries.people(db) if row.person_id == 'candidate:phone:+15550100')
        artifact = next(row for row in queries.artifacts(db, kind='facts') if row.person_id == person_id)
        self.assertFalse(artifact.input_fingerprint)
        path = self.deep_context / f'raw/{person_id}.json'
        path.write_text(json.dumps({'person_id': person_id, 'messages': [{'channel': 'imessage', 'at': '2026-10-01', 'direction': 'from_them', 'text': 'New contact'}]}))
        project_person_source_bundle(db, path, person_id)
        self.assertIn(person_id, self._pending(db))

    def test_empty_facts_do_not_invent_baseline(self):
        path = self.legacy / 'deep-context/facts/person-jordan.jsonl'
        path.write_text('\n')
        db, person_id = self._seed_with_messages()
        self.assertIn(person_id, self._pending(db))

    def test_reprojection_preserves_baseline_and_original_version(self):
        db, person_id = self._seed_with_messages()
        path = self.deep_context / f'facts/{person_id}.jsonl'
        record = json.loads(path.read_text())
        self.assertNotIn('synthesis_version', record)
        self.assertTrue(record['input_evidence_fingerprint'].startswith('seed:'))
        record['facts']['labels'] = ['colleague']
        path.write_text(json.dumps(record) + '\n')
        project_person_fact(db, path, person_id)
        self.assertNotIn(person_id, self._pending(db))

    def test_in_place_seed_preserves_paid_facts(self):
        db, person_id = self._seed_with_messages()
        path = self.deep_context / f'facts/{person_id}.jsonl'
        record = json.loads(path.read_text())
        record.pop('input_evidence_fingerprint')
        path.write_text(json.dumps(record) + '\n')
        original = path.read_bytes()
        legacy = mock.Mock()
        legacy.facts_files.return_value = [path]
        legacy.key_ids.return_value = Ids()
        legacy.raw_ids.return_value = Ids()
        legacy.children_of_parent_id = {}
        cold = ColdIndex(db)
        Seed(db=db)._carry_facts(cold, legacy)
        self.assertEqual(path.read_bytes(), original)
        artifact = next(row for row in queries.artifacts(db, kind='facts') if row.person_id == person_id)
        self.assertEqual(Path(artifact.path), (path.parent / 'seed' / path.name).resolve())

    def test_old_seed_version_does_not_rebill_unchanged_messages(self):
        db, person_id = self._seed_with_messages()
        path = self.deep_context / f'facts/{person_id}.jsonl'
        record = json.loads(path.read_text())
        record['synthesis_version'] = 'old-version'
        path.write_text(json.dumps(record) + '\n')
        project_person_fact(db, path, person_id)
        self.assertNotIn(person_id, self._pending(db))
