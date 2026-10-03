import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.ensure_parents.imported_people import _imported_people, project_imported_people
from packs.ingestion.primitives.deep_context.merge_candidates.linkedin_name_matches import apply_linkedin_name_matches, linkedin_name_matches, match_names
from packs.ingestion.primitives.pipeline.contract import PeopleRow


class LinkedInNameMatchesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Db(self.root / 'deep-context.sqlite')

    def seed(self, contact='Jordan Bravo', connection='Jordan Bravo', extra=()):
        rows = (PeopleRow(id='linkedin-jordan', full_name=connection,
            public_identifier='jordan-bravo', linkedin_url='https://www.linkedin.com/in/jordan-bravo',
            source_channels='linkedin_csv'), PeopleRow(id='candidate:phone:+15550100123',
            full_name=contact, primary_phone='+15550100123', source_channels='whatsapp'), *extra)
        project_imported_people(self.db, _imported_people(rows))
        return {row.person_id: row.parent_id for row in queries.people(self.db)}

    def test_names_use_full_surname_and_first_name_or_initial_or_prefix(self):
        for first, second in (('Jordan Bravo', 'Jordan Bravo'), ('J. Bravo', 'Jordan Bravo'),
                              ('Ben Bravo', 'Benjamin Bravo'), ('Jordan Alex Bravo', 'Jordan Bravo')):
            with self.subTest(first=first):
                self.assertTrue(match_names(first, second))
        for first, second in (('Jordan B', 'Jordan Bravo'), ('Jordan', 'Jordan Bravo'),
                              ('Jon Bravo', 'John Bravo'), ('Alex Chow', 'Alex Clayton'),
                              ('Jordan Ruth Bravo', 'Jordan Alex Bravo'), ('', 'Jordan Bravo')):
            with self.subTest(first=first):
                self.assertFalse(match_names(first, second))

    def test_connection_without_facts_matches_phone_parent(self):
        parents = self.seed()
        self.assertEqual(queries.facts(self.db), ())
        plan = linkedin_name_matches(self.db)
        self.assertEqual(len(plan.matches), 1)
        self.assertEqual(set(plan.matches[0].parent_ids), set(parents.values()))
        self.assertEqual(plan.matches[0].linkedin_url, 'https://www.linkedin.com/in/jordan-bravo')
        self.assertEqual(len(queries.parents(self.db)), 2)

    def test_initial_matches_full_given_name(self):
        self.seed(contact='J Bravo')
        self.assertEqual(len(linkedin_name_matches(self.db).matches), 1)

    def test_competing_imported_urls_withhold(self):
        self.seed(extra=(PeopleRow(id='linkedin-jordan-two', full_name='Jordan Bravo',
            public_identifier='jordan-bravo-two', linkedin_url='https://www.linkedin.com/in/jordan-bravo-two',
            source_channels='linkedin_csv'),))
        plan = linkedin_name_matches(self.db)
        self.assertEqual(plan.matches, ())
        self.assertEqual(plan.withheld[0].reason, 'ambiguous_linkedin')

    def test_human_no_on_connection_withholds_whole_match(self):
        parents = self.seed()
        self.db.decide_worth(parents['linkedin-jordan'], 'no')
        self.assertEqual(linkedin_name_matches(self.db).matches, ())

    def test_human_detach_withholds(self):
        self.seed()
        self.db.decide_identity('jordan-bravo', 'detach')
        self.assertEqual(linkedin_name_matches(self.db).matches, ())

    def test_extracted_different_linkedin_withholds(self):
        from packs.ingestion.primitives.deep_context.db.projectors import project_person_fact
        self.seed()
        path = self.root / 'facts.jsonl'
        path.write_text(json.dumps({'facts': {'canonical_name': 'Jordan Bravo',
            'owned_identifiers': {'urls': ['https://www.linkedin.com/in/jordan-other']}}}) + '\n')
        project_person_fact(self.db, path, 'candidate:phone:+15550100123')
        self.assertEqual(linkedin_name_matches(self.db).matches, ())

    def test_apply_merges_approves_worth_without_facts_and_rerun_changes_nothing(self):
        from packs.ingestion.primitives.deep_context.db.identity_views import enrichment_queue, judge_candidates
        from packs.ingestion.primitives.deep_context.db.worth_views import worth_queue
        from packs.ingestion.primitives.deep_context.enrich.settle import SettleEnrichment
        from packs.ingestion.primitives.deep_context.realize.export_people import ExportPeople
        from packs.ingestion.primitives.deep_context.db.identity_queries import links
        parents = self.seed(contact='J Bravo')
        plan = linkedin_name_matches(self.db)
        apply_linkedin_name_matches(self.db, plan)
        self.assertEqual(len(queries.parents(self.db)), 1)
        self.assertEqual({row.parent_id for row in queries.people(self.db)}, {parents['linkedin-jordan']})
        self.assertTrue(all(row.machine_approved == 'auto' for row in links(self.db)))
        self.assertTrue(all(row.source == 'deep-context-name-match' for row in links(self.db)))
        self.assertTrue(all(row.decision_action is None for row in links(self.db)))
        self.assertEqual(enrichment_queue(self.db), [])
        self.assertEqual(judge_candidates(self.db), [])
        self.assertEqual(worth_queue(self.db), [])
        SettleEnrichment(db=self.db).run()
        parent = queries.parents(self.db)[0]
        self.assertEqual((parent.machine_worth, parent.source), ('yes', 'deep-context-name-match'))
        before = {table: [tuple(row) for row in self.db.query(f'SELECT * FROM {table}')]
                  for table in ('parents', 'people', 'links', 'candidate_people', 'facts', 'imported_people')}
        apply_linkedin_name_matches(self.db, linkedin_name_matches(self.db))
        self.assertEqual(before, {table: [tuple(row) for row in self.db.query(f'SELECT * FROM {table}')]
                                 for table in before})
        exported = ExportPeople(db=self.db, out_dir=self.root / 'export').run()
        self.assertEqual(exported['rows'], 1)
        self.assertEqual({row.public_identifier for row in queries.imported_people(self.db)}, {'jordan-bravo'})

    def test_one_winner_survives_raw_url_alias_settle_export_and_warm_build(self):
        from packs.ingestion.primitives.deep_context.db.identity_queries import links
        from packs.ingestion.primitives.deep_context.db.identity_views import approved_identities, linkedin_queue
        from packs.ingestion.primitives.deep_context.db.models import CandidatePeopleProjection, CandidatePersonRow, LinkRow
        from packs.ingestion.primitives.deep_context.enrich.profiles.models import ProfileResult, ProfileTarget
        from packs.ingestion.primitives.deep_context.enrich.profiles.projection import project_profile_results
        from packs.ingestion.primitives.deep_context.enrich.settle import SettleEnrichment
        from packs.ingestion.primitives.deep_context.ensure_parents.ensure_parents import EnsureParents
        from packs.ingestion.primitives.deep_context.merge_candidates.build_parents import BuildParents
        from packs.ingestion.primitives.deep_context.realize.export_people import ExportPeople
        parents = self.seed()
        parent = parents['candidate:phone:+15550100123']
        self.db.project_rows((LinkRow('research:jordan', parent, 'jordan-bravo', 'research',
            'https://linkedin.com/in/jordan-bravo', 'Jordan Bravo', True, source='deep-research'),
            CandidatePeopleProjection('research:jordan', (CandidatePersonRow('research:jordan',
                'candidate:phone:+15550100123', parent),))))
        for link in links(self.db):
            if not link.linkedin_url:
                continue
            result = ProfileResult.from_payload('jordan-bravo', link.linkedin_url, {
                'state': 'content', 'normalized_profile': {'success': True, 'full_name': 'Jordan Bravo',
                    'member_id': '42', 'experiences': [{'title': 'Engineer'}]}})
            project_profile_results(self.db, ((ProfileTarget('jordan-bravo', link.linkedin_url,
                link.row_key, link.parent_id), result),), self.root / 'profile-cache')
        self.assertEqual(apply_linkedin_name_matches(self.db), 1)
        SettleEnrichment(db=self.db).run()
        exported = ExportPeople(db=self.db, out_dir=self.root / 'export').run()
        self.assertEqual(exported['rows'], 1)
        EnsureParents(db=self.db, people_csv=self.root / 'export' / 'people.csv').execute()
        BuildParents(db=self.db, parents_dir=self.root / 'parents').execute()
        self.assertEqual(apply_linkedin_name_matches(self.db), 0)
        SettleEnrichment(db=self.db).run()
        self.assertEqual(ExportPeople(db=self.db, out_dir=self.root / 'export').run()['rows'], 1)
        self.assertEqual([row.row_key for row in approved_identities(self.db)], ['jordan-bravo'])
        self.assertEqual(linkedin_queue(self.db), [])
        self.assertEqual(links(self.db, row_keys=('research:jordan',))[0].machine_action, 'detach')

    def test_human_native_approval_keeps_its_contact_scope(self):
        from packs.ingestion.primitives.deep_context.db.identity_queries import links, memberships
        self.seed()
        self.db.decide_identity('jordan-bravo', 'verify')
        before = links(self.db, row_keys=('jordan-bravo',))[0]
        members = tuple(row.person_id for row in memberships(self.db, row_key='jordan-bravo'))
        self.assertEqual(apply_linkedin_name_matches(self.db), 0)
        self.assertEqual(len(queries.parents(self.db)), 2)
        self.assertEqual(linkedin_name_matches(self.db).withheld[0].reason, 'human_decisions')
        after = links(self.db, row_keys=('jordan-bravo',))[0]
        self.assertEqual(before.decision_action, after.decision_action)
        self.assertEqual(before.decided_at, after.decided_at)
        self.assertEqual(members, tuple(row.person_id for row in memberships(self.db, row_key='jordan-bravo')))
        self.assertTrue(all(row.source != 'deep-context-name-match' for row in links(self.db)))

    def test_human_source_retarget_keeps_its_parent_and_decision_scope(self):
        self.seed()
        self.db.decide_identity('candidate:phone:+15550100123', 'retarget',
            replacement_url='https://www.linkedin.com/in/jordan-bravo',
            replacement_public_identifier='jordan-bravo')
        before = [tuple(row) for row in self.db.query('SELECT * FROM links')]

        plan = linkedin_name_matches(self.db)

        self.assertEqual(plan.matches, ())
        self.assertEqual(plan.withheld[0].reason, 'human_decisions')
        self.assertEqual(apply_linkedin_name_matches(self.db, plan), 0)
        self.assertEqual(len(queries.parents(self.db)), 2)
        self.assertEqual(before, [tuple(row) for row in self.db.query('SELECT * FROM links')])

    def test_direct_enrichment_matches_before_research_selection(self):
        from packs.ingestion.primitives.deep_context.db.identity_views import enrichment_queue
        from packs.ingestion.primitives.deep_context.enrich.enrichment_pipeline import EnrichmentPipeline
        from packs.ingestion.primitives.deep_context.manifests.receipt_status import ReceiptStatus
        self.seed()
        def research():
            self.assertEqual(len(queries.parents(self.db)), 1)
            self.assertEqual(enrichment_queue(self.db), [])
            return SimpleNamespace(status=ReceiptStatus.REUSED)
        with (
            mock.patch('packs.ingestion.primitives.deep_context.enrich.research_reconcile.coordinator.ReconcileDeepResearch.run', side_effect=research),
            mock.patch('packs.ingestion.primitives.deep_context.enrich.profiles.prefetch.PrefetchProfiles.run', return_value=SimpleNamespace(status='completed')),
            mock.patch('packs.ingestion.primitives.deep_context.enrich.enrichment_pipeline.judge_mapped_candidates', return_value=SimpleNamespace(judge_errors=0)),
            mock.patch('packs.ingestion.primitives.deep_context.enrich.identity_reconcile.relationship.ReviewRelationships.run', return_value={'status': 'completed'}),
        ):
            pipeline = EnrichmentPipeline(self.db, manifest=self.root / 'enrich' / 'manifest.json')
            self.assertEqual(pipeline.run(total=0, budget=0, request_fingerprint='fixture')['errors'], [])


if __name__ == '__main__':
    unittest.main()
