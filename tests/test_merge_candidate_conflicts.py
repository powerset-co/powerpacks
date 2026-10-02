"""A shared candidate must not merge contacts judged to be different people."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import packs.ingestion.primitives.deep_context.merge_candidates.build_parents as build_parents
from packs.ingestion.primitives.deep_context.merge_candidates.candidate_pairs import accepted_edges

from packs.ingestion.primitives.deep_context.merge_candidates.models import (
    MergeDecision, MergePairVerdict, MergePerson,
)
from packs.ingestion.primitives.deep_context.merge_candidates.receipts import _confirmed, verdict_rows


class MergeConflictTests(unittest.TestCase):
    def test_repair_separates_only_given_names_that_spell_differently(self):
        from packs.ingestion.primitives.deep_context.db.merge_repair import _given_names_differ
        for first, second in [('Jordan Bravo', 'Casey Bravo'), ('Casey Bravo', 'Jordan Bravo'),
                              ('Jordan Bravo', 'Casey Delta')]:
            with self.subTest(first=first, second=second):
                self.assertTrue(_given_names_differ(first, second))
        for first, second in [('Bravo, Jordan', 'Jordan Bravo'), ('J Bravo', 'Jordan Bravo'),
                              ('Casey B', 'Casey Bravo'), ('Bravo', 'Casey Bravo'),
                              ('Ann Bravo', 'Annmay Bravo'), ('Ann', 'Annmay Bravo'),
                              ('Casey Bravo', 'Casey Morgan Delta')]:
            with self.subTest(first=first, second=second):
                self.assertFalse(_given_names_differ(first, second))

    def test_a_pair_decided_two_people_blocks_a_bridge_between_them(self):
        edges = accepted_edges([('a', 'b', True, .99), ('b', 'c', True, .92), ('a', 'c', False, .05)])
        self.assertEqual(edges, [('a', 'b')])

    def test_hub_does_not_override_negative_leaf_pair(self):
        people = [MergePerson(key, key, key, key) for key in ('a', 'b', 'c')]
        a, b, c = people
        verdicts = [
            MergePairVerdict(a, c, 'ac', MergeDecision(True, .6, True, '', 'llm')),
            MergePairVerdict(b, c, 'bc', MergeDecision(False, .1, True, '', 'llm')),
            MergePairVerdict(a, b, 'ab', MergeDecision(True, .8, True, '', 'llm')),
        ]
        _, groups = _confirmed(people, verdicts)
        self.assertEqual(groups, [['a', 'b']])
        accepted = {(r.person_a, r.person_b) for r in verdict_rows(verdicts) if r.accepted}
        self.assertEqual(accepted, {('a', 'b')})

    def test_parent_application_does_not_override_negative_leaf_pair(self):
        people = [SimpleNamespace(person_id=key, parent_id=key, display_name=key) for key in ('a', 'b', 'c')]
        verdicts = [
            SimpleNamespace(person_a=a, person_b=b, same_person=same, accepted=same,
                            confidence=score, updated_at='2026-10-01T00:00:00Z')
            for a, b, same, score in [('a', 'b', True, .8), ('a', 'c', True, .6), ('b', 'c', False, .1)]
        ]
        with patch.object(build_parents, 'person_rows', return_value=people), patch.object(
            build_parents, 'merge_verdicts', return_value=verdicts,
        ):
            self.assertEqual(build_parents._accepted_components(None), (('a', 'b'),))

    def test_tied_scores_have_stable_selection(self):
        verdicts = [('a', 'c', True, .8), ('b', 'c', False, .1), ('a', 'b', True, .8)]
        self.assertEqual(accepted_edges(verdicts), [('a', 'b')])
        self.assertEqual(accepted_edges(list(reversed(verdicts))), [('a', 'b')])

    def test_consistent_positive_triangle_remains_accepted(self):
        self.assertEqual(len(accepted_edges([
            ('a', 'b', True, .8), ('a', 'c', True, .7), ('b', 'c', True, .6),
        ])), 3)

    def test_valid_transitive_chain_remains_joined(self):
        from packs.ingestion.primitives.deep_context.merge_candidates.candidate_pairs import connected_components
        edges = accepted_edges([('a', 'b', True, .8), ('b', 'c', True, .7)])
        self.assertEqual(connected_components(['a', 'b', 'c'], edges), [['a', 'b', 'c']])

    def test_exact_identifier_positive_cannot_override_explicit_negative(self):
        a = MergePerson('a', 'a', 'Jordan Bravo', 'jordan bravo', emails=('jordan@example.com',))
        b = MergePerson('b', 'b', 'Jordan Bravo', 'jordan bravo', emails=('jordan@example.com',))
        c = MergePerson('c', 'c', 'Jordan Bravo', 'jordan bravo', emails=('jordan@example.com',))
        verdicts = [
            MergePairVerdict(a, b, 'ab', MergeDecision(True, .99, True, '', 'slam_dunk')),
            MergePairVerdict(a, c, 'ac', MergeDecision(True, .99, True, '', 'slam_dunk')),
            MergePairVerdict(b, c, 'bc', MergeDecision(False, .1, True, '', 'llm')),
        ]
        _, groups = _confirmed([a, b, c], verdicts)
        self.assertEqual(groups, [['a', 'b']])

    def test_receipts_and_parent_application_select_the_same_edges(self):
        people = [MergePerson(key, key, key, key, parent_id=key) for key in ('a', 'b', 'c')]
        a, b, c = people
        verdicts = [
            MergePairVerdict(a, b, 'ab', MergeDecision(True, .8, True, '', 'llm')),
            MergePairVerdict(a, c, 'ac', MergeDecision(True, .6, True, '', 'llm')),
            MergePairVerdict(b, c, 'bc', MergeDecision(False, .1, True, '', 'llm')),
        ]
        stored = verdict_rows(verdicts)
        _, groups = _confirmed(people, verdicts)
        with patch.object(build_parents, 'person_rows', return_value=[
            SimpleNamespace(person_id=p.person_id, parent_id=p.parent_id, display_name=p.name) for p in people
        ]), patch.object(
            build_parents, 'merge_verdicts', return_value=stored,
        ):
            self.assertEqual(build_parents._accepted_components(None), tuple(tuple(g) for g in groups))

    def test_older_negative_between_children_blocks_newer_positive_between_parents(self):
        people = [SimpleNamespace(person_id=child, parent_id=parent, display_name=parent)
                  for child, parent in [('a1', 'a'), ('a2', 'a'), ('b1', 'b'), ('b2', 'b')]]
        verdicts = [
            SimpleNamespace(person_a='a1', person_b='b1', same_person=False,
                            accepted=False, confidence=.1, updated_at='2026-10-01T01:00:00Z'),
            SimpleNamespace(person_a='a2', person_b='b2', same_person=True,
                            accepted=True, confidence=.9, updated_at='2026-10-01T02:00:00Z'),
        ]
        with patch.object(build_parents, 'person_rows', return_value=people), patch.object(
            build_parents, 'merge_verdicts', return_value=verdicts,
        ):
            self.assertEqual(build_parents._accepted_components(None), ())

    def test_pair_signature_changes_with_every_rendered_evidence_field(self):
        from dataclasses import replace
        from packs.ingestion.primitives.deep_context.merge_candidates.judge import judge_prompt
        from packs.ingestion.primitives.deep_context.merge_candidates.receipts import pair_sig
        from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence
        first = MergePerson('a', 'a', 'Jordan Bravo', 'jordan bravo')
        second = MergePerson('b', 'b', 'Casey Delta', 'casey delta')
        for field, value in [('from_me', ('Hello Jordan',)), ('from_them', ('I am Casey Delta',))]:
            with self.subTest(field=field):
                changed = replace(first, evidence=DossierEvidence(**{field: value}))
                self.assertNotEqual(judge_prompt(first, second), judge_prompt(changed, second))
                self.assertNotEqual(pair_sig(first, second), pair_sig(changed, second))

    def test_aggregate_parent_cannot_rejudge_a_source_child_rejection(self):
        import tempfile
        from pathlib import Path
        from packs.ingestion.primitives.deep_context.db.models import ParentRow, PersonRow
        from packs.ingestion.primitives.deep_context.db.store import Db
        import packs.ingestion.primitives.deep_context.merge_candidates.receipts as receipts
        with tempfile.TemporaryDirectory() as directory:
            db = Db(Path(directory) / 'context.sqlite')
            db.project_rows(tuple(ParentRow(key, key) for key in ('a', 'b', 'c')) +
                            tuple(PersonRow(key, key) for key in ('a', 'b', 'c')))
            a = MergePerson('a', 'a', 'Jordan Bravo', 'jordan bravo', parent_id='a', member_person_ids=('a',))
            b = MergePerson('b', 'b', 'Jordan Bravo', 'jordan bravo', parent_id='b', member_person_ids=('b',))
            db.replace_merge_verdicts(verdict_rows([
                MergePairVerdict(a, b, receipts.pair_sig(a, b), MergeDecision(False, .1, True, '', 'llm')),
            ]))
            # New evidence can change a singleton judgment.
            from dataclasses import replace
            from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence
            changed = replace(a, evidence=DossierEvidence(from_them=('I work with Jordan at Oriel Robotics',)))
            with patch.object(receipts, 'merge_people', return_value=[changed, b]):
                self.assertEqual(len(receipts.survey_pairs(db).to_judge), 1)
            # A later aggregate uses the same representative ID but cannot erase the source-pair no.
            db.merge_parents('a', 'c')
            aggregate = replace(changed, member_person_ids=('a', 'c'), emails=('jordan@example.com',))
            same_email = replace(b, emails=('jordan@example.com',))
            with patch.object(receipts, 'merge_people', return_value=[aggregate, same_email]):
                self.assertEqual(receipts.survey_pairs(db, refresh=True).to_judge, [])
                self.assertEqual(receipts.survey_pairs(db).pairs, [])
            self.assertFalse(db.query('SELECT same_person FROM merge_verdicts')[0]['same_person'])

    def test_extracted_shared_office_phone_cannot_skip_the_judge(self):
        from packs.ingestion.primitives.deep_context.merge_candidates.candidate_pairs import slam_dunk_verdict
        from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence
        first = MergePerson('a', 'a', 'Jordan B', 'jordan b',
                            extra_phones=('15550100100',),
                            evidence=DossierEvidence(from_them=('That is our shared office number.',)))
        second = MergePerson('b', 'b', 'Jordan B', 'jordan b', extra_phones=('15550100100',))
        self.assertIsNone(slam_dunk_verdict(first, second))

    def test_extracted_shared_email_cannot_skip_the_judge(self):
        from packs.ingestion.primitives.deep_context.merge_candidates.candidate_pairs import slam_dunk_verdict
        first = MergePerson('a', 'a', 'Jordan B', 'jordan b', extra_emails=('office@example.com',))
        second = MergePerson('b', 'b', 'Jordan B', 'jordan b', extra_emails=('office@example.com',))
        self.assertIsNone(slam_dunk_verdict(first, second))

    def test_extracted_shared_phone_under_one_full_name_merges_on_the_name_alone(self):
        from packs.ingestion.primitives.deep_context.merge_candidates.candidate_pairs import SAME_FULL_NAME, slam_dunk_verdict
        from packs.ingestion.primitives.deep_context.merge_candidates.judge import asks_keep_apart
        first = MergePerson('a', 'a', 'Jordan Bravo', 'jordan bravo', extra_phones=('15550100100',))
        second = MergePerson('b', 'b', 'Jordan Bravo', 'jordan bravo', extra_phones=('15550100100',))
        verdict = slam_dunk_verdict(first, second)
        # The office number is not credited; the name is, and the facts are still read.
        self.assertEqual(verdict.reason, SAME_FULL_NAME)
        self.assertTrue(asks_keep_apart(verdict))

    def test_shared_direct_contact_email_remains_free(self):
        from packs.ingestion.primitives.deep_context.merge_candidates.candidate_pairs import slam_dunk_verdict
        first = MergePerson('a', 'a', 'Jordan Bravo', 'jordan bravo', emails=('jordan@example.com',))
        second = MergePerson('b', 'b', 'Jordan Bravo', 'jordan bravo', emails=('jordan@example.com',))
        self.assertTrue(slam_dunk_verdict(first, second).same_person)

    def test_stored_singleton_no_cannot_be_replaced_by_slam_dunk(self):
        import tempfile
        from pathlib import Path
        from packs.ingestion.primitives.deep_context.db.models import ParentRow, PersonRow
        from packs.ingestion.primitives.deep_context.db.store import Db
        import packs.ingestion.primitives.deep_context.merge_candidates.receipts as receipts
        with tempfile.TemporaryDirectory() as directory:
            db = Db(Path(directory) / 'context.sqlite')
            db.project_rows((ParentRow('a', 'a'), ParentRow('b', 'b'), PersonRow('a', 'a'), PersonRow('b', 'b')))
            a = MergePerson('a', 'a', 'Jordan Bravo', 'jordan bravo', parent_id='a', member_person_ids=('a',), emails=('jordan@example.com',))
            b = MergePerson('b', 'b', 'Jordan Bravo', 'jordan bravo', parent_id='b', member_person_ids=('b',), emails=('jordan@example.com',))
            db.replace_merge_verdicts(verdict_rows([
                MergePairVerdict(a, b, receipts.pair_sig(a, b), MergeDecision(False, .98, True, 'Shared office email', 'llm')),
            ]))
            with patch.object(receipts, 'merge_people', return_value=[a, b]):
                survey = receipts.survey_pairs(db)
                self.assertEqual(survey.slam, [])
                self.assertEqual(len(survey.reused), 1)
                self.assertFalse(survey.reused[0].decision.same_person)
                refreshed = receipts.survey_pairs(db, refresh=True)
                self.assertEqual(refreshed.slam, [])
                self.assertEqual(len(refreshed.to_judge), 1)
