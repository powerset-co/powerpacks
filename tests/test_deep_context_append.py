"""Accumulated facts preserve observations and only consume submitted evidence."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from packs.ingestion.primitives.deep_context.collection.models import MessageEntry, MessageChannel
from packs.ingestion.primitives.deep_context.synthesis.history import FactHistory


class AppendHistoryTests(unittest.TestCase):
    def test_preserves_old_observations_without_caps_and_uses_latest_scalar(self):
        old = {'facts': {'title': 'Engineer', 'topics': [f'topic {i}' for i in range(150)]},
               'messages': [{'fingerprint': 'old', 'channel': 'imessage', 'at': '2025-01-01', 'direction': 'from_them'}], 'updated_at': '2025-01-01'}
        new = {'facts': {'title': 'Founder', 'topics': ['new topic']},
               'messages': [{'fingerprint': 'new', 'channel': 'imessage', 'at': '2026-01-01', 'direction': 'from_them'}], 'updated_at': '2026-01-01'}
        history = FactHistory.from_records([new, old])
        self.assertEqual(history.facts.title, 'Founder')
        self.assertEqual(len(history.facts.topics), 151)
        self.assertEqual(history.processed, frozenset({'old', 'new'}))
        self.assertEqual(history.records[0].record.facts.title, 'Engineer')

    def test_message_hash_handles_timestamp_ties_and_source_changes(self):
        message = MessageEntry.of(MessageChannel.IMESSAGE, '2026-01-01', from_me=False, text='old')
        self.assertNotEqual(message.fingerprint(), replace(message, text='new').fingerprint())
        self.assertNotEqual(message.fingerprint(), replace(message, channel=MessageChannel.WHATSAPP).fingerprint())

    def test_single_legacy_record_keeps_exact_fact_order(self):
        record = {'facts': {'title': 'Engineer', 'legacy_field': 'keep', 'canonical_name': 'Jordan Bravo'}}
        self.assertEqual(FactHistory.from_records([record]).payload()['facts'], record['facts'])

from unittest.mock import patch
from types import SimpleNamespace
from packs.ingestion.primitives.deep_context.db.models import ParentRow, PersonRow, OwnerContextRow
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.projectors import project_parent_source_bundle
from packs.ingestion.primitives.deep_context.db.context_queries import parent_histories
from packs.ingestion.primitives.deep_context.synthesis import runner
from packs.ingestion.primitives.deep_context.synthesis.synthesize_person_context import SynthesizePersonContext


class AppendPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Db(self.root / 'deep-context.sqlite')
        self.db.project_rows((ParentRow('jordan', 'jordan'), PersonRow('person-jordan', 'jordan'),
            OwnerContextRow('owner', json.dumps({'name': 'Synthetic Owner'}), 'owner.json', '0' * 64)))
        self.calls = []
        self.fail = False
        outer = self
        class Caller:
            def __init__(self, config): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def call(self, **kwargs):
                prompt = kwargs['user_prompt']
                outer.calls.append(prompt)
                if outer.fail:
                    raise RuntimeError('synthetic failure')
                new = 'new-company' in prompt
                return SimpleNamespace(payload={'canonical_name': 'Jordan Bravo',
                    'title': 'Founder' if new else 'Engineer',
                    'employers': [{'name': 'New Company' if new else 'Old Company', 'role': 'Founder' if new else 'Engineer', 'status': 'current'}]},
                    usage=runner.SynthesisUsage())
        self.caller = patch.object(runner, 'OpenAIResponsesCaller', Caller)
        self.caller.start()
        self.addCleanup(self.caller.stop)
        self.tag = patch.object(runner, 'tag_saved_facts', return_value=runner.JevUsage())
        self.tag.start()
        self.addCleanup(self.tag.stop)

    def bundle(self, texts, parent='jordan'):
        path = self.root / f'{parent}.json'
        path.write_text(json.dumps({'person_id': parent, 'messages': [
            {'channel': 'imessage', 'at': '2026-01-01', 'direction': 'from_them', 'text': text}
            for text in texts]}))
        project_parent_source_bundle(self.db, path, parent)

    def run_node(self, **kwargs):
        node = SynthesizePersonContext(db=self.db, raw_dir=self.root, out_dir=self.root / 'facts',
            people_csv=self.root / 'people.csv', model='fixture-model', chunk_chars=1, **kwargs)
        return node.run()

    def test_delta_keeps_historical_employer_and_skips_old_same_time_message(self):
        self.bundle(['old-company'])
        self.run_node()
        self.bundle(['old-company', 'new-company'])
        self.run_node()
        history = parent_histories(self.db)['jordan']
        self.assertEqual(history.facts.title, 'Founder')
        self.assertEqual([e.name for e in history.facts.employers], ['New Company', 'Old Company'])
        self.assertEqual(len(history.records), 2)
        self.assertNotIn('old-company', self.calls[-1])
        self.run_node()
        self.assertEqual(len(self.calls), 2)

    def test_failed_delta_preserves_facts_and_coverage(self):
        self.bundle(['old-company'])
        self.run_node()
        path = self.root / 'facts/jordan.jsonl'
        before = path.read_bytes()
        self.bundle(['old-company', 'new-company'])
        self.fail = True
        self.assertEqual(self.run_node().status, 'failed')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(parent_histories(self.db)['jordan'].processed), 1)
        self.fail = False
        self.run_node()
        self.assertEqual(len(parent_histories(self.db)['jordan'].processed), 2)

    def test_max_batches_advances_only_consumed_evidence(self):
        self.bundle(['old-company', 'new-company', 'third-message'])
        self.run_node(max_batches=1)
        self.assertEqual(len(parent_histories(self.db)['jordan'].processed), 1)
        self.run_node(max_batches=1)
        self.run_node(max_batches=1)
        self.run_node(max_batches=1)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(parent_histories(self.db)['jordan'].processed), 3)

    def test_merge_keeps_both_histories_on_next_append(self):
        self.bundle(['old-company'])
        self.run_node()
        self.db.project_rows((ParentRow('casey', 'casey'), PersonRow('person-casey', 'casey')))
        self.bundle(['new-company'], parent='casey')
        self.run_node()
        self.db.merge_parents('jordan', 'casey')
        self.run_node()
        self.assertEqual(len(self.calls), 2)
        self.bundle(['old-company', 'new-company', 'third-message'])
        self.run_node()
        history = parent_histories(self.db)['jordan']
        self.assertEqual(len(history.records), 3)
        self.assertEqual(len(history.processed), 3)
        self.run_node()
        self.assertEqual(len(self.calls), 3)

    def test_failed_model_switch_retries_from_successful_record(self):
        self.bundle(['old-company'])
        self.run_node()
        node = SynthesizePersonContext(db=self.db, raw_dir=self.root, out_dir=self.root / 'facts',
            people_csv=self.root / 'people.csv', model='other-model', chunk_chars=1)
        self.fail = True
        self.assertEqual(node.run().status, 'failed')
        self.assertEqual(len(node._plan().bundles), 1)
        self.fail = False
        node.run()
        self.assertEqual(node._plan().bundles, ())
        self.assertEqual(len(parent_histories(self.db)['jordan'].records), 2)

    def test_force_appends_current_bundle_and_retains_prior_records(self):
        self.bundle(['old-company'])
        self.run_node()
        self.run_node(force=True)
        self.assertEqual(len(parent_histories(self.db)['jordan'].records), 2)
        self.assertEqual(len(parent_histories(self.db)['jordan'].processed), 1)

    def test_old_backfill_does_not_replace_recent_title_or_lose_jev_channels(self):
        self.bundle(['new-company'])
        self.run_node()
        path = self.root / 'jordan.json'
        path.write_text(json.dumps({'person_id': 'jordan', 'source_channels': ['gmail_msgvault'], 'messages': [
            {'channel': 'gmail', 'at': '2020-01-01', 'direction': 'from_me', 'text': 'old-company'}]}))
        project_parent_source_bundle(self.db, path, 'jordan')
        self.run_node()
        history = parent_histories(self.db)['jordan']
        self.assertEqual(history.facts.title, 'Founder')
        from packs.ingestion.primitives.deep_context.jev_worth.questions import build_request
        from packs.ingestion.primitives.deep_context.jev_worth.models import WorthFacts
        from packs.ingestion.primitives.deep_context.synthesis.selection import effective_parent_bundles
        request = build_request(facts=WorthFacts.from_payload(history.facts.to_payload()),
            bundle=effective_parent_bundles(self.db)['jordan'], owner=None, reference_date='2026-01-01', history=history)
        serialized = json.dumps(request)
        self.assertIn('imessage', serialized)
        self.assertIn('gmail', serialized)
        self.assertIn('2020-01-01', serialized)

    def test_absorbed_only_history_uses_survivor_id(self):
        self.bundle(['old-company'])
        self.run_node()
        self.db.project_rows((ParentRow('casey', 'casey'), PersonRow('person-casey', 'casey')))
        self.db.merge_parents('casey', 'jordan')
        self.run_node()
        self.assertEqual(len(self.calls), 1)
        self.bundle(['new-company'], parent='casey')
        self.run_node()
        self.assertEqual(len(parent_histories(self.db)['casey'].records), 2)

    def test_jev_tags_accumulated_facts_without_erasing_extractions(self):
        self.bundle(['old-company'])
        self.run_node()
        self.bundle(['new-company'])
        self.run_node()
        self.tag.stop()
        from unittest.mock import AsyncMock
        from packs.ingestion.primitives.deep_context.jev_worth.models import WorthResult, WorthEstimate
        from packs.ingestion.primitives.deep_context.synthesis.models import NetworkWorthFact
        answer = WorthResult(NetworkWorthFact('yes', 'Human relationship'), {'professional': 0.9}, runner.JevUsage(), 0, 0)
        with patch.object(runner.jev_worth, 'classify', AsyncMock(return_value=answer)) as classify, \
                patch.object(runner.jev_worth, 'estimate', return_value=WorthEstimate(cached=True)):
            self.run_node()
            self.run_node()
        classify.assert_awaited_once()
        facts = classify.await_args.kwargs['facts'].facts
        self.assertEqual([employer.name for employer in facts.employers], ['New Company', 'Old Company'])
        history = parent_histories(self.db)['jordan']
        self.assertEqual(len(history.records), 2)
        self.assertEqual(history.facts.network_worth.decision, 'yes')
        self.assertEqual(history.records[0].record.facts.title, 'Engineer')

    def test_owner_context_change_requires_new_extraction(self):
        self.bundle(['old-company'])
        self.run_node()
        self.db.project_rows((OwnerContextRow('owner', json.dumps({'name': 'New Synthetic Owner'}), 'owner.json', '1' * 64),))
        self.run_node()
        self.assertEqual(len(self.calls), 2)
        self.run_node()
        self.assertEqual(len(self.calls), 2)
