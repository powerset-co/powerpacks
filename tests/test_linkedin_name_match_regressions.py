import json
import unittest

import test_linkedin_name_matches as fixtures

from packs.ingestion.primitives.deep_context.db import identity_queries, identity_views, queries
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    CandidatePeopleProjection,
    CandidatePersonRow,
    LinkRow,
)
from packs.ingestion.primitives.deep_context.db.projectors import project_person_fact
from packs.ingestion.primitives.deep_context.enrich.settle import SettleEnrichment
from packs.ingestion.primitives.deep_context.merge_candidates.cluster_merge_candidates import ClusterMergeCandidates
from packs.ingestion.primitives.deep_context.merge_candidates.linkedin_name_matches import (
    apply_linkedin_name_matches,
    linkedin_name_matches,
)
from packs.ingestion.primitives.deep_context.realize.export_people import ExportPeople
from packs.ingestion.primitives.pipeline.contract import PeopleRow


class LinkedInNameMatchRegressionsTests(unittest.TestCase):
    setUp = fixtures.LinkedInNameMatchesTests.setUp
    seed = fixtures.LinkedInNameMatchesTests.seed

    def _fact(self, name, url=None):
        facts = {'canonical_name': name}
        if url:
            facts['owned_identifiers'] = {'urls': [url]}
        path = self.root / 'contact.jsonl'
        path.write_text(json.dumps({'facts': facts}) + '\n')
        project_person_fact(self.db, path, 'candidate:phone:+15550100123')

    def test_one_word_canonical_name_conflicts_with_imported_given_name(self):
        parents = self.seed()
        self._fact('Casey')

        plan = linkedin_name_matches(self.db)

        self.assertEqual(plan.matches, ())
        self.assertIn((parents['candidate:phone:+15550100123'], 'conflicting_names'),
                      {(row.parent_id, row.reason) for row in plan.withheld})

    def test_persisted_dossier_url_conflicts_without_local_artifact_file(self):
        parents = self.seed()
        path = self.root / 'unavailable-dossier.md'
        self.db.project_rows((ArtifactRow(
            'dossier:contact', 'dossier', parents['candidate:phone:+15550100123'],
            str(path), 'synthetic-fingerprint', 'projected',
            payload_json=json.dumps({'body': 'https://www.linkedin.com/in/casey-delta'}),
        ),))
        self.assertFalse(path.exists())

        plan = linkedin_name_matches(self.db)

        self.assertEqual(plan.matches, ())
        self.assertIn('conflicting_linkedin', {row.reason for row in plan.withheld})

    def test_late_conflict_after_export_withdraws_machine_approval_and_queues_review(self):
        parents = self.seed(extra=(PeopleRow(
            id='candidate:phone:+15550100456', full_name='Casey Delta',
            primary_phone='+15550100456', source_channels='whatsapp',
        ),))
        self.db.decide_worth(parents['candidate:phone:+15550100123'], 'yes', note='Keep contact')
        self.db.decide_identity('candidate:phone:+15550100456', 'exclude', note='Human choice')
        sources = {table: [tuple(row) for row in self.db.query(f'SELECT * FROM {table}')]
                   for table in ('person_sources', 'person_identifiers')}
        self.assertEqual(apply_linkedin_name_matches(self.db), 1)
        self.assertEqual(ExportPeople(db=self.db, out_dir=self.root / 'export').run()['rows'], 2)
        human_worth = [tuple(row) for row in self.db.query(
            'SELECT parent_id,human_worth,human_worth_note,human_worth_source,human_worth_at '
            'FROM parents WHERE human_worth IS NOT NULL')]
        human_identity = [tuple(row) for row in self.db.query(
            'SELECT row_key,decision_action,decision_approved,decision_note,decision_source,decided_at '
            'FROM links WHERE decision_action IS NOT NULL')]
        self._fact('Casey', 'https://www.linkedin.com/in/casey-delta')

        apply_linkedin_name_matches(self.db)

        native = next(row for row in identity_queries.links(self.db) if row.row_key == 'jordan-bravo')
        self.assertNotIn(native.machine_approved, {'yes', 'auto'})
        self.assertIn(parents['linkedin-jordan'],
                      {row.parent_id for row in identity_views.linkedin_queue(self.db)})
        self.assertEqual(sources, {
            table: [tuple(row) for row in self.db.query(f'SELECT * FROM {table}')]
            for table in sources
        })
        self.assertEqual(human_worth, [tuple(row) for row in self.db.query(
            'SELECT parent_id,human_worth,human_worth_note,human_worth_source,human_worth_at '
            'FROM parents WHERE human_worth IS NOT NULL')])
        self.assertEqual(human_identity, [tuple(row) for row in self.db.query(
            'SELECT row_key,decision_action,decision_approved,decision_note,decision_source,decided_at '
            'FROM links WHERE decision_action IS NOT NULL')])
        self.assertEqual({row.id: (row.full_name, row.primary_phone, row.source_channels)
                          for row in queries.imported_people(self.db)}, {
            'linkedin-jordan': ('Jordan Bravo', '', 'linkedin_csv'),
            'candidate:phone:+15550100123': ('Jordan Bravo', '+15550100123', 'whatsapp'),
            'candidate:phone:+15550100456': ('Casey Delta', '+15550100456', 'whatsapp'),
        })

    def test_cluster_dry_run_leaves_all_database_rows_unchanged(self):
        self.seed()
        tables = [row['name'] for row in self.db.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        before = {table: [tuple(row) for row in self.db.query(f'SELECT * FROM {table}')]
                  for table in tables}
        cluster = ClusterMergeCandidates(
            db=self.db, dossier_dir=self.root / 'dossiers',
            out_csv=self.root / 'merges.csv', out_md=self.root / 'merges.md',
        )

        estimate = cluster.estimate()

        self.assertEqual(estimate['status'], 'dry_run')
        self.assertEqual(estimate['linkedin_name_matches'], 1)
        self.assertEqual(before, {
            table: [tuple(row) for row in self.db.query(f'SELECT * FROM {table}')]
            for table in tables
        })
        self.assertFalse((self.root / 'dossiers' / 'merge_manifest.json').exists())

    def test_equivalent_research_url_keeps_one_native_winner_through_export_and_rerun(self):
        parents = self.seed()
        parent_id = parents['candidate:phone:+15550100123']
        self.db.project_rows((
            LinkRow('research:jordan', parent_id, 'jordan-bravo', 'research',
                    linkedin_url='https://linkedin.com/in/jordan-bravo',
                    display_name='Jordan Bravo', candidate_origin=True, source='deep-research'),
            CandidatePeopleProjection('research:jordan', (
                CandidatePersonRow('research:jordan', 'candidate:phone:+15550100123', parent_id),
            )),
        ))

        for _ in range(2):
            apply_linkedin_name_matches(self.db)
            SettleEnrichment(db=self.db).run()
            ExportPeople(db=self.db, out_dir=self.root / 'export').run()

            rows = identity_queries.links(self.db)
            native = next(row for row in rows if row.row_key == 'jordan-bravo')
            self.assertEqual((native.machine_action, native.machine_approved), ('verify', 'auto'))
            approved = [row.row_key for row in rows
                        if row.machine_action in {'verify', 'retarget'} and row.machine_approved in {'yes', 'auto'}]
            self.assertEqual(approved, ['jordan-bravo'])
            self.assertEqual(next(row.machine_action for row in rows if row.row_key == 'research:jordan'), 'detach')
            self.assertEqual({row.person_id for row in identity_queries.memberships(self.db)
                              if row.row_key == 'jordan-bravo'}, set(parents))
            self.assertEqual(identity_views.linkedin_queue(self.db), [])
            self.assertEqual(identity_views.judge_candidates(self.db), [])
            self.assertEqual({row.public_identifier for row in queries.imported_people(self.db)}, {'jordan-bravo'})


if __name__ == '__main__':
    unittest.main()
