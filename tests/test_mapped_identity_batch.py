"""Mapped identity preparation reads each parent's evidence once per batch."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packs.ingestion.primitives.deep_context.db import context_queries
from packs.ingestion.primitives.deep_context.db.models import ArtifactRow, FactRow, LinkRow, ParentRow, PersonRow
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich.research_reconcile import judging
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence


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
