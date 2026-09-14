from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from packs.ingestion.primitives.deep_context.db.models import ArtifactRow, FactRow, LinkRow, ParentRow, PersonRow, WriterSource
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.relationship import ReviewRelationships
from packs.ingestion.primitives.deep_context.shared.openai_responses import OpenAIResponse, OpenAIUsage


class RelationshipTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"OPENAI_API_KEY": "fixture-key"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Db(self.root / 'context.sqlite')
        self.db.project_rows((
            ParentRow('jordan', 'parent:jordan', 'Jordan Bravo'),
            PersonRow('candidate:jordan', 'jordan'),
            ArtifactRow('facts:jordan', 'facts', 'jordan', '/facts/jordan', 'fixture', 'projected'),
            FactRow('jordan', 'jordan', 'facts:jordan', machine_worth='yes', facts_json=json.dumps({
                'canonical_name': 'Jordan Bravo', 'relationship_to_owner': 'Close collaborator on a robotics project',
                'network_worth': {'decision': 'yes', 'reason': 'OLD MACHINE REASON'},
            })),
            LinkRow('jordan:proposal', 'jordan', 'jordan-bravo', 'research',
                    linkedin_url='https://linkedin.com/in/jordan-bravo', candidate_origin=True, source=WriterSource.RECONCILE.value),
        ))
        self.response = OpenAIResponse({
            'decision': 'keep', 'reason': 'Substantial collaboration',
            'useful_answerable_question': True,
            'human_question': 'Is Jordan the robotics collaborator at Example Robotics?', 'priority': 2,
        }, OpenAIUsage(100, 50))

    def stage(self, **kwargs):
        return ReviewRelationships(db=self.db, out_dir=self.root / 'relationships', model='gpt-5.2', **kwargs)

    def test_preview_never_calls_provider_or_settles(self):
        before = self.db.db_path.read_bytes()
        with patch('packs.ingestion.primitives.deep_context.shared.openai_responses.OpenAIResponsesCaller.call', new_callable=AsyncMock) as call:
            result = self.stage().run()
        self.assertEqual(result['status'], 'needs_approval')
        self.assertEqual(result['calls'], 1)
        call.assert_not_called()
        self.assertEqual(self.db.db_path.read_bytes(), before)

    def test_paid_output_replays_without_call_and_omits_old_machine_judgment(self):
        with patch('packs.ingestion.primitives.deep_context.shared.openai_responses.OpenAIResponsesCaller.call', new_callable=AsyncMock, return_value=self.response) as call:
            first = self.stage(approve_spend=True).run()
            second = self.stage().run()
        self.assertEqual(call.call_count, 1)
        self.assertNotIn('OLD MACHINE REASON', call.call_args.kwargs['user_prompt'])
        self.assertEqual(first['status'], 'completed')
        self.assertEqual(second['status'], 'completed')
        self.assertEqual(second['calls'], 0)
        self.assertEqual(second['reused'], 1)

    def test_machine_exclusion_changes_worth_without_rewriting_facts(self):
        from packs.ingestion.primitives.deep_context.db.worth_views import worth_rows
        from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.settlement import MachineIdentitySettlement, settle_machine_identities
        before = self.db.query('SELECT facts_json FROM facts')[0][0]
        settle_machine_identities(self.db, (MachineIdentitySettlement(
            key='jordan:proposal', judgment_fingerprint='relationship-fixture',
            judgment_payload_json=None, machine_action='exclude', machine_approved='auto',
            machine_reason='Trivial transaction only', machine_judgment='needs_review',
            machine_confidence=None, source=WriterSource.RECONCILE.value,
        ),))
        self.assertEqual(worth_rows(self.db)[0].effective, 'no')
        self.assertEqual(self.db.query('SELECT facts_json FROM facts')[0][0], before)
        self.db.decide_worth('jordan', 'yes')
        self.assertEqual(worth_rows(self.db)[0].effective, 'yes')

    def test_partial_failure_reuses_completed_judgments_without_settling(self):
        self.db.project_rows((
            ParentRow('casey', 'parent:casey', 'Casey Delta'), PersonRow('candidate:casey', 'casey'),
            ArtifactRow('facts:casey', 'facts', 'casey', '/facts/casey', 'fixture', 'projected'),
            FactRow('casey', 'casey', 'facts:casey', machine_worth='yes', facts_json='{"title":"Engineer"}'),
            LinkRow('casey:proposal', 'casey', 'casey-delta', 'research', candidate_origin=True, source=WriterSource.RECONCILE.value),
        ))
        async def partial(**kwargs):
            if kwargs['context'] == 'casey':
                raise RuntimeError('fixture provider failure')
            return self.response
        with patch('packs.ingestion.primitives.deep_context.shared.openai_responses.OpenAIResponsesCaller.call',
                   new_callable=AsyncMock, side_effect=partial):
            first = self.stage(approve_spend=True).run()
        self.assertEqual((first['status'], first['remaining']), ('failed', 1))
        self.assertTrue(all(row['machine_approved'] is None for row in self.db.query('SELECT machine_approved FROM links')))
        with patch('packs.ingestion.primitives.deep_context.shared.openai_responses.OpenAIResponsesCaller.call',
                   new_callable=AsyncMock, return_value=self.response) as call:
            second = self.stage(approve_spend=True).run()
        self.assertEqual((second['status'], second['calls'], second['reused']), ('completed', 1, 1))
        self.assertEqual(call.call_args.kwargs['context'], 'casey')

    def test_limit_does_not_finish_unjudged_parents(self):
        self.db.project_rows((
            ParentRow('casey', 'parent:casey', 'Casey Delta'), PersonRow('candidate:casey', 'casey'),
            ArtifactRow('facts:casey', 'facts', 'casey', '/facts/casey', 'fixture', 'projected'),
            FactRow('casey', 'casey', 'facts:casey', machine_worth='yes', facts_json='{"title":"Engineer"}'),
            LinkRow('casey:proposal', 'casey', 'casey-delta', 'research', candidate_origin=True, source=WriterSource.RECONCILE.value),
        ))
        with patch('packs.ingestion.primitives.deep_context.shared.openai_responses.OpenAIResponsesCaller.call', new_callable=AsyncMock, return_value=self.response) as call:
            result = self.stage(approve_spend=True, limit=1).run()
        self.assertEqual(call.call_count, 1)
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['remaining'], 1)
        self.assertTrue(all(row['machine_approved'] is None for row in self.db.query('SELECT machine_approved FROM links')))
