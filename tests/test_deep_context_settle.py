"""Offline settlement policy and enrichment-chain regressions."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from deep_context_sqlite_test_helpers import seed_identity
from packs.ingestion.primitives.deep_context.db._view_sql import WORTH_CTE
from packs.ingestion.primitives.deep_context.db.identity_queries import links
from packs.ingestion.primitives.deep_context.db.identity_views import enrichment_queue, linkedin_queue, synthetic_fallback
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow, EnrichmentWork, EnrichRun, EnrichRunStatus, FactRow, IdentityMachineProjection, PersonRow,
    PersonSourceRow, PersonSourcesProjection,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.workflow_views import enrichment_work, workflow_state
from packs.ingestion.primitives.deep_context.enrich.enrichment_pipeline import EnrichmentPipeline
from packs.ingestion.primitives.deep_context.enrich.profiles.models import ProfileResult
from packs.ingestion.primitives.deep_context.enrich.profiles.projection import project_profile_results
from packs.ingestion.primitives.deep_context.enrich.profiles.models import ProfileTarget
from packs.ingestion.primitives.deep_context.enrich.settle import SettleEnrichment
from packs.ingestion.primitives.deep_context.enrich.settle_policy import REVIEW_MESSAGE_BAR
from packs.ingestion.primitives.deep_context.manifests.receipt_status import ReceiptStatus
from packs.ingestion.primitives.pipeline.contract import PeopleRow


class SettleEnrichmentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Db(self.root / 'deep-context.sqlite')

    def tearDown(self):
        self.temp.cleanup()

    def seed(self, parent='parent', *, worth='yes', messages=0, accepted=False,
             own=False, human_worth=None, include_link=True):
        key = f'candidate:{parent}'
        seed_identity(
            self.db, parent_id=parent, person_id=f'person:{parent}', row_key=key,
            name='Jordan Bravo', machine_worth=worth, human_worth=human_worth,
            linkedin_url='https://www.linkedin.com/in/jordan-bravo',
            candidate_people=include_link, include_link=include_link,
            link_updates={'machine_action': 'verify', 'machine_approved': 'auto',
                          'judgment_fingerprint': 'fixture'} if accepted else {},
        )
        if own:
            self.db.project_rows((PersonSourcesProjection(
                f'person:{parent}', (PersonSourceRow(f'person:{parent}', 'linkedin_csv'),),
            ),))
        roster = list(self.db.query('SELECT row_json FROM imported_people'))
        rows = [PeopleRow.model_validate(json.loads(row['row_json'])) for row in roster]
        rows.append(PeopleRow.model_validate({
            'id': f'person:{parent}', 'full_name': 'Jordan Bravo',
            'interaction_counts': json.dumps({'imessage': messages}),
        }))
        self.db.replace_imported_people(tuple(rows))
        return key

    def profile(self, key, *, experiences=(), education=(), state='content'):
        link = links(self.db, row_keys=(key,))[0]
        result = ProfileResult.from_payload('jordan-bravo', link.linkedin_url, {
            'state': state, 'normalized_profile': {
                'success': True, 'full_name': 'Jordan Bravo', 'experiences': list(experiences), 'education': list(education),
            },
        })
        project_profile_results(self.db, ((ProfileTarget(
            'jordan-bravo', link.linkedin_url, key, link.parent_id,
        ), result),), self.root / 'profile-cache')

    def worth(self, parent='parent'):
        return self.db.query(WORTH_CTE + 'SELECT * FROM worth WHERE parent_id=?', (parent,))[0]

    def run_chain(self):
        # All paid stages are replaced at their definitions; settlement and
        # synthetic assembly execute against the real temporary SQLite store.
        with (
            mock.patch('packs.ingestion.primitives.deep_context.enrich.research_reconcile.coordinator.ReconcileDeepResearch.run',
                       return_value=SimpleNamespace(status=ReceiptStatus.REUSED)),
            mock.patch('packs.ingestion.primitives.deep_context.enrich.profiles.prefetch.PrefetchProfiles.run',
                       return_value=SimpleNamespace(status='completed')),
            mock.patch('packs.ingestion.primitives.deep_context.enrich.enrichment_pipeline.judge_mapped_candidates',
                       return_value=SimpleNamespace(judge_errors=0)),
            mock.patch('packs.ingestion.primitives.deep_context.enrich.identity_reconcile.relationship.ReviewRelationships.run',
                       return_value={'status': 'completed'}),
        ):
            pipeline = EnrichmentPipeline(self.db, manifest=self.root / 'deep-research' / 'manifest.json')
            self.assertEqual(pipeline.run(total=0, budget=0, request_fingerprint='fixture')['errors'], [])

    def test_empty_lookup_profiles_detach(self):
        for parent, state in (('missing', None), ('empty', 'empty'), ('error', 'error'), ('blank', 'content')):
            key = self.seed(parent, accepted=True)
            if state:
                self.profile(key, state=state)
        SettleEnrichment(db=self.db).run()
        for link in links(self.db):
            self.assertEqual(link.machine_action, 'detach')
            self.assertEqual(link.machine_approved, 'auto')
            self.assertIn('profile is missing or empty', link.machine_reason)

    def test_empty_accepted_retarget_detaches_and_clears_proposal(self):
        key = self.seed()
        self.db.project_rows((IdentityMachineProjection(
            key, machine_action='retarget', machine_approved='auto',
            machine_proposed_url='https://www.linkedin.com/in/jordan-bravo',
            machine_proposed_public_identifier='jordan-bravo',
            judgment_fingerprint='fixture', source='deep-research',
        ),))
        SettleEnrichment(db=self.db).run()
        link = links(self.db)[0]
        self.assertEqual(link.machine_action, 'detach')
        self.assertIsNone(link.machine_proposed_url)
        self.assertIsNone(link.machine_proposed_public_identifier)

    def test_own_connection_and_human_link_are_preserved(self):
        own = self.seed('own', own=True, accepted=True)
        human = self.seed('human', accepted=True)
        self.db.decide_identity(human, 'verify')
        before = links(self.db, row_keys=(human,))[0]
        SettleEnrichment(db=self.db).run()
        self.assertEqual(links(self.db, row_keys=(own,))[0].machine_action, 'verify')
        self.assertEqual(links(self.db, row_keys=(human,))[0], before)
        self.assertEqual(self.worth('own')['effective_worth'], 'yes')
        # A LinkedIn the human kept is who this person is, whatever its profile holds.
        self.assertEqual(self.worth('human')['effective_worth'], 'yes')

    def test_own_connection_is_always_worth_yes_without_a_human_decision(self):
        for parent, worth in (('said-no', 'no'), ('said-maybe', 'maybe'), ('said-yes', 'yes')):
            self.seed(parent, own=True, worth=worth)
        self.seed('human-no', own=True, worth='yes', human_worth='no')
        SettleEnrichment(db=self.db).run()
        for parent in ('said-no', 'said-maybe', 'said-yes'):
            self.assertEqual(self.worth(parent)['effective_worth'], 'yes')
        self.assertEqual(self.worth('said-no')['machine_worth_reason'], 'own LinkedIn connection')
        self.assertEqual(self.worth('human-no')['effective_worth'], 'no')
        # A connection the worth pass already called yes needs no parent decision.
        self.assertEqual(
            tuple(self.db.query("SELECT machine_worth FROM parents WHERE parent_id='said-yes'")[0]), (None,),
        )

    def test_what_a_completed_run_could_not_finish_does_not_hold_the_flow(self):
        key = self.seed(messages=REVIEW_MESSAGE_BAR)
        self.assertEqual(workflow_state(self.db).next_action, 'enrich')
        # The run tried this LinkedIn and could not judge it, whatever the reason.
        left = enrichment_work(self.db)
        self.assertEqual(left.judgments, (key,))
        self.db.record_enrich_run(EnrichRun(EnrichRunStatus.COMPLETED, 'synthetic', ('identity: 1 deferred',), left))
        state = workflow_state(self.db)
        self.assertNotEqual(state.next_action, 'enrich')
        # It is still left to do, and the next run tries it again.
        self.assertEqual((state.progress.judgments_pending, state.progress.enrichment_untried), (1, 0))

    def test_work_that_arrives_after_a_completed_run_is_pending(self):
        self.seed(messages=REVIEW_MESSAGE_BAR)
        left = enrichment_work(self.db)
        self.db.record_enrich_run(EnrichRun(EnrichRunStatus.COMPLETED, 'synthetic', (), left))
        later = self.seed('later', messages=REVIEW_MESSAGE_BAR)
        state = workflow_state(self.db)
        self.assertEqual(state.next_action, 'enrich')
        self.assertEqual(enrichment_work(self.db).without(left).judgments, (later,))
        self.assertEqual(state.progress.judgments_pending, 2)
        self.assertEqual(state.progress.enrichment_untried, enrichment_work(self.db).count() - left.count())

    def test_a_run_that_has_not_finished_holds_the_flow(self):
        self.assertNotEqual(workflow_state(self.db).next_action, 'enrich')
        for status in (EnrichRunStatus.RUNNING, EnrichRunStatus.FAILED):
            self.db.record_enrich_run(EnrichRun(status, 'settle'))
            state = workflow_state(self.db)
            self.assertEqual((state.next_action, state.progress.enrichment_step), ('enrich', 'settle'))
        self.db.record_enrich_run(EnrichRun(EnrichRunStatus.COMPLETED, 'synthetic'))
        state = workflow_state(self.db)
        self.assertNotEqual(state.next_action, 'enrich')
        self.assertEqual(state.progress.enrichment_step, '')

    def test_a_run_started_again_tries_everything_left(self):
        key = self.seed(messages=REVIEW_MESSAGE_BAR)
        self.db.record_enrich_run(
            EnrichRun(EnrichRunStatus.COMPLETED, 'synthetic', (), EnrichmentWork(judgments=(key,))))
        self.db.record_enrich_run(EnrichRun(EnrichRunStatus.RUNNING, 'research'))
        self.assertEqual(workflow_state(self.db).progress.enrichment_untried, enrichment_work(self.db).count())

    def test_filled_accepted_profile_keeps_worth(self):
        for parent, field in (('work', 'experiences'), ('school', 'education')):
            key = self.seed(parent, accepted=True, worth='maybe')
            self.profile(key, **{field: ({'title': 'Engineer', 'school_name': 'Example School'},)})
        SettleEnrichment(db=self.db).run()
        for parent in ('work', 'school'):
            self.assertEqual(self.worth(parent)['effective_worth'], 'maybe')

    def test_errored_profile_with_entries_is_empty(self):
        key = self.seed(accepted=True)
        self.profile(key, state='error', experiences=({'title': 'Engineer'},))
        SettleEnrichment(db=self.db).run()
        self.assertEqual(links(self.db)[0].machine_action, 'detach')
        self.assertEqual(self.worth()['effective_worth'], 'no')

    def test_message_bar_and_reason_and_queue(self):
        self.assertEqual(REVIEW_MESSAGE_BAR, 25)
        self.seed('low', worth='maybe', messages=24)
        self.seed('high', worth='maybe', messages=25)
        self.run_chain()
        low = self.worth('low')
        self.assertEqual(low['effective_worth'], 'no')
        self.assertEqual(low['machine_worth_reason'],
                         'not enough to know who this is: no LinkedIn profile and 24 messages')
        self.assertEqual(self.worth('high')['effective_worth'], 'maybe')
        self.assertEqual([row.parent_id for row in linkedin_queue(self.db)], ['high'])

    def test_human_worth_and_already_no_keep_their_decision(self):
        self.seed('human', human_worth='yes')
        self.seed('no', worth='no')
        self.seed('human-no', human_worth='no')
        SettleEnrichment(db=self.db).run()
        self.assertEqual(self.worth('human')['effective_worth'], 'yes')
        self.assertEqual(self.worth('no')['machine_worth_reason'], 'fixture')
        self.assertEqual(self.worth('human-no')['effective_worth'], 'no')
        self.assertEqual(self.db.query('SELECT machine_worth FROM parents WHERE parent_id="no"')[0][0], None)

    def test_idempotence_and_lift_when_profile_arrives(self):
        key = self.seed(messages=24)
        SettleEnrichment(db=self.db).run()
        before = tuple(self.db.query('SELECT * FROM parents')[0])
        SettleEnrichment(db=self.db).run()
        self.assertEqual(tuple(self.db.query('SELECT * FROM parents')[0]), before)
        self.assertEqual(self.worth()['effective_worth'], 'no')
        # The judge accepts the LinkedIn and its profile has content.
        self.db.project_rows((IdentityMachineProjection(
            key, machine_action='verify', machine_approved='auto',
            judgment_fingerprint='fixture', source='deep-context-reconcile',
        ),))
        self.profile(key, education=({'school_name': 'Example School'},))
        SettleEnrichment(db=self.db).run()
        self.assertEqual(self.worth()['effective_worth'], 'yes')
        self.assertEqual(tuple(self.db.query('SELECT machine_worth, machine_worth_reason FROM parents')[0]), (None, None))

    def test_lift_when_messages_arrive_and_jev_cannot_undo_no(self):
        self.seed(include_link=False, messages=24)
        SettleEnrichment(db=self.db).run()
        self.db.project_rows((FactRow('person:parent', 'parent', 'facts:person:parent',
                                   person_id='person:parent', machine_worth='yes', machine_worth_reason='new JEV answer'),))
        self.assertEqual(self.worth()['effective_worth'], 'no')
        self.assertEqual(enrichment_queue(self.db), [])
        self.db.replace_imported_people((PeopleRow.model_validate({
            'id': 'person:parent', 'full_name': 'Jordan Bravo',
            'interaction_counts': json.dumps({'imessage': 25}),
        }),))
        SettleEnrichment(db=self.db).run()
        self.assertEqual(self.worth()['effective_worth'], 'yes')
        self.assertEqual(len(enrichment_queue(self.db)), 1)

    def test_sum_non_owner_imported_people_and_channels(self):
        self.seed(messages=12)
        self.db.project_rows((PersonRow('second', 'parent'), PersonRow('owner', 'parent', is_owner=True)))
        self.db.replace_imported_people(tuple(PeopleRow.model_validate(row) for row in (
            {'id': 'person:parent', 'interaction_counts': json.dumps({'imessage': 12, 'whatsapp': 1})},
            {'id': 'second', 'interaction_counts': json.dumps({'gmail': 12})},
            {'id': 'owner', 'interaction_counts': json.dumps({'imessage': 1000})},
        )))
        SettleEnrichment(db=self.db).run()
        self.assertEqual(self.worth()['effective_worth'], 'yes')
        self.db.replace_imported_people((PeopleRow.model_validate({
            'id': 'owner', 'interaction_counts': json.dumps({'imessage': 1000}),
        }),))
        SettleEnrichment(db=self.db).run()
        self.assertEqual(self.worth()['effective_worth'], 'no')
        self.assertTrue(self.worth()['machine_worth_reason'].endswith('0 messages'))

    def test_unaccepted_filled_profile_does_not_keep_worth(self):
        key = self.seed()
        self.profile(key, experiences=({'title': 'Engineer'},))
        SettleEnrichment(db=self.db).run()
        self.assertEqual(self.worth()['effective_worth'], 'no')

    def test_settle_precedes_synthetic_assembly(self):
        from packs.ingestion.primitives.deep_context.db.models import ResearchRow
        for parent, messages in (('low', 24), ('high', 25)):
            self.seed(parent, messages=messages, include_link=False)
            payload = json.dumps({'type': 'json', 'content': {
                'real_name': 'Jordan Bravo', 'linkedin_url': None,
                'work_experience': [{'title': 'Engineer', 'company_name': 'Example Labs'}],
                'education': [], 'summary': 'Engineer',
            }, 'basis': []})
            artifact_key = f'research:{parent}'
            self.db.project_rows((
                ArtifactRow(artifact_key, 'research', parent, '/fixture/result.json', 'fixture', 'projected', payload_json=payload),
                ResearchRow(parent, parent, 'no_match', artifact_key=artifact_key, result_json=payload),
            ))
        self.run_chain()
        self.assertEqual([row.parent_id for row in synthetic_fallback(self.db)], ['high'])
        self.assertEqual([row[0] for row in self.db.query('SELECT public_identifier FROM synthetic_profiles')], ['high'])


if __name__ == '__main__':
    unittest.main()
