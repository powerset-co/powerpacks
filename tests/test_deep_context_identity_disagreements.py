"""Parent identity decisions use exact candidate URLs without quotas."""
import unittest
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.candidate_selection import RelationshipDecision

class IdentityDisagreementsTest(unittest.TestCase):
    def test_multiple_distinct_winners_rejected(self):
        with self.assertRaises(ValueError):
            RelationshipDecision.from_payload('parent', 'fingerprint', {'candidates': [
                {'url': 'https://linkedin.com/in/jordan-bravo', 'verdict': 'yes', 'reason': 'Email match', 'confidence': .9},
                {'url': 'https://linkedin.com/in/casey-bravo', 'verdict': 'yes', 'reason': 'Name match', 'confidence': .8}]})

    def test_duplicate_url_requires_same_verdict(self):
        with self.assertRaises(ValueError):
            RelationshipDecision.from_payload('parent', 'fingerprint', {'candidates': [
                {'url': 'https://linkedin.com/in/jordan-bravo', 'verdict': 'yes', 'reason': 'Email match', 'confidence': .9},
                {'url': 'https://linkedin.com/in/jordan-bravo', 'verdict': 'no', 'reason': 'Wrong person', 'confidence': .8}]})

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch
from packs.ingestion.primitives.deep_context.db.models import ArtifactRow, FactRow, LinkRow, ParentRow, PersonRow
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.relationship import ReviewRelationships
from packs.ingestion.primitives.deep_context.shared.openai_responses import OpenAIResponse, OpenAIUsage

class ParentIdentityTest(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {'OPENAI_API_KEY': 'fixture-key'})
        environment.start()
        self.addCleanup(environment.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = Db(self.root / 'context.sqlite')

    def parent(self, parent='jordan'):
        self.db.project_rows((ParentRow(parent, f'worth:{parent}', 'Jordan Bravo'),
            PersonRow(f'person:{parent}', parent),
            ArtifactRow(f'facts:{parent}', 'facts', parent, '/fixture', 'fixture', 'projected'),
            FactRow(parent, parent, f'facts:{parent}', machine_worth='yes', facts_json=json.dumps({'canonical_name':'Jordan Bravo'})),
            LinkRow(f'{parent}:a', parent, f'{parent}-bravo', 'research', candidate_origin=True, source="deep-context-reconcile",
                linkedin_url=f'https://linkedin.com/in/{parent}-bravo')))

    def run_stage(self, payload, **kwargs):
        response = OpenAIResponse(payload, OpenAIUsage(100, 50))
        with patch('packs.ingestion.primitives.deep_context.shared.openai_responses.OpenAIResponsesCaller.call',
            new_callable=AsyncMock, return_value=response) as call:
            result = ReviewRelationships(db=self.db, out_dir=self.root/'relationships', **kwargs).run()
        return result, call

    def test_confirmed_candidate_and_wrong_alternative_settle_without_review(self):
        self.parent()
        self.db.project_rows((LinkRow('jordan:b', 'jordan', 'casey-bravo', 'research', candidate_origin=True, source="deep-context-reconcile",
            linkedin_url='https://linkedin.com/in/casey-bravo'),))
        with self.db.transaction() as conn:
            conn.execute("UPDATE links SET machine_judgment='confirmed', machine_action='verify', machine_approved='auto' WHERE row_key='jordan:a'")
        result, call = self.run_stage({'candidates':[
            {'url':'https://linkedin.com/in/jordan-bravo', 'verdict':'yes','reason':'Email and history agree','confidence':.95},
            {'url':'https://linkedin.com/in/casey-bravo', 'verdict':'no','reason':'Different employer and location','confidence':.95}]}, approve_spend=True)
        self.assertEqual(len(json.loads(call.call_args.kwargs['user_prompt'])['candidates']), 2)
        self.assertEqual(result['reviews']['review_parents'], 0)
        self.assertEqual([row['machine_judgment'] for row in self.db.query('SELECT machine_judgment FROM links ORDER BY row_key')], ['confirmed','wrong_person'])

    def test_review_cache_reuses_unchanged_evidence(self):
        self.parent()
        payload={'candidates':[{'url':'https://linkedin.com/in/jordan-bravo','verdict':'review','reason':'Two plausible histories','confidence':.5}]}
        self.run_stage(payload, approve_spend=True)
        result, call = self.run_stage(payload)
        call.assert_not_called()
        self.assertEqual(result['reused'], 1)

    def test_human_retarget_preserved(self):
        self.parent()
        with self.db.transaction() as conn:
            conn.execute("UPDATE links SET decision_action='retarget', decision_approved='yes', replacement_url='https://linkedin.com/in/other-bravo' WHERE parent_id='jordan'")
        before = [tuple(row) for row in self.db.query('SELECT * FROM links')]
        result, call = self.run_stage({'candidates':[]}, approve_spend=True)
        call.assert_not_called()
        self.assertEqual(before, [tuple(row) for row in self.db.query('SELECT * FROM links')])

    def test_more_than_100_unresolved_parents_remain_pending(self):
        from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.candidate_selection import finish_reviews
        decisions=[]
        for index in range(101):
            parent=f'fixture{index}'
            self.parent(parent)
            decisions.append(RelationshipDecision.from_payload(parent, 'fingerprint', {'candidates':[
                {'url':f'https://linkedin.com/in/{parent}-bravo','verdict':'review','reason':'Unresolved history','confidence':.5}]}))
        result=finish_reviews(self.db, tuple(decisions))
        self.assertEqual(result['review_parents'],101)
        self.assertEqual(self.db.query("SELECT count(*) FROM links WHERE machine_approved IS NULL")[0][0],101)

    def test_duplicate_url_is_one_machine_winner(self):
        from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.candidate_selection import finish_reviews
        self.parent()
        self.db.project_rows((LinkRow('jordan:b', 'jordan', 'jordan-bravo', 'research', candidate_origin=True,
            source='deep-context-reconcile', linkedin_url='https://linkedin.com/in/jordan-bravo'),))
        decision=RelationshipDecision.from_payload('jordan','fingerprint', {'candidates':[
            {'url':'https://linkedin.com/in/jordan-bravo','verdict':'yes','reason':'Email agrees','confidence':.95}]})
        finish_reviews(self.db, (decision,))
        self.assertEqual([row[0] for row in self.db.query('SELECT machine_approved FROM links')], ['auto','auto'])

    def test_two_human_no_decisions_preserved(self):
        self.parent()
        self.db.project_rows((LinkRow('jordan:b', 'jordan', 'casey-bravo', 'research', candidate_origin=True,
            source='deep-context-reconcile', linkedin_url='https://linkedin.com/in/casey-bravo'),))
        self.db.decide_identity('jordan:a', 'detach')
        self.db.decide_identity('jordan:b', 'detach')
        before=[tuple(row) for row in self.db.query('SELECT * FROM links')]
        result, call=self.run_stage({'candidates':[]}, approve_spend=True)
        call.assert_not_called()
        self.assertEqual(before,[tuple(row) for row in self.db.query('SELECT * FROM links')])

    def test_changed_candidate_evidence_requires_a_new_judgment(self):
        self.parent()
        payload={'candidates':[{'url':'https://linkedin.com/in/jordan-bravo','verdict':'review','reason':'Unresolved history','confidence':.5}]}
        self.run_stage(payload, approve_spend=True)
        with self.db.transaction() as conn:
            conn.execute("UPDATE facts SET facts_json=? WHERE parent_id='jordan'", (json.dumps({'canonical_name':'Jordan Bravo','relationship_to_owner':'Former colleague'}),))
        result, call=self.run_stage(payload)
        call.assert_not_called()
        self.assertEqual((result['status'],result['calls']),('needs_approval',1))
