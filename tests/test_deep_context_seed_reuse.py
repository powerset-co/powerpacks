"""Seeded facts remain usable until their carried message evidence changes."""

from __future__ import annotations

import json
from dataclasses import replace
from unittest import mock

from test_deep_context_seed import SeedFixture
from packs.ingestion.primitives.deep_context.collection.models import CollectionBundle
from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.deep_context.db.projectors import project_parent_fact, project_parent_source_bundle
from packs.ingestion.primitives.deep_context.synthesis import prompting, selection
from packs.ingestion.primitives.deep_context.migration.seed import Ids, Seed


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
        parent_id = next(row.parent_id for row in queries.people(db) if row.person_id == 'person-jordan')
        return db, parent_id

    def _pending(self, db, **overrides):
        params = dict(system_prompt='current system', chunk_chars=1000, max_batches=2, force=False)
        params.update(overrides)
        return {bundle.person_id for bundle in selection.pending_target_bundles(db, **params)}

    def test_seed_skips_unchanged_and_recollected_evidence(self):
        db, parent_id = self._seed_with_messages()
        self.assertNotIn(parent_id, self._pending(db))
        path = self.deep_context / f'raw/{parent_id}.json'
        payload = json.loads(path.read_text())
        payload['collected_at'] = '2026-10-01'
        payload['messages'].reverse()
        path.write_text(json.dumps(payload, indent=2))
        project_parent_source_bundle(db, path, parent_id)
        self.assertNotIn(parent_id, self._pending(db, system_prompt='new prompt'))
        bundle = CollectionBundle.from_payload(payload)
        self.assertEqual(prompting.seed_evidence_fingerprint(bundle), prompting.seed_evidence_fingerprint(replace(bundle, person_id='old-id')))

    def test_new_message_requires_synthesis_but_group_names_do_not_replay_messages(self):
        db, parent_id = self._seed_with_messages()
        path = self.deep_context / f'raw/{parent_id}.json'
        original = json.loads(path.read_text())
        for field, added in [('messages', {'channel': 'imessage', 'at': '2026-10-01', 'direction': 'from_them', 'subject': '', 'text': 'New job'}), ('groups', 'New colleagues')]:
            with self.subTest(field=field):
                payload = dict(original)
                payload[field] = [*original.get(field, []), added]
                path.write_text(json.dumps(payload))
                project_parent_source_bundle(db, path, parent_id)
                if field == 'messages':
                    self.assertIn(parent_id, self._pending(db))
                else:
                    self.assertNotIn(parent_id, self._pending(db))

    def test_explicit_recompute_overrides_seed(self):
        db, parent_id = self._seed_with_messages()
        self.assertIn(parent_id, self._pending(db, force=True))
        path = self.deep_context / f'facts/{parent_id}.jsonl'
        record = json.loads(path.read_text())
        record['model'] = 'old-model'
        record['reasoning_effort'] = 'medium'
        path.write_text(json.dumps(record) + '\n')
        project_parent_fact(db, path, parent_id)
        self.assertIn(parent_id, self._pending(db, model='new-model', reasoning_effort='medium'))

    def test_missing_raw_does_not_invent_baseline(self):
        db, _ = self._seed_with_messages()
        parent_id = next(row.parent_id for row in queries.people(db) if row.person_id == 'candidate:phone:+15550100')
        artifact = queries.artifacts(db, kind='facts', parent_id=parent_id)[0]
        self.assertFalse(artifact.input_fingerprint)
        path = self.deep_context / f'raw/{parent_id}.json'
        path.write_text(json.dumps({'person_id': parent_id, 'messages': [{'channel': 'imessage', 'at': '2026-10-01', 'direction': 'from_them', 'text': 'New contact'}]}))
        project_parent_source_bundle(db, path, parent_id)
        self.assertIn(parent_id, self._pending(db))

    def test_empty_facts_do_not_invent_baseline(self):
        path = self.legacy / 'deep-context/facts/person-jordan.jsonl'
        path.write_text('\n')
        # Remove the sibling's facts so the empty file is the selected record.
        sibling = self.legacy / 'deep-context/facts/candidate:email:casey@example.com.jsonl'
        sibling.rename(sibling.with_suffix('.bkup'))
        db, parent_id = self._seed_with_messages()
        self.assertIn(parent_id, self._pending(db))

    def test_reprojection_preserves_baseline_and_original_version(self):
        db, parent_id = self._seed_with_messages()
        path = self.deep_context / f'facts/{parent_id}.jsonl'
        record = json.loads(path.read_text())
        self.assertNotIn('synthesis_version', record)
        self.assertTrue(record['input_evidence_fingerprint'].startswith('seed:'))
        record['facts']['labels'] = ['colleague']
        path.write_text(json.dumps(record) + '\n')
        project_parent_fact(db, path, parent_id)
        self.assertNotIn(parent_id, self._pending(db))

    def test_in_place_seed_backs_up_paid_facts(self):
        db, parent_id = self._seed_with_messages()
        path = self.deep_context / f'facts/{parent_id}.jsonl'
        record = json.loads(path.read_text())
        record.pop('input_evidence_fingerprint')
        path.write_text(json.dumps(record) + '\n')
        original = path.read_bytes()
        legacy = mock.Mock()
        legacy.facts_files.return_value = [path]
        legacy.key_ids.return_value = Ids()
        legacy.raw_ids.return_value = Ids()
        cold = mock.Mock()
        cold.decide.return_value = {parent_id}
        Seed(db=db, people_csv=self.people_csv)._carry_facts(cold, legacy)
        self.assertEqual(path.with_suffix('.jsonl.bkup').read_bytes(), original)

    def test_old_seed_version_does_not_rebill_unchanged_messages(self):
        db, parent_id = self._seed_with_messages()
        path = self.deep_context / f'facts/{parent_id}.jsonl'
        record = json.loads(path.read_text())
        record['synthesis_version'] = 'old-version'
        path.write_text(json.dumps(record) + '\n')
        project_parent_fact(db, path, parent_id)
        self.assertNotIn(parent_id, self._pending(db))
