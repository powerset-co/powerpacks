"""Ordinary cached identity runs preserve earlier family settlement."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from parallel.types import TaskRunJsonOutput
from packs.ingestion.primitives.deep_context.db.models import ArtifactRow, FactRow, LinkRow, ParentRow, PersonRow
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.view_models import EnrichmentQueueRow
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile import judge, runner, settlement
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import IdentityJudgeResult, IdentityUsage, IdentityVerdict
from packs.ingestion.primitives.deep_context.enrich.parallel_research.result import ResearchResult
from packs.ingestion.primitives.deep_context.enrich.profiles import projection
from packs.ingestion.primitives.deep_context.enrich.profiles.models import ProfileResult, ProfileTarget
from packs.ingestion.primitives.deep_context.enrich.research_reconcile import judging
from packs.ingestion.primitives.deep_context.manifests.reconcile_linkedin_manifest import ReconcileLinkedinManifest


class CachedFamilyTest(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.db = Db(self.root / 'fixture.sqlite')
        self.db.project_rows((
            ParentRow('parent-jordan', 'parent-worth:parent-jordan', 'Jordan Bravo'),
            PersonRow('person-jordan', 'parent-jordan'),
            ArtifactRow('facts-jordan', 'facts', 'parent-jordan', 'fixture.json', 'fixture', 'projected'),
            FactRow('person-jordan', 'parent-jordan', 'facts-jordan', person_id='person-jordan', machine_worth='yes', facts_json='{}'),
        ))
        self.addCleanup(patch.stopall)
        patch('socket.socket.connect', side_effect=AssertionError('No providers')).start()
        self.judge = patch.object(judge, 'judge_batch', side_effect=self.judge_results).start()
        self.clock = patch.object(settlement, 'now_iso', return_value='2026-01-01T00:00:01Z').start()

    def add_attached(self, key, *, profile=True):
        url = 'https://www.linkedin.com/in/' + key
        self.db.project_rows((LinkRow(key, 'parent-jordan', key, 'pub', linkedin_url=url, source='deep-context-reconcile'),))
        if profile:
            result = ProfileResult.from_payload(key, url, {'normalized_profile': {
                'success': True, 'public_identifier': key, 'linkedin_url': url,
                'full_name': 'Jordan Bravo', 'experiences': [{'title': 'Founder', 'company_name': 'Bravo Robotics'}],
            }})
            projection.project_profile_results(self.db, [(ProfileTarget(key, url, key, 'parent-jordan'), result)], self.root)

    def judge_results(self, tasks, *, owner_block='', model, effort, **kwargs):
        verdict = IdentityVerdict.from_payload({'verdict': 'confirmed', 'confidence': .90, 'reason': 'Synthetic employer anchor.'})
        return [IdentityJudgeResult(verdict, IdentityUsage(), '', judge.task_fingerprint(task, owner_block, model=model, effort=effort)) for task in tasks]

    def run_stage(self, **kwargs):
        return runner.run_stage(ReconcileLinkedinManifest, db=self.db, profile_cache_dir=self.root,
            confirm_threshold=.70, detach_threshold=.85, model='claude-opus-5', requested_effort='high',
            concurrency=1, timeout=120, max_retries=0, reapply=kwargs.pop('reapply', False), **kwargs)

    def rows(self):
        return [dict(row) for row in self.db.query('SELECT * FROM links ORDER BY row_key')]

    def research_conflict(self):
        key = 'candidate:email:jordan@example.com'
        self.db.project_rows((LinkRow(key, 'parent-jordan', key, 'candidate_email', source='deep-context-reconcile'),))
        result = ResearchResult.from_output(TaskRunJsonOutput(type='json', content={
            'real_name': 'Jordan Bravo', 'linkedin_url': 'https://www.linkedin.com/in/jordan-other',
            'work_experience': [], 'education': [], 'location_city': '', 'location_country': '',
        }, basis=[]))
        subset = [EnrichmentQueueRow('parent-jordan', 'jordan', 'Jordan Bravo', ('person-jordan',), key, True, '', '', '', (), (), True)]
        with patch.object(projection, 'hydrate_profiles'):
            return judging.propose_retargets(subset, db=self.db, provided_results={'jordan': result},
                profile_cache_dir=self.root, model='claude-opus-5', effort='high')

    def test_cached_family_does_not_reapprove_after_research_conflict(self):
        self.add_attached('jordan-bravo')
        self.run_stage()
        self.research_conflict()
        before = self.rows()
        self.assertTrue(all(row['machine_approved'] is None for row in before))
        self.judge.side_effect = AssertionError('Cached run must not judge')
        self.clock.return_value = '2026-01-01T00:00:02Z'
        result = self.run_stage()
        self.assertEqual(self.rows(), before)
        self.assertEqual((result.tasks, result.reused, result.judged, result.needs_review), (1, 1, 0, 1))
        self.assertEqual(result.overrides.verified, 0)

    def test_fresh_sibling_arbitrates_with_cached_candidate(self):
        self.add_attached('jordan-bravo')
        self.run_stage()
        self.add_attached('jordan-other')
        result = self.run_stage()
        self.assertEqual((result.tasks, result.reused, result.judged), (2, 1, 1))
        self.assertEqual((result.needs_review, result.conflicts_to_review), (2, 2))
        self.assertTrue(all(row['machine_action'] == 'review' and row['machine_approved'] is None for row in self.rows()))
        before = self.rows()
        self.judge.side_effect = AssertionError('Cached family must not judge')
        self.clock.return_value = '2026-01-01T00:00:03Z'
        cached = self.run_stage()
        self.assertEqual(self.rows(), before)
        self.assertEqual((cached.needs_review, cached.conflicts_to_review), (2, 2))

    def test_unchanged_no_profile_rule_preserves_full_rows(self):
        self.add_attached('jordan-bravo', profile=False)
        with patch.object(projection, 'provider_key_available', return_value=False):
            self.run_stage()
            before = self.rows()
            self.clock.return_value = '2026-01-01T00:00:02Z'
            result = self.run_stage()
        self.assertEqual(self.rows(), before)
        self.assertEqual((result.tasks, result.judged, result.needs_review), (1, 0, 1))

    def assert_explicit_reconsideration(self, flag):
        self.add_attached('jordan-bravo')
        self.run_stage()
        self.research_conflict()
        self.assertTrue(all(row['machine_approved'] is None for row in self.rows()))
        result = self.run_stage(**{flag: True})
        row = next(row for row in self.rows() if row['row_key'] == 'jordan-bravo')
        self.assertEqual(row['machine_approved'], 'auto')
        self.assertEqual(result.judged, 1 if flag == 'force' else 0)

    def test_force_rejudges_cached_family(self):
        self.assert_explicit_reconsideration('force')

    def test_reapply_reconsiders_cached_family_without_judging(self):
        self.assert_explicit_reconsideration('reapply')


if __name__ == '__main__':
    unittest.main()
