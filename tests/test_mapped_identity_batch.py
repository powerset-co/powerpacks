"""Mapped identity preparation reads each parent's evidence once per batch."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from packs.ingestion.primitives.deep_context.db import context_queries
from packs.ingestion.primitives.deep_context.db.models import ArtifactRow, FactRow, LinkRow, ParentRow, PersonRow, ResearchRow
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich.research_reconcile import judging
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import (
    IdentityJudgeResult, IdentityUsage, IdentityVerdict, JudgeProfile,
)


class MappedIdentityBatchTests(unittest.TestCase):
    def test_duplicate_candidates_share_the_same_batched_parent_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Db(Path(directory) / 'context.sqlite')
            db.project_rows((ParentRow('jordan', 'jordan'), PersonRow('candidate:jordan', 'jordan'),
                ArtifactRow('facts:jordan', 'facts', 'jordan', '/facts/jordan', 'fixture', 'projected'),
                FactRow('jordan', 'jordan', 'facts:jordan', machine_worth='yes', facts_json=json.dumps(
                    {'canonical_name': 'Jordan Bravo', 'employers': [{'name': 'Oriel Robotics'}]})),
                LinkRow('jordan-a', 'jordan', 'jordan-a', 'pub', source='legacy-migration', linkedin_url='https://linkedin.com/in/jordan-a'),
                LinkRow('jordan-b', 'jordan', 'jordan-b', 'pub', source='legacy-migration', linkedin_url='https://linkedin.com/in/jordan-b')))
            expected = DossierEvidence.from_db(db, ('jordan',))
            expected = replace(expected, name='', dossier=expected.dossier + '\nSource contact names: [""]\nSource contacts: []')
            class Captured(Exception):
                pass
            captured = []
            def capture(tasks, **kwargs):
                captured.extend(tasks)
                raise Captured()
            with patch.object(context_queries, 'dossier_evidence_rows', wraps=context_queries.dossier_evidence_rows) as read, \
                 patch.object(judging.jev_judge, 'judge_batch', side_effect=capture):
                with self.assertRaises(Captured):
                    judging.judge_mapped_candidates(db)
            self.assertEqual(len(captured), 2)
            self.assertEqual([task.evidence for task in captured], [expected, expected])
            self.assertEqual(read.call_count, 1)


class MappedIdentityUrlCacheTests(unittest.TestCase):
    def test_first_research_request_and_settled_rerun_reuse_exact_judgment(self):
        for original_url in ('https://linkedin.com/in/jordan-bravo',
                             'https://www.linkedin.com/in/jordan-bravo/'):
            with self.subTest(url=original_url), tempfile.TemporaryDirectory() as directory:
                db = Db(Path(directory) / 'context.sqlite')
                db.project_rows((ParentRow('jordan', 'jordan'), PersonRow('person-jordan', 'jordan'),
                    ArtifactRow('facts:jordan', 'facts', 'jordan', '/facts/jordan', 'fixture', 'projected'),
                    FactRow('jordan', 'jordan', 'facts:jordan', machine_worth='yes',
                            facts_json=json.dumps({'canonical_name': 'Jordan Bravo'})),
                    LinkRow('jordan-profile', 'jordan', 'jordan-bravo', 'pub',
                            linkedin_url=original_url, source='deep-research'),
                    ResearchRow('jordan-research', 'jordan', 'complete', candidate_key='jordan-profile',
                        result_json=json.dumps({'type': 'json', 'content': {'linkedin_url': original_url,
                            'real_name': 'Jordan Bravo', 'work_experience': [], 'education': []}, 'basis': []}))))

                def hydrated(source, projected):
                    return JudgeProfile.from_payload({'linkedin_url': source.linkedin_url,
                        'public_identifier': 'jordan-bravo', 'full_name': 'Jordan Bravo',
                        'experiences': ['Engineer @ Oriel Robotics'], 'source': 'cache'})

                requests = []
                def answer(tasks, *, imported_urls, reference_date, **kwargs):
                    requests.extend(judging.jev_judge._requests(task, urls, reference_date)
                                    for task, urls in zip(tasks, imported_urls))
                    return [IdentityJudgeResult(
                        IdentityVerdict.from_payload({'verdict': 'needs_review', 'confidence': .6}),
                        IdentityUsage(), '', judging.jev_judge.judgment_fingerprint(task, urls, reference_date))
                        for task, urls in zip(tasks, imported_urls)]

                with patch.object(judging, 'linkedin_view', side_effect=hydrated), patch.object(
                    judging.jev_judge, 'judge_batch', side_effect=answer) as paid:
                    first = judging.judge_mapped_candidates(db)
                    second = judging.judge_mapped_candidates(db)
                self.assertEqual(first.judge_calls, 1)
                self.assertEqual((second.judge_calls, second.cached_verdicts), (0, 1))
                paid.assert_called_once()
                self.assertEqual(requests[0]['network']['state']['profile']['linkedin_url'],
                                 'https://www.linkedin.com/in/jordan-bravo')
