from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context.collection.collect_person_context import CollectPersonContext
from packs.ingestion.primitives.deep_context.collection.models import ChatDbProbe, MessageChannel, MessageEntry
from packs.ingestion.primitives.deep_context.db.models import ParentRow, PersonRow, PersonIdentifierRow, PersonIdentifiersProjection, PersonSourceRow, PersonSourcesProjection
from packs.ingestion.primitives.deep_context.db.queries import artifacts
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.synthesis import selection, runner
from packs.ingestion.primitives.deep_context.synthesis.models import SynthesisConfig, SynthesisRecord, SynthesisResult
from packs.ingestion.primitives.deep_context.shared.openai_responses import OpenAIResponsesConfig


class ContactFactsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Db(self.root / 'deep-context.sqlite')
        self.db.project_rows((ParentRow('parent-1', 'parent-worth:parent-1', 'Jordan Bravo'),))
        for person, name, phone in [('jordan', 'Jordan Bravo', '+15550100'), ('casey', 'Casey Bravo', '+15550101')]:
            self.db.project_rows((PersonRow(person, 'parent-1', display_name=name),
                PersonIdentifiersProjection(person, (PersonIdentifierRow(person, 'phone', phone),)),
                PersonSourcesProjection(person, (PersonSourceRow(person, 'imessage'),))))

    def collect(self):
        subjects = []
        def collect_person(person, **kwargs):
            subjects.append(person)
            return [MessageEntry.of(MessageChannel.IMESSAGE, '2026-01-01', from_me=False, text=person.full_name)], 1
        collector = CollectPersonContext(db=self.db, out_dir=self.root / 'raw',
            msgvault_db=self.root / 'none', chat_db=self.root / 'none', wacli_db=self.root / 'none')
        with mock.patch('packs.ingestion.primitives.deep_context.collection.context_sources.probe_chat_db', return_value=ChatDbProbe(False, False, 0, 0, None)), mock.patch.object(collector.sources, 'collect_person', side_effect=collect_person), mock.patch.object(collector.sources, 'imessage_groups', return_value=[]):
            collector.execute()
        return subjects

    def test_collection_keeps_contact_subjects_after_parent_merge(self):
        subjects = self.collect()
        self.assertEqual({p.person_id: p.phones for p in subjects}, {'jordan': ['+15550100'], 'casey': ['+15550101']})
        rows = artifacts(self.db, kind='source_bundle', parent_owned=False)
        self.assertEqual({r.person_id for r in rows}, {'jordan', 'casey'})

    def test_synthesis_selects_contacts_and_preserves_each_history(self):
        self.collect()
        pending = selection.pending_target_bundles(self.db, system_prompt='test', chunk_chars=9000, max_batches=20, force=False)
        self.assertEqual({b.person_id for b in pending}, {'jordan', 'casey'})
        config = SynthesisConfig(self.root / 'raw', self.root / 'facts', OpenAIResponsesConfig('fixture', 'low', 1, 30, 0), 9000, 20, False)
        config.facts_dir.mkdir()
        for bundle in pending:
            record = SynthesisRecord.from_payload({'facts': {'canonical_name': bundle.full_name}, 'updated_at': '2026-01-02',
                'messages': [{'fingerprint': m.fingerprint(), 'channel': m.channel, 'at': m.at, 'direction': m.direction} for m in bundle.messages]})
            runner._store_facts(self.db, config, {}, SynthesisResult(bundle.person_id, record, 0))
        rows = artifacts(self.db, kind='facts', parent_owned=False)
        self.assertEqual({r.person_id for r in rows}, {'jordan', 'casey'})
        self.assertEqual(selection.pending_target_bundles(self.db, system_prompt='test', chunk_chars=9000, max_batches=20, force=False), [])

    def test_mixed_parent_history_does_not_claim_contact_coverage(self):
        self.collect()
        from packs.ingestion.primitives.deep_context.db.projectors import project_parent_fact
        path = self.root / 'mixed.jsonl'
        path.write_text(json.dumps({'facts': {'canonical_name': 'Jordan and Casey'}, 'updated_at': '2026-01-02'}) + '\n')
        project_parent_fact(self.db, path, 'parent-1')
        pending = selection.pending_target_bundles(self.db, system_prompt='test', chunk_chars=9000, max_batches=20, force=False)
        self.assertEqual({b.person_id for b in pending}, {'jordan', 'casey'})

    def test_missing_paid_file_preserves_all_family_artifacts(self):
        from packs.ingestion.primitives.deep_context.db.projectors import project_parent_fact, ProjectionError
        self.collect()
        path = self.root / 'mixed.jsonl'
        path.write_text(json.dumps({'facts': {'canonical_name': 'Jordan and Casey'}}) + '\n')
        project_parent_fact(self.db, path, 'parent-1')
        before = artifacts(self.db)
        with self.assertRaises(ProjectionError):
            project_parent_fact(self.db, self.root / 'missing.jsonl', 'parent-1')
        self.assertEqual(artifacts(self.db), before)

    def test_parent_composition_retains_root_contact_and_mixed_artifacts(self):
        from packs.ingestion.primitives.deep_context.db.projectors import project_parent_fact
        from packs.ingestion.primitives.deep_context.synthesis.normalization import normalize_parent_cache
        self.collect()
        mixed = self.root / 'mixed.jsonl'
        mixed.write_text(json.dumps({'facts': {'canonical_name': 'Jordan and Casey'}}) + '\n')
        project_parent_fact(self.db, mixed, 'parent-1')
        paid = artifacts(self.db, kind='facts')[0]
        facts_dir = self.root / 'facts'
        facts_dir.mkdir()
        originals = {}
        for person, name in [('jordan', 'Jordan Bravo'), ('casey', 'Casey Bravo')]:
            path = facts_dir / f'{person}.jsonl'
            path.write_text(json.dumps({'facts': {'canonical_name': name}, 'updated_at': '2026-01-01'}) + '\n')
            originals[path] = path.read_bytes()
        normalize_parent_cache(self.db, raw_dir=self.root / 'raw', facts_dir=facts_dir)
        self.assertEqual(artifacts(self.db, kind='facts', parent_owned=True)[0], paid)
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)
        rows = self.db.query('SELECT person_id,artifact_key FROM facts ORDER BY subject_key')
        self.assertEqual({row['person_id'] for row in rows if row['person_id']}, {'jordan', 'casey'})
        self.assertEqual([row['artifact_key'] for row in rows if not row['person_id']], ['parent-facts:parent-1'])

    def test_singleton_restoration_unions_newer_parent_facts_with_root_cache(self):
        from packs.ingestion.primitives.deep_context.db.context_queries import person_histories
        from packs.ingestion.primitives.deep_context.db.models import ArtifactRow
        from packs.ingestion.primitives.deep_context.db.projectors import project_parent_fact, project_person_fact, project_person_source_bundle
        from packs.ingestion.primitives.deep_context.synthesis.normalization import normalize_parent_cache
        self.db.project_rows((ParentRow('parent-2', 'parent-worth:parent-2', 'Casey Bravo'),
                              PersonRow('casey', 'parent-2', display_name='Casey Bravo')))
        facts_dir = self.root / 'facts'
        facts_dir.mkdir()
        messages = [MessageEntry.of(MessageChannel.IMESSAGE, date, from_me=False, text=text)
                    for date, text in [('2026-07-01', 'old message'), ('2026-09-01', 'new message')]]
        def record(employer, index):
            message = messages[index]
            return {'facts': {'employers': [{'name': employer}]}, 'updated_at': message.at,
                    'input_evidence_fingerprint': f'original-{index}',
                    'messages': [{'fingerprint': message.fingerprint(), 'channel': message.channel,
                                  'at': message.at, 'direction': message.direction}]}
        original = facts_dir / 'jordan.jsonl'
        original.write_text(json.dumps(record('Old Example', 0)) + '\n')
        parent = facts_dir / 'parent-1.jsonl'
        parent.write_text(json.dumps(record('New Example', 1)) + '\n')
        project_parent_fact(self.db, parent, 'parent-1')
        project_person_fact(self.db, original, 'jordan')
        old_derived = facts_dir / 'parents/parent-1.jsonl'
        old_derived.parent.mkdir()
        old_derived.write_bytes(original.read_bytes())
        project_parent_fact(self.db, old_derived, 'parent-1', artifact_key='parent-facts:parent-1')
        archived = {'facts': {'employers': [{'name': 'Mixed Archive'}]}, 'updated_at': '2026-10-01'}
        self.db.project_rows((ArtifactRow('facts:archived-parent', 'facts', 'parent-1', str(self.root / 'mixed.jsonl'),
                                         '0' * 64, 'failed', payload_json=json.dumps(archived)),))
        raw = self.root / 'raw'
        raw.mkdir()
        bundle_path = raw / 'jordan.json'
        bundle_path.write_text(json.dumps({'person_id': 'jordan', 'messages': [message.to_payload() for message in messages]}))
        project_person_source_bundle(self.db, bundle_path, 'jordan')
        originals = original.read_bytes(), parent.read_bytes()
        normalize_parent_cache(self.db, raw_dir=raw, facts_dir=facts_dir)
        history = person_histories(self.db)['jordan']
        self.assertEqual({employer.name for employer in history.facts.employers}, {'Old Example', 'New Example'})
        self.assertEqual(history.processed, {message.fingerprint() for message in messages})
        self.assertEqual([item.record.input_evidence_fingerprint for item in history.records], ['original-0', 'original-1'])
        contact = next(row for row in artifacts(self.db, kind='facts') if row.person_id == 'jordan')
        self.assertEqual(Path(contact.path), (facts_dir / 'seed/jordan.jsonl').resolve())
        self.assertEqual(runner._tagging_inputs({}, 'jordan', Path(contact.path))[3], history)
        self.assertEqual(selection.pending_target_bundles(self.db, system_prompt='test', chunk_chars=9000, max_batches=20, force=False), [])
        self.assertEqual(normalize_parent_cache(self.db, raw_dir=raw, facts_dir=facts_dir), 0)
        self.assertEqual(len(person_histories(self.db)['jordan'].records), 2)
        self.assertEqual((original.read_bytes(), parent.read_bytes()), originals)
        from unittest.mock import AsyncMock
        from packs.ingestion.primitives.deep_context.db.models import OwnerProfile
        from packs.ingestion.primitives.deep_context.jev_worth.models import WorthEstimate, WorthResult
        from packs.ingestion.primitives.deep_context.synthesis.models import JevUsage, NetworkWorthFact, SynthesisPlan
        config = SynthesisConfig(raw, facts_dir, OpenAIResponsesConfig('fixture', 'low', 1, 30, 0), 9000, 20, False)
        plan = SynthesisPlan('test', (), OwnerProfile('Synthetic Owner'))
        answer = WorthResult(NetworkWorthFact('yes', 'Synthetic relationship'), {'is_professional': 0.9}, JevUsage(), 0, 0)
        with mock.patch.object(runner.jev_worth, 'classify', AsyncMock(return_value=answer)) as classify, \
                mock.patch.object(runner.jev_worth, 'estimate', return_value=WorthEstimate(cached=True)):
            runner.tag_saved_facts(self.db, config, plan)
            self.assertEqual(normalize_parent_cache(self.db, raw_dir=raw, facts_dir=facts_dir), 1)
            after = Path(contact.path).read_bytes()
            self.assertEqual(normalize_parent_cache(self.db, raw_dir=raw, facts_dir=facts_dir), 0)
            runner.tag_saved_facts(self.db, config, plan)
        classify.assert_awaited_once()
        self.assertEqual(Path(contact.path).read_bytes(), after)
        self.assertEqual(person_histories(self.db)['jordan'].facts.labels, {'is_professional': 0.9})
        self.assertEqual((original.read_bytes(), parent.read_bytes()), originals)

    def test_parent_composition_waits_for_each_contact(self):
        from packs.ingestion.primitives.deep_context.db.projectors import project_parent_fact, project_person_fact
        from packs.ingestion.primitives.deep_context.synthesis.normalization import normalize_parent_cache
        facts_dir = self.root / 'facts'
        facts_dir.mkdir()
        mixed = facts_dir / 'parent-1.jsonl'
        mixed.write_text(json.dumps({'facts': {'canonical_name': 'Jordan and Casey'}}) + '\n')
        project_parent_fact(self.db, mixed, 'parent-1')
        original = artifacts(self.db, kind='facts')[0]
        child = facts_dir / 'jordan.jsonl'
        child.write_text(json.dumps({'facts': {'canonical_name': 'Jordan Bravo'}}) + '\n')
        project_person_fact(self.db, child, 'jordan')
        normalize_parent_cache(self.db, raw_dir=self.root / 'raw', facts_dir=facts_dir)
        self.assertEqual(artifacts(self.db, kind='facts', parent_owned=True), (original,))
        self.assertEqual(self.db.query("SELECT artifact_key FROM facts WHERE person_id IS NULL")[0]['artifact_key'], original.artifact_key)
        empty = facts_dir / 'casey.jsonl'
        empty.write_text(json.dumps({'facts': {}}) + '\n')
        project_person_fact(self.db, empty, 'casey')
        normalize_parent_cache(self.db, raw_dir=self.root / 'raw', facts_dir=facts_dir)
        self.assertEqual(artifacts(self.db, kind='facts', parent_owned=True), (original,))

    def test_build_parents_refreshes_both_derived_parents_after_merge(self):
        from packs.ingestion.primitives.deep_context.db.models import MergeVerdictRow
        from packs.ingestion.primitives.deep_context.db.projectors import project_person_fact
        from packs.ingestion.primitives.deep_context.merge_candidates.build_parents import BuildParents, _parent_plans
        from packs.ingestion.primitives.deep_context.shared.lookup_person import main as lookup_main
        from packs.ingestion.primitives.deep_context.synthesis.normalization import normalize_parent_cache
        from packs.ingestion.primitives.pipeline.contract import PeopleRow
        self.db.project_rows((
            ParentRow('parent-1', 'parent-worth:parent-1', 'Jordan Bravo', 'jordan-parent'),
            ParentRow('parent-2', 'parent-worth:parent-2', 'Jordan A. Bravo', 'jordan-alias'),
            PersonRow('casey', 'parent-2', display_name='Jordan A. Bravo'),
        ))
        self.db.replace_imported_people((
            PeopleRow(id='jordan', full_name='Jordan Bravo'),
            PeopleRow(id='casey', full_name='Jordan A. Bravo'),
        ))
        facts_dir = self.root / 'facts'
        facts_dir.mkdir()
        for person, employer in [('jordan', 'Example One'), ('casey', 'Example Two')]:
            path = facts_dir / f'{person}.jsonl'
            path.write_text(json.dumps({'facts': {'canonical_name': 'Jordan Bravo', 'employers': [{'name': employer}]}}) + '\n')
            project_person_fact(self.db, path, person)
        normalize_parent_cache(self.db, raw_dir=self.root / 'raw', facts_dir=facts_dir)
        self.db.replace_merge_verdicts((MergeVerdictRow('casey', 'jordan', 'casey', 'jordan', 'fixture', 'slam_dunk',
                                             True, 1.0, True, accepted=True),))
        built = BuildParents(db=self.db, parents_dir=self.root / 'parents').execute()
        self.assertEqual(built.parents_merged, 1)
        plans, _ = _parent_plans(self.db)
        self.assertEqual(len(plans), 1)
        self.assertEqual({employer.name for employer in plans[0].merged.employers}, {'Example One', 'Example Two'})
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(lookup_main(['--name', 'Jordan A. Bravo', '--db', str(self.db.db_path)]), 0)
        self.assertIn('Example One', output.getvalue())
        self.assertIn('Example Two', output.getvalue())

    def test_source_contacts_exclude_cached_import_aggregate(self):
        from packs.ingestion.primitives.deep_context.db.projectors import project_person_fact
        from packs.ingestion.primitives.deep_context.synthesis.normalization import normalize_parent_cache
        from packs.ingestion.primitives.pipeline.contract import PeopleRow
        original = 'candidate:email:jordan@example.com'
        self.db.project_rows((PersonRow(original, 'parent-1', display_name='Jordan Bravo'),
            PersonIdentifiersProjection(original, (PersonIdentifierRow(original, 'email', 'jordan@example.com'),)),
            PersonSourcesProjection(original, (PersonSourceRow(original, 'gmail_msgvault'),))))
        self.collect()
        self.db.project_rows((PersonIdentifiersProjection('jordan', ()),))
        self.db.replace_imported_people((PeopleRow(id='jordan', superseded_person_ids=json.dumps([original])),))
        self.assertEqual({bundle.person_id for bundle in selection.effective_person_bundles(self.db).values()}, {original, 'casey'})
        facts_dir = self.root / 'facts'
        facts_dir.mkdir()
        for person, employer in [('jordan', 'Mixed Only'), (original, 'Independent'), ('casey', 'Casey Only')]:
            path = facts_dir / f'{person}.jsonl'
            path.write_text(json.dumps({'facts': {'employers': [{'name': employer}]}}) + '\n')
            project_person_fact(self.db, path, person)
        config = SynthesisConfig(self.root / 'raw', facts_dir, OpenAIResponsesConfig('fixture', 'low', 1, 30, 0), 9000, 20, False)
        with mock.patch.object(runner, '_needs_tagging', return_value=True):
            paths = runner._tagging_paths(self.db, config, selection.effective_person_bundles(self.db), None, headlines={})
        self.assertEqual({person for person, _ in paths}, {original, 'casey'})
        normalize_parent_cache(self.db, raw_dir=self.root / 'raw', facts_dir=facts_dir)
        aggregate = json.loads(self.db.query("SELECT facts_json FROM facts WHERE subject_key='parent-1'")[0]['facts_json'])
        self.assertEqual({employer['name'] for employer in aggregate['employers']}, {'Independent', 'Casey Only'})
        self.assertTrue(any(row.person_id == 'jordan' for row in artifacts(self.db, kind='facts')))
        self.assertFalse(self.db.query("SELECT subject_key FROM facts WHERE person_id='jordan'"))
